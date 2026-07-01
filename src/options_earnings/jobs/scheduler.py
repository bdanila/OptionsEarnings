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


def _iv_monitor_task(db_path: Path, window: int) -> None:
    """Hourly during NY market hours: pull current IV snapshot for every
    symbol with iv_monitored=TRUE. Reuses the chain-job machinery so the
    existing ATM IV column auto-updates from the latest snapshot.
    """
    from options_earnings.options.job import run_chain_job
    with get_conn(db_path) as conn:
        symbols = repo.monitored_symbols(conn)
        if not symbols:
            log.info("scheduler: iv monitor — nothing monitored, skipping tick")
            return
        job_id = repo.create_job(conn, symbols, window_size=window)
    log.info("scheduler: iv monitor tick — job %s for %d symbols", job_id, len(symbols))
    run_chain_job(db_path, job_id, window=window)


def start_scheduler(settings: Settings) -> BackgroundScheduler | None:
    """Boot APScheduler if enabled in settings. Returns the scheduler (caller should `shutdown()`)."""
    if not settings.scheduler_enabled:
        return None
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _refresh_watchlist_chains,
        trigger=CronTrigger.from_crontab(settings.scheduler_cron, timezone="UTC"),
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
            trigger=CronTrigger.from_crontab(settings.large_cap_scheduler_cron, timezone="UTC"),
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
    if settings.iv_monitor_enabled:
        scheduler.add_job(
            _iv_monitor_task,
            trigger=CronTrigger.from_crontab(
                settings.iv_monitor_cron, timezone=settings.iv_monitor_timezone
            ),
            kwargs={
                "db_path": settings.db_path,
                "window": settings.option_chain_window,
            },
            id="iv_monitor_hourly",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    log.info(
        "scheduler: started; watchlist cron='%s' days=%d; large-cap cron='%s' enabled=%s "
        "threshold=%s; iv-monitor cron='%s' tz=%s enabled=%s",
        settings.scheduler_cron, settings.scheduler_watchlist_days_to_earnings,
        settings.large_cap_scheduler_cron, settings.large_cap_scheduler_enabled,
        settings.large_cap_chain_threshold,
        settings.iv_monitor_cron, settings.iv_monitor_timezone, settings.iv_monitor_enabled,
    )
    return scheduler
