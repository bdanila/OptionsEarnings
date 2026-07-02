#!/usr/bin/env bash
# Pull latest master + reinstall deps + restart. Run on the droplet as root:
#   sudo bash /opt/options-earnings/deploy/droplet/update.sh

set -euo pipefail

: "${APP_USER:=options}"
: "${APP_DIR:=/opt/options-earnings}"

if [[ $EUID -ne 0 ]]; then
    echo "Run as root." >&2
    exit 1
fi

echo "== git pull"
sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only

echo "== pip install"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && '$APP_DIR/.venv/bin/pip' install -r '$APP_DIR/requirements.txt'"

echo "== systemd restart"
systemctl restart options-earnings
sleep 2
systemctl --no-pager status options-earnings | head -8
