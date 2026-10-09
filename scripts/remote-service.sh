#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# All commands use the existing agent; no private key or host-key changes.
host="${TUBESCRIBE_SSH_HOST:-tubescribe-beast}"
port="${TUBESCRIBE_REMOTE_PORT:-18765}"
ssh_opts=(-o BatchMode=yes)
control="$(pwd)/${TUBESCRIBE_DATA:-data}/main-ssh.sock"
if ssh -S "$control" -O check "$host" >/dev/null 2>&1; then
  ssh_opts+=(-S "$control")
fi
case "${1:-status}" in
  install)
    ssh "${ssh_opts[@]}" "$host" 'mkdir -p ~/.local/share/tubescribe/service ~/.config/systemd/user'
    # Transfer source, never the archive, .venv, credentials, or model weights.
    COPYFILE_DISABLE=1 tar --no-xattrs --exclude='__pycache__' -cf - pyproject.toml uv.toml uv.lock src | ssh "${ssh_opts[@]}" "$host" 'tar -xf - -C ~/.local/share/tubescribe/service'
    ssh "${ssh_opts[@]}" "$host" bash -s -- "$port" <<'REMOTE'
set -euo pipefail
port="$1"
cd ~/.local/share/tubescribe/service
~/.local/bin/uv --config-file uv.toml sync --locked --extra asr --no-dev --python 3.12
mkdir -p ~/.cache/tubescribe/models
# NVIDIA wheels provide isolated CUDA 12 libraries without modifying system CUDA.
libpath="$(.venv/bin/python -c 'import nvidia.cublas.lib, nvidia.cudnn.lib; print(next(iter(nvidia.cublas.lib.__path__))+":"+next(iter(nvidia.cudnn.lib.__path__)))')"
cat > ~/.config/systemd/user/tubescribe-asr.service <<UNIT
[Unit]
Description=TubeScribe Whisper ASR (loopback)
After=network-online.target
[Service]
Type=simple
WorkingDirectory=$HOME/.local/share/tubescribe/service
Environment=LD_LIBRARY_PATH=$libpath
Environment=TUBESCRIBE_MODEL=large-v3-turbo
Environment=TUBESCRIBE_MODEL_REVISION=0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf
Environment=TUBESCRIBE_MODEL_CACHE=$HOME/.cache/tubescribe/models
Environment=CUDA_VISIBLE_DEVICES=0
Environment=TUBESCRIBE_DEVICE=cuda
Environment=TUBESCRIBE_COMPUTE=float16
Environment=HF_HUB_DISABLE_TELEMETRY=1
ExecStart=$HOME/.local/share/tubescribe/service/.venv/bin/python -m tubescribe.server --port $port
Restart=on-failure
RestartSec=30
TimeoutStopSec=120
[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
printf 'Installed service on 127.0.0.1:%s (not started yet)\n' "$port"
REMOTE
    ;;
  start)
    ssh "${ssh_opts[@]}" "$host" bash -s -- "$port" <<'REMOTE'
set -euo pipefail
if systemctl --user is-active --quiet tubescribe-asr; then
  echo 'TubeScribe service already running'
else
  python3 - "$1" <<'PY'
import socket, sys
with socket.socket() as s:
    s.bind(('127.0.0.1', int(sys.argv[1])))
PY
  systemctl --user enable --now tubescribe-asr
fi
systemctl --user --no-pager status tubescribe-asr
REMOTE
    ;;
  stop) ssh "${ssh_opts[@]}" "$host" 'systemctl --user stop tubescribe-asr' ;;
  status) ssh "${ssh_opts[@]}" "$host" 'systemctl --user --no-pager status tubescribe-asr' ;;
  logs) ssh "${ssh_opts[@]}" "$host" 'journalctl --user -u tubescribe-asr -n 60 --no-pager' ;;
  *) echo 'Usage: remote-service.sh {install|start|stop|status|logs}' >&2; exit 2 ;;
esac
