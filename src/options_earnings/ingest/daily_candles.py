from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb

from options_earnings.db.repo import OHLCRow

logger = logging.getLogger(__name__)


def fetch_daily_candles(
    symbol: str,
    *,
    lookback_days: int = 90,
    since: date | None = None,
) -> list[OHLCRow]:
    """Fetch daily OHLC from yfinance for one symbol.

    - If ``since`` is None: fetch the last ``lookback_days`` calendar days.
    - Else: fetch from ``since`` (inclusive) through today (inclusive).
    """
    import yfinance as yf
    from options_earnings.ingest.earnings_history import _df_to_ohlc_rows

    today = date.today()
    start = since if since is not None else today - timedelta(days=lookback_days)
    if start > today:
        return []
    end = today + timedelta(days=1)  # yfinance end is exclusive

    try:
        hist = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily candles fetch failed for %s: %s", symbol, exc)
        return []
    return _df_to_ohlc_rows(symbol, hist)


def stale_symbols(conn: duckdb.DuckDBPyConnection, limit: int) -> list[str]:
    """Symbols whose last stored OHLC row is not today's date (or that have no
    OHLC at all). Never-fetched come first, then oldest-last-day first, then
    alphabetical for stable ordering.
    """
    rows = conn.execute(
        """
        SELECT s.symbol
        FROM symbols s
        LEFT JOIN (
            SELECT symbol, MAX(trading_day) AS last_day
            FROM earnings_ohlc GROUP BY symbol
        ) o ON o.symbol = s.symbol
        WHERE o.last_day IS NULL OR o.last_day < CURRENT_DATE
        ORDER BY o.last_day ASC NULLS FIRST, s.symbol ASC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [r[0] for r in rows]


def _last_day_per_symbol(
    conn: duckdb.DuckDBPyConnection, symbols: list[str]
) -> dict[str, date]:
    if not symbols:
        return {}
    placeholders = ", ".join(["?"] * len(symbols))
    rows = conn.execute(
        f"SELECT symbol, MAX(trading_day) FROM earnings_ohlc "
        f"WHERE symbol IN ({placeholders}) GROUP BY symbol",
        symbols,
    ).fetchall()
    return {r[0]: r[1] for r in rows if r[1] is not None}


def run_daily_candles_batch(
    db_path: str | Path,
    *,
    batch_size: int,
    lookback_days: int,
    skip_weekends: bool = True,
) -> dict[str, Any]:
    """Fetch daily candles for the next ``batch_size`` stalest symbols.
    Returns a small summary dict for logging.
    """
    from options_earnings.db import repo
    from options_earnings.db.connection import get_conn

    today = date.today()
    if skip_weekends and today.weekday() >= 5:
        return {"symbols": 0, "rows": 0, "skipped_weekend": True}

    inserted_rows = 0
    processed: list[str] = []
    with get_conn(db_path) as conn:
        symbols = stale_symbols(conn, batch_size)
        if not symbols:
            return {"symbols": 0, "rows": 0, "skipped_weekend": False}

        last_days = _last_day_per_symbol(conn, symbols)
        for sym in symbols:
            since = last_days.get(sym)
            since = (since + timedelta(days=1)) if since is not None else None
            rows = fetch_daily_candles(
                sym, lookback_days=lookback_days, since=since
            )
            if rows:
                inserted_rows += repo.upsert_ohlc(conn, rows)
            processed.append(sym)

    logger.info(
        "daily candles batch: processed=%d rows=%d symbols=%s",
        len(processed), inserted_rows, ",".join(processed),
    )
    return {
        "symbols": len(processed),
        "rows": inserted_rows,
        "skipped_weekend": False,
        "processed": processed,
    }
