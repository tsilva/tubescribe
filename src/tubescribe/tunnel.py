"""A dedicated SSH master owns the forward; stopping it cannot close other masters."""

import json
import socket
import subprocess
import time
from pathlib import Path

from .asr import Client
from .storage import atomic_write


def tunnel(root: Path, action: str, host: str, local_port: int, remote_port: int):
    root.mkdir(parents=True, exist_ok=True)
    control = root.resolve() / "main-ssh.sock"
    if len(str(control).encode()) > 100:
        raise ValueError("Data path too long for an SSH control socket; use a shorter --data path")
    state = root / "tunnel.json"
    cmd = [
        "ssh",
        "-S",
        str(control),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        host,
    ]
    alive = subprocess.run([*cmd[:-1], "-O", "check", host], capture_output=True).returncode == 0
    if action == "stop":
        if alive:
            subprocess.run([*cmd[:-1], "-O", "exit", host], check=True)
        control.unlink(missing_ok=True)
        state.unlink(missing_ok=True)
        return {"stopped": True}
    if action == "status":
        saved = json.loads(state.read_text()) if state.exists() else None
        return {
            "connected": alive,
            "forward": saved,
            "health": Client(saved["endpoint"]).health() if alive and saved else None,
        }
    if alive:
        saved = json.loads(state.read_text()) if state.exists() else None
        if saved and (
            saved.get("host") != host
            or saved.get("remote_port") != remote_port
            or (local_port and saved.get("local_port") != local_port)
        ):
            raise RuntimeError(
                "Existing forward differs; stop the tunnel before changing its settings"
            )
        if saved:
            try:
                return {**saved, "health": Client(saved["endpoint"]).health(), "reused": True}
            except Exception as error:
                with socket.socket() as probe:
                    probe.settimeout(1)
                    if probe.connect_ex(("127.0.0.1", saved["local_port"])) == 0:
                        raise RuntimeError(
                            f"SSH is connected, but ASR health failed: {error}. Inspect remote-service.sh logs."
                        ) from error
        # Adopt an interactively authorized dedicated master without asking for a new signature.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", saved["local_port"] if saved else local_port))
            local_port = probe.getsockname()[1]
        endpoint = f"http://127.0.0.1:{local_port}"
        forward = f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}"
        subprocess.run([*cmd[:-1], "-O", "forward", "-L", forward, host], check=True)
        saved = {
            "host": host,
            "local_port": local_port,
            "remote_port": remote_port,
            "endpoint": endpoint,
        }
        atomic_write(state, json.dumps(saved, indent=2).encode())
        return {**saved, "health": Client(endpoint).health(), "reused": True}
    control.unlink(missing_ok=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", local_port))
        local_port = s.getsockname()[1]
    endpoint = f"http://127.0.0.1:{local_port}"
    forward = f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}"
    # A separate session survives the terminal/tool process that starts it.
    with (root / "tunnel.log").open("ab") as output:
        process = subprocess.Popen(
            [
                *cmd[:-1],
                "-o",
                "ControlMaster=yes",
                "-o",
                "ControlPersist=no",
                "-o",
                "ExitOnForwardFailure=yes",
                "-NT",
                "-L",
                forward,
                host,
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
        )
    for _ in range(30):
        if process.poll() is not None:
            raise RuntimeError(
                f"SSH tunnel failed (exit {process.returncode}); inspect {root / 'tunnel.log'}"
            )
        if subprocess.run([*cmd[:-1], "-O", "check", host], capture_output=True).returncode == 0:
            break
        time.sleep(1)
    else:
        process.terminate()
        raise RuntimeError(f"SSH tunnel connection timed out; inspect {root / 'tunnel.log'}")
    saved = {
        "host": host,
        "local_port": local_port,
        "remote_port": remote_port,
        "endpoint": endpoint,
    }
    atomic_write(state, json.dumps(saved, indent=2).encode())
    error = None
    for _ in range(5):
        try:
            return {**saved, "health": Client(endpoint).health(), "reused": False}
        except Exception as e:
            error = e
            time.sleep(1)
    # A healthy SSH connection can point at an unready service; preserve it for retry.
    raise RuntimeError(
        f"Tunnel established at {endpoint}, but ASR health failed: {error}. Inspect remote-service.sh logs."
    )
