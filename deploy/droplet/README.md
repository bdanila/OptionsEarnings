# Deploy to a DigitalOcean Droplet

This directory contains everything needed to run OptionsEarnings on an Ubuntu 24.04 Droplet with the DuckDB file living on a persistent block-storage volume.

## Assumptions

- Fresh Ubuntu 24.04 Droplet (Basic $6/mo works — pandas/scipy install fine on 512MB but 1GB is more comfortable).
- A DO block storage volume of 20GB mounted at `/mnt/volume_fra1_1782985717099` (adjust `VOLUME_MOUNT` env var if yours is elsewhere).
- Root SSH access.

## First-time install

SSH to the droplet as root and run:

```bash
# Option A — one-liner (works even before you clone the repo)
curl -sSL https://raw.githubusercontent.com/bdanila/OptionsEarnings/master/deploy/droplet/install.sh | bash

# Option B — clone first, then run
git clone https://github.com/bdanila/OptionsEarnings.git /opt/options-earnings
bash /opt/options-earnings/deploy/droplet/install.sh
```

The script is idempotent — re-running upgrades in place without destroying the `.env` or the DB file.

### What it does

1. Installs apt packages: `python3 python3-venv git build-essential libxml2-dev libxslt1-dev`.
2. Creates a `options` system user with home in `/home/options`.
3. Ensures `/mnt/volume_fra1_1782985717099/oe_data/` exists, owned by `options`.
4. Clones (or pulls) the repo into `/opt/options-earnings`.
5. Creates a venv, installs `requirements.txt`.
6. Copies `.env.production.example` → `.env` if none exists, patching `DB_PATH` to point at the volume.
7. Renders the systemd unit from the `@APP_DIR@` / `@APP_USER@` / `@PORT@` template and installs it at `/etc/systemd/system/options-earnings.service`.
8. Enables and starts the service.
9. Curls `GET /` to confirm.

### Open the port

App binds to `0.0.0.0:8080`. Firewall must allow it:

- **DO Cloud Firewall** (recommended): add an inbound rule `TCP 8080` from your IP (or Any).
- **Or ufw on the droplet**: `ufw allow 8080/tcp && ufw enable`.

Then access at `http://<droplet_ip>:8080/`.

## Updates (after `git push` on your laptop)

SSH in and run:

```bash
sudo bash /opt/options-earnings/deploy/droplet/update.sh
```

Does `git pull` + `pip install -r requirements.txt` + `systemctl restart options-earnings`.

## Logs

```bash
journalctl -u options-earnings -f          # live tail
journalctl -u options-earnings --since "1 hour ago"
systemctl status options-earnings
```

## First-time data population

The DB starts empty. On first boot:

- Wait ~1 min: at server start, no automatic refresh happens. You need to trigger it once.
- Two ways:
  1. **In the UI**: no button yet — best to run the CLI directly (stop the service, run refresh in foreground, restart).
  2. **CLI** (on the droplet, as the `options` user):
     ```bash
     sudo systemctl stop options-earnings
     sudo -u options /opt/options-earnings/.venv/bin/python -m options_earnings.cli refresh
     sudo systemctl start options-earnings
     ```
     Takes ~2-3 min: ingests S&P 500 + prices + earnings, then automatically fetches option chains for all mcap ≥ 200B names (`FETCH_CHAINS_ON_REFRESH=true`).

After first refresh, the schedulers take over:
- Hourly watchlist refresh (earnings within 14d).
- Daily large-cap chain refresh at 22:00 UTC.
- Hourly IV monitor during NY trading hours for symbols you flag with **Monitor IV** in the UI.

## Backing up the DB

The `.duckdb` file is on the persistent volume, so it survives reboots and Droplet resizes.

For extra safety, `cron` a daily snapshot to DO Spaces or another volume:

```bash
# /etc/cron.daily/options-earnings-backup
DATE=$(date -u +%F)
cp /mnt/volume_fra1_1782985717099/oe_data/options.duckdb \
   /mnt/volume_fra1_1782985717099/oe_data/backups/options-${DATE}.duckdb
find /mnt/volume_fra1_1782985717099/oe_data/backups -name 'options-*.duckdb' -mtime +14 -delete
```

## Overrides

If your volume mount is at a different path, set env vars before running:

```bash
export VOLUME_MOUNT=/mnt/your_volume_here
export PORT=9090        # if you want a different port
bash /opt/options-earnings/deploy/droplet/install.sh
```

Everything else (`APP_USER`, `APP_DIR`, `REPO_URL`, `BRANCH`) is similarly override-able — see the top of `install.sh`.
