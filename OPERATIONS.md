# OptionsEarnings — Operations Manual

Everything needed to run, feed, ship and fix this system. `README.md` explains what the
app *is*; this file is about operating it.

---

## 1. The two installations

| | **Laptop (dev)** | **Droplet (prod)** |
|---|---|---|
| Location | `C:\Users\Lenovo-BDA-PC\Dropbox\Trading\OptionsEarnings` | `/opt/options-earnings` |
| Runs as | you, in a terminal | systemd service `options-earnings`, user `options` |
| Python | `.venv\Scripts\python.exe` | `/opt/options-earnings/.venv/bin/python` |
| URL | http://127.0.0.1:8001 | `http://<droplet_ip>:8080/` |
| Database | `data/options.duckdb` (inside Dropbox) | `/mnt/volume_fra1_1782985717099/oe_data/options.duckdb` (persistent volume) |
| Config | `.env` | `/opt/options-earnings/.env` |
| Schedulers | **on** | **on** |

The two databases are completely separate. Refreshing data on the laptop does nothing for
prod, and vice versa.

---

## 2. Start and stop (laptop)

### Start

```bash
cd "C:\Users\Lenovo-BDA-PC\Dropbox\Trading\OptionsEarnings"
.venv/Scripts/python.exe -m options_earnings.cli serve --port 8001
```

Then open **http://127.0.0.1:8001**.

**Use `--port 8001`, not the default 8000.** Your `.env` says `WEB_PORT=8000`, but port 8000
is occupied by the `run_dashboard.py` from the IBAPITrade project. Starting on 8000 fails
with "address already in use". If you'd rather free up 8000, stop that dashboard first
(`Stop-Process -Id <its parent PID>`), but 8001 is less disruptive.

Add `--reload` while editing code — uvicorn restarts on every save.

### Stop

**Ctrl+C** in the terminal running it. Let it exit cleanly — see §8 on why that matters.

### Check whether it's already running

```powershell
Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8000,8001,8080 |
  ForEach-Object { [pscustomobject]@{Port=$_.LocalPort; PID=$_.OwningProcess;
    Name=(Get-Process -Id $_.OwningProcess -EA SilentlyContinue).Name} }
```

### Important: the local server also runs the schedulers

`serve` loads the same production app object as the droplet does, and your local `.env` has
all four schedulers enabled. So a running local server is **continuously hitting yfinance and
writing to the local DB** on the schedule in §6 — not just serving pages. If you only want
to look at the UI without background activity, set `SCHEDULER_ENABLED=false`,
`LARGE_CAP_SCHEDULER_ENABLED=false`, `IV_MONITOR_ENABLED=false`,
`DAILY_CANDLES_ENABLED=false` in `.env` first.

---

## 3. The web UI

### Stocks page (`/`) — the main screen

**Status pills at the top**

- *Daily candles: N / total up to date* — OHLC ingest coverage.
- *Earnings dates: N / total upcoming · N past due · N missing* — with the update buttons.

**Filters** — search by symbol/company, min market cap (accepts `10B`, `500M`), earnings
date range, IV-monitored yes/no, 3-month range %. Every column header sorts.

**Row actions** — tick the checkboxes, then:

| Button | What it does |
|---|---|
| **Follow Option Chain** | Creates a chain job for the ticked symbols and opens the job page. One-off snapshot. |
| **Monitor IV** | Flags the ticked symbols as IV-monitored *and* fetches their chains now. Monitored symbols are then re-polled automatically all day (§6). |
| **Stop Monitor** | Clears the flag. Existing history is kept. |
| **Update earnings dates** | Re-fetches `next_earnings` for symbols whose date is past due or missing. This is the fix for stale earnings dates. |
| **All** (next to it) | Same, but for every symbol — catches rescheduled future dates too. Slower. |
| **Refresh data** (nav bar) | Full rebuild: S&P 500 constituent list, prices, market caps, earnings dates, then option chains for all mcap ≥ 200B names. Takes 2–3 min. |

All of these run in the background and immediately redirect you back. There's no progress
bar — reload the page and watch the pill counters move.

### Other pages

- `/jobs/{id}` — chain job result; self-polls every 2 s until done, then shows the ATM summary.
- `/jobs/{id}/{symbol}` — the full ±N strike grid for that snapshot.
- `/symbols/{symbol}/candles` — daily OHLC chart.
- `/symbols/{symbol}/iv-history` — IV time series (`.json` for the raw data).

---

## 4. Refreshing data from the CLI

Same operations as the buttons, but you see the log output and it doesn't depend on the
server. Run from the project directory. **Stop the server first** if it's running — two
processes writing the same DuckDB file will fight.

