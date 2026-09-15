from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from options_earnings.config import Settings
from options_earnings.db import repo
from options_earnings.db.connection import get_conn
from options_earnings.options.job import run_chain_job

log = logging.getLogger(__name__)

# Standard crontab numbers days 0=Sunday..6=Saturday; APScheduler's own
# day_of_week field numbers them 0=Monday..6=Sunday, and
# CronTrigger.from_crontab does NOT translate between the two. So a literal
# "1-5" — which every config file here means as Mon-Fri — silently becomes
# Tue-Sat: Monday never runs and Saturday runs instead. Map the numbers onto
# APScheduler's day names, which are unambiguous in both conventions.
_CRON_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _translate_dow(field: str) -> str:
    """Rewrite numeric day-of-week tokens from standard-crontab numbering to
    APScheduler day names. Names and ``*`` pass through untouched.
    """
    def _tok(tok: str) -> str:
        if tok.isdigit() and 0 <= int(tok) <= 7:
            return _CRON_DOW_NAMES[int(tok)]
        return tok

    out: list[str] = []
    for part in field.split(","):
        step = ""
        if "/" in part:
            part, _, step = part.partition("/")
            step = "/" + step
        if "-" in part and not part.startswith("-"):
            lo, _, hi = part.partition("-")
            out.append(f"{_tok(lo)}-{_tok(hi)}{step}")
        else:
            out.append(f"{_tok(part)}{step}")
    return ",".join(out)


def crontab_trigger(expr: str, timezone: str) -> CronTrigger:
    """``CronTrigger.from_crontab`` with standard-crontab weekday semantics."""
    fields = expr.split()
    if len(fields) == 5:
        fields[4] = _translate_dow(fields[4])
        expr = " ".join(fields)
    return CronTrigger.from_crontab(expr, timezone=timezone)


def _watchlist_symbols(db_path: Path, days: int) -> list[str]:
    cutoff = date.today() + timedelta(days=days)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT symbol FROM symbols "
            "WHERE next_earnings IS NOT NULL "
            "  AND next_earnings >= CURRENT_DATE "
            "  AND next_earnings <= ? "
            "ORDER BY next_earnings ASC",
            [cutoff],
        ).fetchall()
    return [r[0] for r in rows]


def _refresh_watchlist_chains(db_path: Path, window: int, days_to_earnings: int) -> None:
    symbols = _watchlist_symbols(db_path, days_to_earnings)
    if not symbols:
        log.info("scheduler: no symbols with earnings within %d days; nothing to do", days_to_earnings)
        return
    log.info("scheduler: refreshing chains for %d watchlist symbols", len(symbols))
    with get_conn(db_path) as conn:
        job_id = repo.create_job(conn, symbols, window)
    run_chain_job(db_path, job_id, window=window)


def _refresh_large_cap_chains_task(db_path: Path, threshold: float, window: int) -> None:
    from options_earnings.ingest.runner import refresh_large_cap_chains
    log.info("scheduler: refreshing large-cap chains (threshold=%s)", threshold)
    n = refresh_large_cap_chains(db_path, threshold=threshold, window=window)
    log.info("scheduler: large-cap chain refresh done; %d symbols processed", n)


def _daily_candles_task(db_path: Path, batch_size: int, lookback_days: int) -> None:
    from options_earnings.ingest.daily_candles import run_daily_candles_batch
    result = run_daily_candles_batch(
        db_path, batch_size=batch_size, lookback_days=lookback_days
    )
    log.info(
        "scheduler: daily candles tick — symbols=%d rows=%d weekend_skip=%s",
        result.get("symbols", 0), result.get("rows", 0),
        result.get("skipped_weekend", False),
    )


