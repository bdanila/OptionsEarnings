from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import yfinance as yf

from options_earnings.config import get_settings
from options_earnings.db.connection import get_conn
from options_earnings.db.repo import (
    get_job,
    get_symbol,
    insert_quotes,
    update_job_status,
    upsert_earnings_move,
    upsert_ohlc,
)
from options_earnings.ingest.earnings_history import compute_recent_earnings_data
from options_earnings.options.chain import fetch_chain_slice

logger = logging.getLogger(__name__)


def _resolve_risk_free_rate(explicit: float | None) -> float:
    if explicit is not None:
        return explicit
    settings = get_settings()
    try:
        irx = yf.Ticker("^IRX")
        hist = irx.history(period="5d")
        if hist is None or hist.empty:
            raise RuntimeError("no IRX history")
        last = float(hist["Close"].dropna().iloc[-1])
        if math.isnan(last):
            raise RuntimeError("nan IRX")
        return last / 100.0
    except Exception as exc:  # noqa: BLE001
        logger.warning("falling back to risk_free_rate_fallback: %s", exc)
        return settings.risk_free_rate_fallback


def run_chain_job(
    db_path: str | Path,
    job_id: UUID,
    *,
    window: int,
    target_expiry: date | None = None,
    risk_free_rate: float | None = None,
    skip_earnings_history: bool = False,
    workers: int = 1,
) -> list[str]:
    with get_conn(db_path) as conn:
        job = get_job(conn, job_id)
        if job is None:
            raise ValueError(f"job {job_id} not found")
        update_job_status(conn, job_id, "running")
        try:
            rate = _resolve_risk_free_rate(risk_free_rate)
            snapshot_ts = datetime.now(timezone.utc).replace(tzinfo=None)
            errors: list[str] = []
            produced: list[str] = []
            successes = 0

            # Resolve per-symbol expiries up front: these are DB reads, and
            # the fetch below runs on worker threads that must not touch conn.
            targets: dict[str, date | None] = {}
            for symbol in job.symbols:
                if target_expiry is not None:
                    targets[symbol] = target_expiry
                else:
                    sym_row = get_symbol(conn, symbol)
                    targets[symbol] = sym_row.next_earnings if sym_row else None

            def _work(symbol: str) -> tuple[str, Any, Any]:
                """Network-only half, safe to run in parallel. Returns
                (symbol, quotes_or_exception, earnings_or_None)."""
                try:
                    quotes: Any = fetch_chain_slice(
                        symbol,
                        window=window,
                        target_expiry=targets[symbol],
                        risk_free_rate=rate,
                        snapshot_ts=snapshot_ts,
                        job_id=job_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("chain fetch failed for %s", symbol)
                    quotes = exc
                earnings: Any = None
                if not skip_earnings_history:
                    try:
                        earnings = compute_recent_earnings_data(symbol, n=8)
                    except Exception:  # noqa: BLE001
                        logger.exception("earnings move computation failed for %s", symbol)
                return symbol, quotes, earnings

            n_workers = max(1, min(workers, len(job.symbols)))
            if n_workers == 1:
                results = [_work(s) for s in job.symbols]
            else:
                results = []
                with ThreadPoolExecutor(max_workers=n_workers) as ex:
                    futures = [ex.submit(_work, s) for s in job.symbols]
                    for fut in as_completed(futures):
                        try:
                            results.append(fut.result())
                        except Exception:  # noqa: BLE001
                            logger.exception("chain worker crashed")

            # All DB writes happen here, on the one thread that owns conn.
            for symbol, quotes, earnings in results:
                if isinstance(quotes, Exception):
                    errors.append(f"{symbol}: {quotes}")
                else:
                    try:
                        insert_quotes(conn, quotes)
                        successes += 1
                        if quotes:
                            produced.append(symbol)
                        else:
                            # Not an error, but nothing was stored: a delisted
                            # ticker or one with no listed options. The caller
                            # needs to know so it can stop re-picking it.
                            logger.info("no quotes stored for %s", symbol)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("insert_quotes failed for %s", symbol)
                        errors.append(f"{symbol}: {exc}")
                if earnings is not None:
                    moves, ohlc_rows = earnings
                    try:
                        for move in moves:
                            upsert_earnings_move(conn, move)
                        upsert_ohlc(conn, ohlc_rows)
                    except Exception:  # noqa: BLE001
                        logger.exception("earnings write failed for %s", symbol)
            errors.sort()

            if successes == 0 and errors:
                update_job_status(conn, job_id, "error", error=", ".join(errors))
            else:
                update_job_status(conn, job_id, "done", error=", ".join(errors) if errors else None)
            return produced
        except Exception as exc:  # noqa: BLE001
            logger.exception("run_chain_job failed")
            update_job_status(conn, job_id, "error", error=str(exc))
            raise
