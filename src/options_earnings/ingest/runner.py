from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from options_earnings.config import get_settings
from options_earnings.db import repo
from options_earnings.ingest.earnings import fetch_next_earnings
from options_earnings.ingest.prices import fetch_quote
from options_earnings.ingest.sp500 import fetch_sp500_constituents

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fetch_one(constituent: dict[str, Any]) -> dict[str, Any] | None:
    symbol = constituent["symbol"]
    try:
        quote = fetch_quote(symbol)
    except Exception as e:  # noqa: BLE001
        logger.warning("fetch_quote failed for %s: %s", symbol, e)
        return None
    try:
        next_earnings, when = fetch_next_earnings(symbol)
    except Exception as e:  # noqa: BLE001
        logger.warning("fetch_next_earnings failed for %s: %s", symbol, e)
        next_earnings, when = None, None
    return {
        "symbol": symbol,
        "company_name": constituent.get("company_name") or "",
        "sector": constituent.get("sector"),
        "last_price": quote.get("last_price"),
        "market_cap": quote.get("market_cap"),
        "next_earnings": next_earnings,
        "earnings_when": when,
    }


def refresh_missing_earnings(
    conn: duckdb.DuckDBPyConnection,
    *,
    max_workers: int = 4,
    retries: int = 2,
    base_delay: float = 0.5,
) -> int:
    """For every row in ``symbols`` whose ``next_earnings`` is NULL, re-try
    ``fetch_next_earnings`` with limited concurrency and a small retry+backoff
    to dodge yfinance rate limiting. Returns count of rows updated.
    """
    rows = conn.execute(
        "SELECT symbol FROM symbols WHERE next_earnings IS NULL ORDER BY symbol"
    ).fetchall()
    symbols = [r[0] for r in rows]
    if not symbols:
        return 0

    def _try(sym: str) -> tuple[Any, Any]:
        for attempt in range(retries + 1):
            try:
                d, when = fetch_next_earnings(sym)
                if d is not None:
                    return d, when
            except Exception as e:  # noqa: BLE001
                logger.debug("fetch attempt %d for %s failed: %s", attempt, sym, e)
            if attempt < retries:
                time.sleep(base_delay * (1 + attempt))
        return None, None

    workers = max(1, min(max_workers, len(symbols)))
    results: list[tuple[str, Any, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_try, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                d, when = fut.result()
            except Exception as e:  # noqa: BLE001
                logger.warning("refresh_missing_earnings worker failed for %s: %s", sym, e)
                continue
            results.append((sym, d, when))

    updated = 0
    now = _utcnow()
    for sym, d, when in results:
        if d is None:
            continue
        conn.execute(
            "UPDATE symbols SET next_earnings = ?, earnings_when = ?, refreshed_at = ? "
            "WHERE symbol = ?",
            [d, when, now, sym],
        )
        updated += 1
    logger.info("refresh_missing_earnings: updated %d / %d symbols", updated, len(symbols))
    return updated


def refresh_missing_data(
    conn: duckdb.DuckDBPyConnection,
    *,
    max_workers: int = 4,
    retries: int = 2,
    base_delay: float = 0.5,
) -> tuple[int, int]:
    """Backfill rows with NULL last_price / market_cap / next_earnings.
    Uses low concurrency + retry to dodge yfinance rate limiting. Only writes
    values that come back non-NULL (existing good values are preserved).
    Returns (price_or_mcap_updated_count, earnings_updated_count).
    """
    rows = conn.execute(
        "SELECT symbol FROM symbols "
        "WHERE last_price IS NULL OR market_cap IS NULL OR next_earnings IS NULL "
        "ORDER BY symbol"
    ).fetchall()
    symbols = [r[0] for r in rows]
    if not symbols:
        return 0, 0

    def _try_quote(sym: str) -> dict[str, Any] | None:
        for attempt in range(retries + 1):
            try:
                q = fetch_quote(sym)
                if q.get("last_price") is not None or q.get("market_cap") is not None:
                    return q
            except Exception as e:  # noqa: BLE001
                logger.debug("quote attempt %d for %s failed: %s", attempt, sym, e)
            if attempt < retries:
                time.sleep(base_delay * (1 + attempt))
        return None

    def _try_earnings(sym: str) -> tuple[Any, Any]:
        for attempt in range(retries + 1):
            try:
                d, when = fetch_next_earnings(sym)
                if d is not None:
                    return d, when
            except Exception as e:  # noqa: BLE001
                logger.debug("earnings attempt %d for %s failed: %s", attempt, sym, e)
            if attempt < retries:
                time.sleep(base_delay * (1 + attempt))
        return None, None

    def _job(sym: str) -> tuple[str, dict[str, Any] | None, tuple[Any, Any]]:
        return sym, _try_quote(sym), _try_earnings(sym)

    workers = max(1, min(max_workers, len(symbols)))
    results: list[tuple[str, dict[str, Any] | None, tuple[Any, Any]]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_job, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                logger.warning("refresh_missing_data worker failed for %s: %s", sym, e)

    price_updated = 0
    earnings_updated = 0
    now = _utcnow()
    for sym, quote, (ne, when) in results:
        sets: list[str] = []
        params: list[Any] = []
        got_price = quote and quote.get("last_price") is not None
        got_mcap = quote and quote.get("market_cap") is not None
        if got_price:
            sets.append("last_price = ?")
            params.append(quote["last_price"])
        if got_mcap:
            sets.append("market_cap = ?")
            params.append(quote["market_cap"])
        if ne is not None:
            sets.append("next_earnings = ?")
            params.append(ne)
            sets.append("earnings_when = ?")
            params.append(when)
        if not sets:
            continue
        sets.append("refreshed_at = ?")
        params.append(now)
        params.append(sym)
        conn.execute(f"UPDATE symbols SET {', '.join(sets)} WHERE symbol = ?", params)
        if got_price or got_mcap:
            price_updated += 1
        if ne is not None:
            earnings_updated += 1

    logger.info(
        "refresh_missing_data: price_or_mcap updated=%d, earnings updated=%d, candidates=%d",
        price_updated, earnings_updated, len(symbols),
    )
    return price_updated, earnings_updated


def large_cap_symbols(
    conn: duckdb.DuckDBPyConnection, threshold: float
) -> list[str]:
    rows = conn.execute(
        "SELECT symbol FROM symbols WHERE market_cap IS NOT NULL AND market_cap >= ? "
        "ORDER BY market_cap DESC",
        [threshold],
    ).fetchall()
    return [r[0] for r in rows]


def refresh_large_cap_chains(
    db_path: str | Path,
    *,
    threshold: float,
    window: int,
) -> int:
    """After symbols are upserted, fetch option chains for symbols with
    market_cap >= ``threshold``. Creates one chain job for all such symbols and
    runs it inline. Returns the number of symbols dispatched.
    """
    from options_earnings.db.connection import get_conn
    from options_earnings.options.job import run_chain_job

    with get_conn(db_path) as conn:
        symbols = large_cap_symbols(conn, threshold)
        if not symbols:
            logger.info("No symbols with market_cap >= %s; skipping chain refresh", threshold)
            return 0
        job_id = repo.create_job(conn, symbols, window_size=window)
    logger.info("Dispatching chain job %s for %d large-cap symbols", job_id, len(symbols))
    run_chain_job(db_path, job_id, window=window)
    return len(symbols)


def refresh_all(
    conn: duckdb.DuckDBPyConnection,
    *,
    max_workers: int = 8,
    limit: int | None = None,
    fetch_chains: bool | None = None,
) -> int:
    constituents = fetch_sp500_constituents()
    if limit is not None:
        constituents = constituents[:limit]
    if not constituents:
        logger.warning("No S&P 500 constituents fetched; nothing to upsert")
        return 0

    workers = max(1, min(max_workers, len(constituents)))
    results: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_fetch_one, c): c for c in constituents}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                merged = fut.result()
            except Exception as e:  # noqa: BLE001
                logger.warning("worker failed for %s: %s", c.get("symbol"), e)
                continue
            if merged is None:
                continue
            results.append(merged)

    count = 0
    for merged in results:
        try:
            row = repo.SymbolRow(
                symbol=merged["symbol"],
                company_name=merged["company_name"],
                sector=merged["sector"],
                market_cap=merged["market_cap"],
                last_price=merged["last_price"],
                next_earnings=merged["next_earnings"],
                earnings_when=merged["earnings_when"],
                refreshed_at=_utcnow(),
            )
            repo.upsert_symbol(conn, row)
            count += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("upsert_symbol failed for %s: %s", merged.get("symbol"), e)
            continue

    logger.info("Upserted %d / %d symbols", count, len(constituents))

    settings = get_settings()
    do_chains = settings.fetch_chains_on_refresh if fetch_chains is None else fetch_chains
    if do_chains:
        try:
            n = refresh_large_cap_chains(
                settings.db_path,
                threshold=settings.large_cap_chain_threshold,
                window=settings.option_chain_window,
            )
            logger.info("Chain refresh complete for %d large-cap symbols", n)
        except Exception:
            logger.exception("large-cap chain refresh failed")

    return count