```bash
# Full rebuild: constituents + prices + mcap + earnings, then large-cap chains
.venv/Scripts/python.exe -m options_earnings.cli refresh

#   --limit 20     only the first 20 symbols (smoke test)
#   --workers 8    parallel network workers
#   --no-chains    skip the option-chain step at the end

# Earnings dates only — this is the stale-dates fix
.venv/Scripts/python.exe -m options_earnings.cli refresh-earnings
#   --scope stale     missing OR already past   (default)
#   --scope missing   only NULL dates
#   --scope all       every symbol, catches reschedules
#   --workers 4 --retries 2

# Backfill any NULL price / market cap / earnings
.venv/Scripts/python.exe -m options_earnings.cli refresh-missing

# One-off: recompute iv_rank_history from every stored snapshot (idempotent)
.venv/Scripts/python.exe -m options_earnings.cli backfill-iv-rank
```

`refresh-earnings` and `refresh-missing` deliberately use low concurrency plus
retry/backoff to avoid yfinance rate limiting, and they **never overwrite a good value with
NULL** when a fetch fails.

### On a cold/empty database

Order matters:

1. `refresh` — populates `symbols` (this must come first; everything else keys off it).
2. Tick the names you care about → **Monitor IV** in the UI.
3. Let the schedulers fill in candles and IV history over the next few hours.

---

## 5. Shipping a change

```bash
# 1. Test — all tests must pass
.venv/Scripts/python.exe -m pytest -q

# 2. Review what you're about to commit
git status --short
git diff

# 3. Commit
git add -A
git commit -m "Short imperative subject

Body: what changed and why."

# 4. Push
git push origin master
```

`master` is the deploy branch. There's no CI, no PR flow, no staging — pushing is the last
checkpoint before prod, so run the tests.

---

## 6. Deploying to prod

### Deploy

SSH to the droplet and run:

```bash
sudo bash /opt/options-earnings/deploy/droplet/update.sh
```

