#!/usr/bin/env bash
# Ellalon recording server - installer for Ubuntu / Debian
#
#   curl -fsSL https://raw.githubusercontent.com/5kst5btj87-rgb/ellalon/main/recorder/install-recorder.sh \
#     | sudo bash -s -- --server http://ELLALON-SERVER --code XXXX-XXXX
#
# The code comes from the Ellalon web UI: Storage -> + Add storage -> Recording server.
# Installs Python 3 and FFmpeg, pairs with the server and installs the "ellalon-recorder" systemd
# service: it starts at boot and records from its saved camera list even without the server.
set -euo pipefail
SERVER=""; CODE=""; PORT=8090
SRC=${ELLALON_RECORDER_SRC:-https://raw.githubusercontent.com/5kst5btj87-rgb/ellalon/main/recorder}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) SERVER=$2; shift 2 ;;
    --code) CODE=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done
log() { echo -e "\033[1;32m==>\033[0m $*"; }
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
[[ -n "$SERVER" && -n "$CODE" ]] || { echo "Usage: ... --server http://ELLALON-SERVER --code XXXX-XXXX"; exit 1; }

log "Installing Python 3 and FFmpeg"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 ffmpeg curl ca-certificates >/dev/null

log "Installing the Ellalon recorder"
install -d -m 755 /opt/ellalon-recorder
HERE=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)
if [[ -f "$HERE/ellalon_recorder.py" ]]; then
  install -m 755 "$HERE/ellalon_recorder.py" /opt/ellalon-recorder/ellalon_recorder.py
else
  curl -fsSL "$SRC/ellalon_recorder.py" -o /opt/ellalon-recorder/ellalon_recorder.py
  chmod 755 /opt/ellalon-recorder/ellalon_recorder.py
fi
ln -sf /opt/ellalon-recorder/ellalon_recorder.py /usr/local/bin/ellalon-recorder

systemctl stop ellalon-recorder 2>/dev/null || true
log "Pairing with $SERVER"
python3 /opt/ellalon-recorder/ellalon_recorder.py register --server "$SERVER" --code "$CODE" --port "$PORT"

log "Installing the service (starts at boot)"
cat > /etc/systemd/system/ellalon-recorder.service <<EOF
[Unit]
Description=Ellalon recording server
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/ellalon-recorder/ellalon_recorder.py run
Restart=always
RestartSec=5
TimeoutStopSec=30
Nice=-5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now ellalon-recorder >/dev/null
systemctl restart ellalon-recorder

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "$PORT/tcp" >/dev/null && log "Opened TCP $PORT in the firewall (playback)"
fi

sleep 3
echo
log "Ellalon recorder is installed and running"
cat <<EOF

  It starts automatically at boot and keeps recording even when the Ellalon server is down.
  Cameras: in the Ellalon web UI, Cameras -> Edit -> Recording storage -> this recording server.

  Status   sudo ellalon-recorder status
  Log      sudo journalctl -u ellalon-recorder -f
  Folder   /var/lib/ellalon-recorder/recordings  (unless changed in the Storage tab)
EOF
