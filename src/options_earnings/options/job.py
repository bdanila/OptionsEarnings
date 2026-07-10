from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from pathlib import Path
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
) -> None:
    with get_conn(db_path) as conn:
        job = get_job(conn, job_id)
        if job is None:
            raise ValueError(f"job {job_id} not found")
        update_job_status(conn, job_id, "running")
        try:
            rate = _resolve_risk_free_rate(risk_free_rate)
            snapshot_ts = datetime.now(timezone.utc).replace(tzinfo=None)
            errors: list[str] = []
            successes = 0
            for symbol in job.symbols:
                if target_expiry is not None:
                    per_symbol_target = target_expiry
                else:
                    sym_row = get_symbol(conn, symbol)
                    per_symbol_target = sym_row.next_earnings if sym_row else None

                try:
                    quotes = fetch_chain_slice(
                        symbol,
                        window=window,
                        target_expiry=per_symbol_target,
                        risk_free_rate=rate,
                        snapshot_ts=snapshot_ts,
                        job_id=job_id,
                    )
                    insert_quotes(conn, quotes)
                    successes += 1
                except Exception as exc:  # noqa: BLE001
                    logger.exception("chain fetch failed for %s", symbol)
                    errors.append(f"{symbol}: {exc}")

                if skip_earnings_history:
                    continue
                try:
                    moves, ohlc_rows = compute_recent_earnings_data(symbol, n=8)
                    for move in moves:
                        upsert_earnings_move(conn, move)
                    upsert_ohlc(conn, ohlc_rows)
                except Exception:  # noqa: BLE001
                    logger.exception("earnings move computation failed for %s", symbol)

            if successes == 0 and errors:
                update_job_status(conn, job_id, "error", error=", ".join(errors))
            else:
                update_job_status(conn, job_id, "done", error=", ".join(errors) if errors else None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("run_chain_job failed")
            update_job_status(conn, job_id, "error", error=str(exc))
            raise
