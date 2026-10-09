#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
host="${TUBESCRIBE_SSH_HOST:-tubescribe-beast}"
port="${TUBESCRIBE_DIARIZATION_REMOTE_PORT:-18767}"
archive_root="${TUBESCRIBE_DATA:-data}"
[[ "$archive_root" = /* ]] || archive_root="$(pwd)/$archive_root"
control="$archive_root/main-ssh.sock"
ssh_opts=(-o BatchMode=yes -S "$control")
case "${1:-status}" in
  install)
    ssh "${ssh_opts[@]}" "$host" 'mkdir -p ~/.local/share/tubescribe/diarization-service ~/.config/systemd/user'
    COPYFILE_DISABLE=1 tar --no-xattrs --exclude='__pycache__' -cf - pyproject.toml uv.toml uv.lock src | ssh "${ssh_opts[@]}" "$host" 'tar -xf - -C ~/.local/share/tubescribe/diarization-service'
    ssh "${ssh_opts[@]}" "$host" bash -s -- "$port" <<'REMOTE'
set -euo pipefail
port="$1"
cd ~/.local/share/tubescribe/diarization-service
~/.local/bin/uv --config-file uv.toml sync --locked --extra diarization --no-dev --python 3.12
cat > ~/.config/systemd/user/tubescribe-diarization.service <<UNIT
[Unit]
Description=TubeScribe Nemotron speaker diarization (loopback RTX 4090)
After=network-online.target
[Service]
Type=simple
WorkingDirectory=$HOME/.local/share/tubescribe/diarization-service
Environment=CUDA_VISIBLE_DEVICES=0
Environment=HF_HUB_DISABLE_TELEMETRY=1
ExecStart=$HOME/.local/share/tubescribe/diarization-service/.venv/bin/python -m tubescribe.diarization_server --port $port
Restart=on-failure
RestartSec=30
TimeoutStopSec=120
[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
echo "Installed diarization service on 127.0.0.1:$port; not restarted"
REMOTE
    ;;
  start)
    ssh "${ssh_opts[@]}" "$host" bash -s -- "$port" <<'REMOTE'
set -euo pipefail
if ! systemctl --user is-active --quiet tubescribe-diarization; then
  python3 - "$1" <<'PY'
import socket, sys
with socket.socket() as probe:
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    probe.bind(('127.0.0.1',int(sys.argv[1])))
PY
  systemctl --user enable --now tubescribe-diarization
fi
systemctl --user --no-pager status tubescribe-diarization
REMOTE
    ;;
  stop) ssh "${ssh_opts[@]}" "$host" 'systemctl --user stop tubescribe-diarization' ;;
  status) ssh "${ssh_opts[@]}" "$host" 'systemctl --user --no-pager status tubescribe-diarization' ;;
  logs) ssh "${ssh_opts[@]}" "$host" 'journalctl --user -u tubescribe-diarization -n 40 --no-pager' ;;
  *) echo 'Usage: remote-diarization.sh {install|start|stop|status|logs}' >&2; exit 2 ;;
esac
