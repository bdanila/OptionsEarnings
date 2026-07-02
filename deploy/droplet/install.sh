#!/usr/bin/env bash
# One-shot bootstrap for an Ubuntu 24.04 DO Droplet.
# Idempotent — safe to re-run.
#
# Usage (run as root on the droplet):
#   curl -sSL https://raw.githubusercontent.com/bdanila/OptionsEarnings/master/deploy/droplet/install.sh | sudo bash
# ...or after `git clone`:
#   sudo bash deploy/droplet/install.sh

set -euo pipefail

: "${REPO_URL:=https://github.com/bdanila/OptionsEarnings.git}"
: "${BRANCH:=master}"
: "${APP_USER:=options}"
: "${APP_DIR:=/opt/options-earnings}"
: "${VOLUME_MOUNT:=/mnt/volume_fra1_1782985717099}"
: "${DATA_DIR:=${VOLUME_MOUNT}/oe_data}"
: "${PORT:=8080}"

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (use sudo)." >&2
    exit 1
fi

if [[ ! -d "$VOLUME_MOUNT" ]]; then
    echo "Volume mount $VOLUME_MOUNT does not exist. Mount the DO volume first." >&2
    exit 1
fi

echo "== 1/8 apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    git build-essential \
    libxml2-dev libxslt1-dev \
    ca-certificates curl

echo "== 2/8 app user"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

echo "== 3/8 data dir on volume"
mkdir -p "$DATA_DIR"
chown -R "$APP_USER":"$APP_USER" "$DATA_DIR"

echo "== 4/8 clone/update repo"
if [[ ! -d "$APP_DIR/.git" ]]; then
    mkdir -p "$(dirname "$APP_DIR")"
    git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
# Always take ownership — the repo may have been cloned by root before
# install.sh was invoked, which trips Git's safe.directory check when
# subsequent commands run as $APP_USER.
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
sudo -u "$APP_USER" git -C "$APP_DIR" fetch origin
sudo -u "$APP_USER" git -C "$APP_DIR" checkout "$BRANCH"
sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only origin "$BRANCH"

echo "== 5/8 venv + deps"
if [[ ! -d "$APP_DIR/.venv" ]]; then
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
# cd into $APP_DIR so `-e .` inside requirements.txt resolves to the app.
sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && '$APP_DIR/.venv/bin/pip' install -r '$APP_DIR/requirements.txt'"

echo "== 6/8 .env"
ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    install -o "$APP_USER" -g "$APP_USER" -m 0640 \
        "$APP_DIR/deploy/droplet/.env.production.example" "$ENV_FILE"
    # Fill in the actual volume-backed DB path
    sed -i "s|^DB_PATH=.*|DB_PATH=${DATA_DIR}/options.duckdb|" "$ENV_FILE"
    echo "   wrote $ENV_FILE (DB_PATH=$DATA_DIR/options.duckdb)"
else
    echo "   $ENV_FILE already exists; leaving untouched"
fi

echo "== 7/8 systemd unit"
UNIT_SRC="$APP_DIR/deploy/droplet/options-earnings.service"
UNIT_DST="/etc/systemd/system/options-earnings.service"
# Substitute PORT + APP_DIR + APP_USER into the unit template
sed \
    -e "s|@APP_DIR@|$APP_DIR|g" \
    -e "s|@APP_USER@|$APP_USER|g" \
    -e "s|@PORT@|$PORT|g" \
    "$UNIT_SRC" >"$UNIT_DST"
systemctl daemon-reload
systemctl enable options-earnings
systemctl restart options-earnings

echo "== 8/8 verify"
sleep 2
systemctl --no-pager status options-earnings | head -12
echo
echo "Health:"
curl -fsS "http://127.0.0.1:${PORT}/" -o /dev/null -w "  GET / -> %{http_code}\n" || echo "  (not up yet — check journalctl -u options-earnings -f)"

echo
echo "Done. Access at http://<droplet_ip>:${PORT}/"
echo "Follow-up: open port ${PORT} in the DigitalOcean Cloud Firewall (or run: ufw allow ${PORT}/tcp)"