That's `git pull --ff-only` + `pip install -r requirements.txt` + `systemctl restart
options-earnings`, then prints the service status. Takes well under a minute when there are
no new dependencies.

The DB is on a separate mounted volume, so a deploy never touches your data.

**Note on DigitalOcean App Platform:** `.do/app.yaml` configures an App Platform service with
`deploy_on_push: true` on `master`. If that app is still live alongside the droplet, every
push auto-deploys there too. I have not verified whether it's still running — worth checking
once in your DO dashboard and deleting it if it's a leftover, since App Platform wipes its
filesystem (and therefore its DuckDB file) on every deploy.

### Service control

```bash
sudo systemctl status options-earnings
sudo systemctl restart options-earnings
sudo systemctl stop options-earnings
sudo systemctl start options-earnings
```

### Logs

```bash
journalctl -u options-earnings -f                    # live tail
journalctl -u options-earnings --since "1 hour ago"
journalctl -u options-earnings -p err                # errors only
```

### Running CLI commands on prod

As the `options` user, with the service stopped so you don't get two writers:

```bash
sudo systemctl stop options-earnings
sudo -u options /opt/options-earnings/.venv/bin/python -m options_earnings.cli refresh-earnings
sudo systemctl start options-earnings
```

### Config changes on prod

Edit `/opt/options-earnings/.env`, then `sudo systemctl restart options-earnings`. The env
file is read by systemd at start, so a restart is required — a `git pull` alone won't pick it
up. `.env` is not in git and survives updates.

---

## 7. What runs automatically

Both installations run these (APScheduler, in-process). Defaults from
`deploy/droplet/.env.production.example`:

| Job | Schedule | What it does |
|---|---|---|
| Watchlist chain refresh | hourly, Mon–Fri (UTC) | Pulls chains for symbols reporting within 14 days |
| Large-cap chain refresh | 22:00 UTC, Mon–Fri | Pulls chains for every symbol with mcap ≥ 200B |
| IV monitor | every 10 min, Mon–Fri, NY time | Round-robin over IV-monitored symbols, 15 per tick, most-stale first |
| Daily candles | every 10 min | Backfills OHLC in batches of 10 symbols, 90-day lookback; skips weekends |

Nothing here refreshes **earnings dates** — that's why they go stale and why the
**Update earnings dates** button exists. Run it every few weeks, or whenever the *past due*
counter climbs.

---

## 8. The database

DuckDB, single file. Tables: `symbols`, `option_chain_jobs`, `option_quotes`,
`earnings_moves`, `earnings_ohlc`, `iv_rank_history`, `iv_alerts_dismissed`.

### Never run two writers at once

One server, or one CLI command — not both. This is the single most common way to wedge things.

### The `.wal` file

Next to `options.duckdb` you'll see `options.duckdb.wal`. DuckDB checkpoints it into the main
file **on clean shutdown**. If a process is killed instead, the WAL survives and gets replayed
on the next open — and replay of a large WAL can take minutes, which looks exactly like a hang.

Your local copy currently has a ~68 MB WAL from a process that died on 21 July. The first
clean start-and-stop cycle will collapse it.

So: always **Ctrl+C** the local server, always `systemctl stop` on prod.

### Inspect it

```bash
.venv/Scripts/python.exe -c "import duckdb; c=duckdb.connect('data/options.duckdb', read_only=True); print(c.execute('''
  SELECT count(*) total,
         count(*) FILTER (WHERE next_earnings IS NULL) missing,
         count(*) FILTER (WHERE next_earnings < CURRENT_DATE) past_due
  FROM symbols''').fetchone())"
```

Use `read_only=True` for inspection so you can't corrupt anything — but note it still blocks
if another process holds the file, and it still has to replay a dirty WAL.

### Backups

Prod: the volume survives reboots and resizes. For real safety, cron a daily copy —
`deploy/droplet/README.md` has a ready-made snippet.

Local: it's in Dropbox, so it's versioned by Dropbox — but a 160 MB binary that changes
constantly is not a great fit for that. Don't rely on it.

---

## 9. Configuration (`.env`)

| Key | Local | Prod | Meaning |
|---|---|---|---|
| `DB_PATH` | `data/options.duckdb` | `/mnt/volume_.../oe_data/options.duckdb` | DuckDB file |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1` / `8000` | `0.0.0.0` / `8080` | Bind address |
| `OPTION_CHAIN_WINDOW` | `20` | `20` | Strikes fetched around ATM (~10 each side) |
| `DEFAULT_CP` | `C` | `C` | Default side for IV history |
| `RISK_FREE_RATE_FALLBACK` | `0.05` | `0.05` | Used when the `^IRX` fetch fails |
| `PAGE_SIZE` | `50` | `50` | Stock-list rows per page |
| `FETCH_CHAINS_ON_REFRESH` | `true` | `true` | Pull large-cap chains at the end of `refresh` |
| `LARGE_CAP_CHAIN_THRESHOLD` | `200000000000` | same | "Large cap" cutoff, in dollars |
| `SCHEDULER_*` | enabled | enabled | Watchlist chain refresh |
| `LARGE_CAP_SCHEDULER_*` | enabled | enabled | Nightly large-cap chains |
| `IV_MONITOR_*` | enabled | enabled | Intraday IV polling; `BATCH_SIZE` symbols per tick |
| `DAILY_CANDLES_*` | enabled | enabled | OHLC backfill; `BATCH_SIZE`, `LOOKBACK_DAYS` |
| `IV_RANK_ALERT_*` | `10.0` / `10` | same | IV-rank drop threshold and lookback |

`.env` is gitignored on both machines. `.env.example` and
`deploy/droplet/.env.production.example` are the templates.

---

## 10. Troubleshooting

**"Address already in use" on start**
Port 8000 is IBAPITrade's dashboard. Use `--port 8001`.

**The server hangs on startup / a CLI command hangs at the DB open**
Dirty WAL replay (§8). Wait it out — a 68 MB WAL takes minutes. Then stop cleanly so it
collapses. If it never finishes, check nothing else holds the file:

```powershell
try { $s=[System.IO.File]::Open("...\data\options.duckdb",'Open','ReadWrite','None')
      "no other process holds it"; $s.Close() } catch { "LOCKED: $($_.Exception.Message)" }
```

**Earnings dates are in the past**
Nothing refreshes them automatically. Click **Update earnings dates**, or run
`refresh-earnings --scope stale`. If a specific symbol stays wrong, try `--scope all` — its
date may have moved forward rather than gone stale.

**Prices, market caps or dates come back empty after a refresh**
Almost always yfinance rate limiting. Wait, then run `refresh-missing`, which retries with
low concurrency. Failed fetches are never written, so nothing good was destroyed.

**A chain job is stuck on `pending`/`running`**
Background tasks die with the process. If you stopped the server mid-job, the row stays
stuck — just create a new job. Check `journalctl` / terminal output for the real error.

**Prod is down after a deploy**
`journalctl -u options-earnings -n 100`. Usual causes: a bad `.env` edit, or a new dependency
that didn't install. Roll back with `git -C /opt/options-earnings reset --hard <prev-sha>`
followed by a restart.

**Nothing is updating on prod but the site is up**
Check the schedulers actually started: `journalctl -u options-earnings | grep "scheduler: started"`.
That line prints every cron expression at boot.