def _iv_monitor_task(
    db_path: Path,
    window: int,
    batch_size: int,
    *,
    tier1_mcap: float | None = None,
    tier1_hours: float = 24.0,
    tier2_hours: float = 48.0,
    workers: int = 1,
) -> None:
    """One tick of the round-robin IV monitor: pull option chains for the
    ``batch_size`` most-overdue IV-monitored symbols, large caps first (see
    ``repo.stale_iv_monitored_symbols``). Skips the earnings history recompute
    (that data is quarterly, not hourly) so each tick makes ~3 yfinance calls
    per symbol instead of ~5.
    """
    from options_earnings.options.job import run_chain_job
    with get_conn(db_path) as conn:
        symbols = repo.stale_iv_monitored_symbols(
            conn, batch_size,
            tier1_mcap=tier1_mcap, tier1_hours=tier1_hours, tier2_hours=tier2_hours,
        )
        if not symbols:
            log.info("scheduler: iv monitor — nothing monitored, skipping tick")
            return
        job_id = repo.create_job(conn, symbols, window_size=window)
    log.info(
        "scheduler: iv monitor tick — job %s for %d symbols (%d workers)",
        job_id, len(symbols), workers,
    )
    produced = run_chain_job(
        db_path, job_id, window=window, skip_earnings_history=True, workers=workers
    )
    # Record the attempt for every symbol, not just the ones that yielded
    # quotes: otherwise a ticker that never returns data keeps its stale
    # timestamp, stays first in the queue and is retried every tick forever.
    with get_conn(db_path) as conn:
        repo.record_iv_attempts(conn, symbols, produced)
        dead = repo.dead_iv_symbols(conn)
    barren = sorted(set(symbols) - set(produced))
    if barren:
        log.warning(
            "scheduler: iv monitor — no data for %d/%d symbols: %s",
            len(barren), len(symbols), ", ".join(barren),
        )
    if dead:
        log.warning(
            "scheduler: %d IV-monitored symbols have failed 5+ times in a row "
            "(likely delisted or no listed options): %s",
            len(dead), ", ".join(f"{s}({n})" for s, n in dead[:15]),
        )


def start_scheduler(settings: Settings) -> BackgroundScheduler | None:
    """Boot APScheduler if enabled in settings. Returns the scheduler (caller should `shutdown()`)."""
    if not settings.scheduler_enabled:
        return None
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _refresh_watchlist_chains,
        trigger=crontab_trigger(settings.scheduler_cron, "UTC"),
        kwargs={
            "db_path": settings.db_path,
            "window": settings.option_chain_window,
            "days_to_earnings": settings.scheduler_watchlist_days_to_earnings,
        },
        id="watchlist_chain_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if settings.large_cap_scheduler_enabled:
        scheduler.add_job(
            _refresh_large_cap_chains_task,
            trigger=crontab_trigger(settings.large_cap_scheduler_cron, "UTC"),
            kwargs={
                "db_path": settings.db_path,
                "threshold": settings.large_cap_chain_threshold,
                "window": settings.option_chain_window,
            },
            id="large_cap_chain_refresh",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if settings.daily_candles_enabled:
        scheduler.add_job(
            _daily_candles_task,
            trigger=crontab_trigger(settings.daily_candles_cron, "UTC"),
            kwargs={
                "db_path": settings.db_path,
                "batch_size": settings.daily_candles_batch_size,
                "lookback_days": settings.daily_candles_lookback_days,
            },
            id="daily_candles",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if settings.iv_monitor_enabled:
        scheduler.add_job(
            _iv_monitor_task,
            trigger=crontab_trigger(settings.iv_monitor_cron, settings.iv_monitor_timezone),
            kwargs={
                "db_path": settings.db_path,
                "window": settings.option_chain_window,
                "batch_size": settings.iv_monitor_batch_size,
                "tier1_mcap": settings.iv_monitor_tier1_mcap,
                "tier1_hours": settings.iv_monitor_tier1_hours,
                "tier2_hours": settings.iv_monitor_tier2_hours,
                "workers": settings.iv_monitor_workers,
            },
            id="iv_monitor_hourly",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    log.info(
        "scheduler: started; watchlist cron='%s' days=%d; large-cap cron='%s' enabled=%s "
        "threshold=%s; iv-monitor cron='%s' tz=%s enabled=%s; daily-candles cron='%s' "
        "batch=%d lookback=%d enabled=%s",
        settings.scheduler_cron, settings.scheduler_watchlist_days_to_earnings,
        settings.large_cap_scheduler_cron, settings.large_cap_scheduler_enabled,
        settings.large_cap_chain_threshold,
        settings.iv_monitor_cron, settings.iv_monitor_timezone, settings.iv_monitor_enabled,
        settings.daily_candles_cron, settings.daily_candles_batch_size,
        settings.daily_candles_lookback_days, settings.daily_candles_enabled,
    )
    return scheduler
