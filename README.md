# OptionsEarnings

A local Python workbench for spotting earnings-driven options setups in the S&P 500.

- Pulls S&P 500 constituents + last price + market cap + next earnings date into DuckDB
- Web UI: paginated, sortable stock table with row-level checkboxes
- "Follow Option Chain" → background job pulls Yahoo option chains around ATM
- Per-symbol drill-down: ±N strike grid (calls & puts) with bid/ask/IV
- IV history chart (Chart.js) per symbol — rolling-ATM or fixed-strike
- Optional scheduler (APScheduler) auto-refreshes chains for names with upcoming earnings

## Quickstart (Windows / PowerShell or bash)

```bash
# 1. Create venv and install
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"

# 2. Configure (optional — defaults work)
cp .env.example .env

# 3. Populate the symbols table from Wikipedia + yfinance
.venv/Scripts/python.exe -m options_earnings.cli refresh
# or, for a quick smoke test, just the first 20 names:
.venv/Scripts/python.exe -m options_earnings.cli refresh --limit 20

# 4. Run the web app
.venv/Scripts/python.exe -m options_earnings.cli serve
# open http://127.0.0.1:8000
```

## Usage flow

1. Open `/`. Sort by **Earnings Date** asc to see soonest reports.
2. Tick rows → click **Follow Option Chain**.
3. The job page (`/jobs/{id}`) polls every 2 s; once `done`, you get an ATM summary table per symbol (Call, Put, computed IV, expiry).
4. Click a symbol → full ±N strike grid for that job's snapshot.
5. From there, click **IV History** → time series of IV at the strike closest to the underlying. Toggle Call/Put or switch to a fixed strike.

## Configuration (`.env`)

| Key | Default | Meaning |
|---|---|---|
| `DB_PATH` | `data/options.duckdb` | DuckDB file location |
| `OPTION_CHAIN_WINDOW` | `20` | Strikes around ATM to fetch (≈10 above + 10 below) |
| `DEFAULT_CP` | `C` | Default option side for IV history (`C` or `P`) |
| `RISK_FREE_RATE_FALLBACK` | `0.05` | Used when `^IRX` fetch fails |
| `PAGE_SIZE` | `50` | Stock-list rows per page |
| `SCHEDULER_ENABLED` | `false` | Turn on APScheduler |
| `SCHEDULER_CRON` | `0 */1 * * 1-5` | Cron expression (UTC) |
| `SCHEDULER_WATCHLIST_DAYS_TO_EARNINGS` | `14` | Auto-refresh names with earnings within N days |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1` / `8000` | Bind address |

## Architecture

```
ingest/        Wikipedia scrape + yfinance prices/earnings → symbols table
options/       Yahoo chain fetcher, Black-Scholes IV solver, history queries
db/            DuckDB schema + repo (only layer that writes SQL)
web/           FastAPI + Jinja2 + HTMX + Chart.js
jobs/          APScheduler (opt-in) for auto chain refresh
cli.py         `refresh` and `serve` subcommands
```

The IV history chart density depends on snapshot frequency. With manual runs you'll get a sparse line; enable the scheduler for an hourly update (during US market hours by default).

## Tests

```bash
.venv/Scripts/python.exe -m pytest
```

All tests run offline (no live yfinance/network calls). Network code is exercised via mocked fetchers.

## Deploy to DigitalOcean App Platform

Detection files are checked in at the repo root:

- [requirements.txt](requirements.txt) — flat dependency list (plus `-e .` to install the package itself).
- [Procfile](Procfile) — `web: uvicorn options_earnings.web.app:app --host 0.0.0.0 --port ${PORT:-8080}`.
- [runtime.txt](runtime.txt) — pins Python 3.12.
- [.do/app.yaml](.do/app.yaml) — App Spec with sensible env-var defaults (scheduler on, all crons set).

### Steps

1. Push this repo to GitHub (`git push`).
2. In DigitalOcean → Apps → Create App → GitHub → pick `bdanila/OptionsEarnings`, branch `master`.
3. DO detects a Python service. Accept the default (Basic XXS `$5/mo` is enough).
4. Optionally, click *Edit Plan → Import from `.do/app.yaml`* to load env vars.
5. Deploy. First build takes ~3-5 min (installs pandas/scipy/etc.).

### Critical caveats

- **Ephemeral filesystem.** DO App Platform wipes `/workspace` on every deploy. The DuckDB file (`data/options.duckdb`) is lost each time. On first boot the DB is empty; you need to trigger `POST /refresh` (or wait for scheduler ticks) to repopulate. For persistent data use a **Droplet + volume** instead, or migrate the storage layer to a managed database (Postgres / DO Spaces sync).
- **Yahoo rate limits.** yfinance is more aggressively throttled from datacenter IPs (DO/AWS/GCP) than from residential IPs. The scheduler's hourly IV tick may see partial data. Retries and low concurrency in `refresh-missing` help.
- **Timezone.** Cron jobs use UTC by default; `IV_MONITOR_TIMEZONE=America/New_York` in `.do/app.yaml` keeps the IV monitor aligned with NY market hours regardless of what timezone the container thinks it's in.
- **Health check.** DO probes `GET /`; the stocks page returns 200 even with an empty DB, so health passes immediately.
- **Cost.** Basic XXS ($5/mo) is fine for the web + scheduler. If chain refresh (~500 symbols) is too slow on that tier, bump to Basic S.

## Notes

- The `symbols` table is keyed by Yahoo-style ticker (`BRK-B`, not `BRK.B`) — the Wikipedia loader normalizes this on the way in.
- `option_quotes` is keyed `(job_id, symbol, expiry, strike, cp)` — each chain pull is a snapshot, time-stamped on `snapshot_ts`.
- Risk-free rate is fetched once per job from `^IRX` (13-week T-bill), divided by 100.
- Implied vol is solved via `scipy.optimize.brentq` on Black-Scholes; `iv_yahoo` is also stored for comparison.
- DuckDB allows multiple connections within a single process, so the FastAPI long-lived connection coexists fine with the scheduler's per-task connections.
