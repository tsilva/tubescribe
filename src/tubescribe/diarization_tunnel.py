"""Add/cancel a diarization forward on the authorized main SSH master."""

import json
import socket
import subprocess
from pathlib import Path

from .diarization import Client
from .storage import atomic_write
from .tunnel import tunnel


def endpoint(root):
    import os

    if os.environ.get("TUBESCRIBE_DIARIZATION_ENDPOINT"):
        return os.environ["TUBESCRIBE_DIARIZATION_ENDPOINT"]
    state = root / "diarization-tunnel.json"
    return json.loads(state.read_text())["endpoint"] if state.exists() else "http://127.0.0.1:18768"


def forward(root: Path, action="start", local_port=18768, remote_port=18767):
    config = root / "tunnel.json"
    main = (
        json.loads(config.read_text())
        if config.exists()
        else {"host": "tubescribe-beast", "local_port": 18766, "remote_port": 18765}
    )
    state_file = root / "diarization-tunnel.json"
    saved = json.loads(state_file.read_text()) if state_file.exists() else None
    control = root.resolve() / "main-ssh.sock"
    cmd = ["ssh", "-S", str(control), "-o", "BatchMode=yes"]
    if action == "status":
        return {"forward": saved, "health": Client(saved["endpoint"]).health() if saved else None}
    if action == "stop":
        if saved:
            spec = f"127.0.0.1:{saved['local_port']}:127.0.0.1:{saved['remote_port']}"
            if (
                subprocess.run([*cmd, "-O", "check", main["host"]], capture_output=True).returncode
                == 0
            ):
                subprocess.run([*cmd, "-O", "cancel", "-L", spec, main["host"]], check=True)
            state_file.unlink(missing_ok=True)
        return {"stopped": True, "main_tunnel_preserved": True}
    # Restore the shared master after disconnection; keep the ASR forward intact.
    tunnel(root, "start", main["host"], main["local_port"], main["remote_port"])
    if saved:
        try:
            return {**saved, "health": Client(saved["endpoint"]).health(), "reused": True}
        except Exception:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", saved["local_port"])) == 0:
                    raise RuntimeError(
                        "Diarization port is occupied but service health failed; inspect the remote service"
                    )
        local_port, remote_port = saved["local_port"], saved["remote_port"]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", local_port))
        local_port = probe.getsockname()[1]
    spec = f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}"
    subprocess.run([*cmd, "-O", "forward", "-L", spec, main["host"]], check=True)
    saved = {
        "host": main["host"],
        "local_port": local_port,
        "remote_port": remote_port,
        "endpoint": f"http://127.0.0.1:{local_port}",
    }
    atomic_write(state_file, json.dumps(saved, indent=2).encode())
    return {**saved, "health": Client(saved["endpoint"]).health(), "reused": False}


def ready(root):
    import os

    from .inference import client_options

    client = Client(endpoint(root), **client_options())
    try:
        client.health()
    except Exception:
        if os.environ.get("TUBESCRIBE_DIARIZATION_ENDPOINT"):
            raise
        forward(root)
        client = Client(endpoint(root), **client_options())
    return client
