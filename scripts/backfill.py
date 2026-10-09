#!/usr/bin/env python3
"""Detach a resumable worker from this chat; use its lock to identify it safely."""

import argparse
import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

repo = Path(__file__).resolve().parent.parent
root = Path(os.environ.get("TUBESCRIBE_DATA", repo / "data")).resolve()
root.mkdir(parents=True, exist_ok=True)
p = argparse.ArgumentParser()
p.add_argument("action", choices=["start", "status", "stop"])
p.add_argument(
    "--diarize", action="store_true", help="Enrich saved ASR instead of importing new videos"
)
args = p.parse_args()
name = "diarization-backfill" if args.diarize else "backfill"
state_file = root / f"{name}.json"
log_file = root / f"{name}.log"
state = json.loads(state_file.read_text()) if state_file.exists() else {}
worker_pid = None
try:
    worker_pid = int((root / "worker.lock").read_text())
    # Test the lock instead of trusting a potentially reused PID in a stale file.
    import fcntl

    with (root / "worker.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            worker_pid = None
        except BlockingIOError:
            pass
except (ValueError, FileNotFoundError):
    pass
if args.action == "status":
    print(
        json.dumps(
            {
                **state,
                "running": worker_pid is not None and state.get("pid") == os.getpgid(worker_pid),
                "archive_worker_active": worker_pid is not None,
                "worker_pid": worker_pid,
                "log": str(log_file),
            },
            indent=2,
        )
    )
elif args.action == "stop":
    if worker_pid is not None:
        if state.get("pid") != os.getpgid(worker_pid):
            sys.exit("Active worker is not owned by this backfill; stop it from its terminal")
        # Stop only this verified supervisor group; the worker preserves stage state.
        os.killpg(state["pid"], signal.SIGTERM)
        print("Stop requested; current stage will be recoverable.")
    else:
        print("No active backfill worker.")
else:
    if worker_pid is not None:
        sys.exit("An archive worker is already running.")
    subprocess.run(
        [str(repo / "scripts/tubescribe"), "--data", str(root), "tunnel", "start"], check=True
    )
    subprocess.run(
        [str(repo / "scripts/tubescribe"), "--data", str(root), "diarization-tunnel", "start"],
        check=True,
    )
    command = [
        str(repo / "scripts/tubescribe"),
        "--data",
        str(root),
        "diarize" if args.diarize else "work",
        "--wait",
    ]
    if sys.platform == "darwin":
        command = ["/usr/bin/caffeinate", "-i", *command]
    with log_file.open("ab") as output:
        proc = subprocess.Popen(
            command,
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
        )
    state = {
        "pid": proc.pid,
        "started_at": datetime.now(UTC).isoformat(),
        "log": str(log_file),
        "command": command,
    }
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, state_file)
    print(json.dumps(state, indent=2))
