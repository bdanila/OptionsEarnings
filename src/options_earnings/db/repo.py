"""Typed read/write functions over DuckDB.

This module is the *only* layer that should write SQL. Other modules (ingest,
options, web) call these functions and pass dataclass rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable
from uuid import UUID, uuid4

import duckdb


def _utcnow() -> datetime:
    """Naive UTC timestamp suitable for DuckDB TIMESTAMP columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------- row types ----------

@dataclass
class SymbolRow:
    symbol: str
    company_name: str
    sector: str | None
    market_cap: int | None
    last_price: float | None
    next_earnings: date | None
    earnings_when: str | None  # 'BMO' | 'AMC' | None
    refreshed_at: datetime
    atm_iv: float | None = None
    iv_monitored: bool = False
    min_3m_pct: float | None = None
    max_3m_pct: float | None = None
    range_3m_pct: float | None = None
    atm_iv_pct_2w: float | None = None


@dataclass
class JobRow:
    job_id: UUID
    created_at: datetime
    symbols: list[str]
    window_size: int
    status: str  # 'pending' | 'running' | 'done' | 'error'
    error: str | None
    completed_at: datetime | None


@dataclass
class EarningsMoveRow:
    """Earnings move row. The ``_3d`` suffix is historical: the values store
    whatever window the producer chose (3d on the cached path, dynamic when
    recomputed from OHLC). The display layer labels them with the actual N.
    """
    symbol: str
    earnings_date: date
    ref_close: float
    max_up_3d_pct: float
    max_down_3d_pct: float
    computed_at: datetime
    window_high_3d: float | None = None
    window_low_3d: float | None = None
    window_close_3d: float | None = None
    window_close_pct_3d: float | None = None


@dataclass
class OHLCRow:
    symbol: str
    trading_day: date
    open: float | None
    high: float
    low: float
    close: float


@dataclass
class QuoteRow:
    job_id: UUID
    symbol: str
    snapshot_ts: datetime
    underlying: float
    expiry: date
    strike: float
    cp: str  # 'C' | 'P'
    bid: float | None
    ask: float | None
    last: float | None
    volume: int | None
    open_interest: int | None
    iv_yahoo: float | None
    iv_computed: float | None


# ---------- symbols ----------

_SORTABLE_SYMBOL_COLS = {
    "symbol": "s.symbol",
    "company_name": "s.company_name",
    "last_price": "s.last_price",
    "next_earnings": "s.next_earnings",
    "market_cap": "s.market_cap",
    "atm_iv": "a.iv",
    "min_3m_pct": "min_3m_pct",
    "max_3m_pct": "max_3m_pct",
    "range_3m_pct": "range_3m_pct",
    "atm_iv_pct_2w": "atm_iv_pct_2w",
}


_ATM_IV_CTE = """
    WITH latest AS (
        SELECT symbol, MAX(snapshot_ts) AS ts
        FROM option_quotes WHERE cp = 'C'
        GROUP BY symbol
    ), ranked AS (
        SELECT q.symbol, COALESCE(q.iv_computed, q.iv_yahoo) AS iv,
               ROW_NUMBER() OVER (
                   PARTITION BY q.symbol
                   ORDER BY ABS(q.strike - q.underlying) ASC, q.strike ASC
               ) AS rk
        FROM option_quotes q
        JOIN latest l ON l.symbol = q.symbol AND l.ts = q.snapshot_ts
        WHERE q.cp = 'C'
    ), atm AS (
        SELECT symbol, iv FROM ranked WHERE rk = 1
    ), stats_3m AS (
        SELECT symbol, MIN(low) AS min_3m, MAX(high) AS max_3m
        FROM earnings_ohlc
        WHERE trading_day >= CURRENT_DATE - 90
        GROUP BY symbol
    ), atm_iv_2w_ranked AS (
        SELECT q.symbol, q.snapshot_ts,
               COALESCE(q.iv_computed, q.iv_yahoo) AS iv,
               ROW_NUMBER() OVER (
                   PARTITION BY q.symbol, q.snapshot_ts
                   ORDER BY ABS(q.strike - q.underlying) ASC, q.strike ASC
               ) AS rk
        FROM option_quotes q
        WHERE q.cp = 'C'
          AND q.snapshot_ts >= CURRENT_TIMESTAMP - INTERVAL 14 DAY
    ), atm_iv_2w AS (
        SELECT symbol, MIN(iv) AS min_iv_2w, MAX(iv) AS max_iv_2w
        FROM atm_iv_2w_ranked
        WHERE rk = 1 AND iv IS NOT NULL
        GROUP BY symbol
    )
"""

_RANGE_3M_EXPR = (
    "(CASE WHEN s.last_price > 0 AND st.min_3m IS NOT NULL AND st.max_3m IS NOT NULL "
    "THEN ABS((st.min_3m - s.last_price) / s.last_price * 100.0) "
    "   + ABS((st.max_3m - s.last_price) / s.last_price * 100.0) "
    "ELSE NULL END)"
)


def upsert_symbol(conn: duckdb.DuckDBPyConnection, row: SymbolRow) -> None:
    """Upsert a symbol. On conflict, fields that come in as NULL are coalesced
    against the existing row so a rate-limited yfinance refresh does not wipe
    previously-good price/market_cap/earnings values.
    """
    conn.execute(
        """
        INSERT INTO symbols (symbol, company_name, sector, market_cap, last_price,
                             next_earnings, earnings_when, refreshed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol) DO UPDATE SET
            company_name  = excluded.company_name,
            sector        = excluded.sector,
            market_cap    = COALESCE(excluded.market_cap,    symbols.market_cap),
            last_price    = COALESCE(excluded.last_price,    symbols.last_price),
            next_earnings = COALESCE(excluded.next_earnings, symbols.next_earnings),
            earnings_when = COALESCE(excluded.earnings_when, symbols.earnings_when),
            refreshed_at  = excluded.refreshed_at
        """,
        [
            row.symbol,
            row.company_name,
            row.sector,
            row.market_cap,
            row.last_price,
            row.next_earnings,
            row.earnings_when,
            row.refreshed_at,
        ],
    )


def get_symbol(conn: duckdb.DuckDBPyConnection, symbol: str) -> SymbolRow | None:
    rows = conn.execute(
        "SELECT symbol, company_name, sector, market_cap, last_price, next_earnings, "
        "earnings_when, refreshed_at, NULL AS atm_iv, COALESCE(iv_monitored, FALSE) "
        "FROM symbols WHERE symbol = ?",
        [symbol],
    ).fetchall()
    if not rows:
        return None
    return SymbolRow(*rows[0])


def set_iv_monitored(
    conn: duckdb.DuckDBPyConnection, symbols: list[str], enabled: bool
) -> int:
    if not symbols:
        return 0
    placeholders = ", ".join(["?"] * len(symbols))
    res = conn.execute(
        f"UPDATE symbols SET iv_monitored = ? WHERE symbol IN ({placeholders})",
        [enabled, *symbols],
    )
    try:
        return res.rowcount or 0
    except AttributeError:
        return len(symbols)


def monitored_symbols(conn: duckdb.DuckDBPyConnection) -> list[str]:
    rows = conn.execute(
        "SELECT symbol FROM symbols WHERE iv_monitored = TRUE ORDER BY symbol"
    ).fetchall()
    return [r[0] for r in rows]


def list_symbols(
    conn: duckdb.DuckDBPyConnection,
    page: int = 1,
    size: int = 50,
    sort: str = "symbol",
    dir_: str = "asc",
    q: str | None = None,
    min_mcap: float | None = None,
    earnings_from: date | None = None,
    earnings_to: date | None = None,
    iv_monitored: bool | None = None,
    range_3m_min: float | None = None,
    range_3m_max: float | None = None,
) -> tuple[list[SymbolRow], int]:
    col = _SORTABLE_SYMBOL_COLS.get(sort, "s.symbol")
    direction = "DESC" if dir_.lower() == "desc" else "ASC"
    offset = max(0, (page - 1) * size)

    where_parts: list[str] = []
    where_params: list = []
    if q:
        where_parts.append("(LOWER(s.symbol) LIKE ? OR LOWER(s.company_name) LIKE ?)")
        like = f"%{q.lower()}%"
        where_params.extend([like, like])
    if min_mcap is not None:
        where_parts.append("s.market_cap >= ?")
        where_params.append(min_mcap)
    if earnings_from is not None:
        where_parts.append("s.next_earnings >= ?")
        where_params.append(earnings_from)
    if earnings_to is not None:
        where_parts.append("s.next_earnings <= ?")
        where_params.append(earnings_to)
    if iv_monitored is True:
        where_parts.append("COALESCE(s.iv_monitored, FALSE) = TRUE")
    elif iv_monitored is False:
        where_parts.append("COALESCE(s.iv_monitored, FALSE) = FALSE")
    if range_3m_min is not None:
        where_parts.append(f"{_RANGE_3M_EXPR} >= ?")
        where_params.append(range_3m_min)
    if range_3m_max is not None:
        where_parts.append(f"{_RANGE_3M_EXPR} <= ?")
        where_params.append(range_3m_max)
    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    rows = conn.execute(
        f"""{_ATM_IV_CTE}
        SELECT s.symbol, s.company_name, s.sector, s.market_cap, s.last_price,
               s.next_earnings, s.earnings_when, s.refreshed_at, a.iv AS atm_iv,
               COALESCE(s.iv_monitored, FALSE) AS iv_monitored,
               CASE WHEN s.last_price > 0 AND st.min_3m IS NOT NULL
                    THEN (st.min_3m - s.last_price) / s.last_price * 100.0
                    ELSE NULL END AS min_3m_pct,
               CASE WHEN s.last_price > 0 AND st.max_3m IS NOT NULL
                    THEN (st.max_3m - s.last_price) / s.last_price * 100.0
                    ELSE NULL END AS max_3m_pct,
               {_RANGE_3M_EXPR} AS range_3m_pct,
               CASE WHEN a.iv IS NOT NULL AND iv2w.max_iv_2w IS NOT NULL
                    AND iv2w.min_iv_2w IS NOT NULL
                    AND (iv2w.max_iv_2w - iv2w.min_iv_2w) > 0
                    THEN (a.iv - iv2w.min_iv_2w) / (iv2w.max_iv_2w - iv2w.min_iv_2w) * 100.0
                    ELSE NULL END AS atm_iv_pct_2w
        FROM symbols s
        LEFT JOIN atm a ON a.symbol = s.symbol
        LEFT JOIN stats_3m st ON st.symbol = s.symbol
        LEFT JOIN atm_iv_2w iv2w ON iv2w.symbol = s.symbol
        {where_sql}
        ORDER BY {col} {direction} NULLS LAST, s.symbol ASC
        LIMIT ? OFFSET ?
        """,
        [*where_params, size, offset],
    ).fetchall()
    total = conn.execute(
        f"""{_ATM_IV_CTE}
        SELECT COUNT(*) FROM symbols s
        LEFT JOIN stats_3m st ON st.symbol = s.symbol
        {where_sql}
        """,
        where_params,
    ).fetchone()[0]
    return [SymbolRow(*r) for r in rows], int(total)


# ---------- jobs ----------

def create_job(
    conn: duckdb.DuckDBPyConnection,
    symbols: list[str],
    window_size: int,
) -> UUID:
    job_id = uuid4()
    conn.execute(
        "INSERT INTO option_chain_jobs (job_id, created_at, symbols, window_size, status) "
        "VALUES (?, ?, ?, ?, 'pending')",
        [str(job_id), _utcnow(), symbols, window_size],
    )
    return job_id


def update_job_status(
    conn: duckdb.DuckDBPyConnection,
    job_id: UUID,
    status: str,
    error: str | None = None,
) -> None:
    completed_at = _utcnow() if status in ("done", "error") else None
    conn.execute(
        "UPDATE option_chain_jobs SET status = ?, error = ?, completed_at = ? WHERE job_id = ?",
        [status, error, completed_at, str(job_id)],
    )


def get_job(conn: duckdb.DuckDBPyConnection, job_id: UUID) -> JobRow | None:
    rows = conn.execute(
        "SELECT job_id, created_at, symbols, window_size, status, error, completed_at "
        "FROM option_chain_jobs WHERE job_id = ?",
        [str(job_id)],
    ).fetchall()
    if not rows:
        return None
    r = rows[0]
    return JobRow(
        job_id=r[0] if isinstance(r[0], UUID) else UUID(str(r[0])),
        created_at=r[1],
        symbols=list(r[2]),
        window_size=r[3],
        status=r[4],
        error=r[5],
        completed_at=r[6],
    )


def list_jobs(conn: duckdb.DuckDBPyConnection, limit: int = 20) -> list[JobRow]:
    rows = conn.execute(
        "SELECT job_id, created_at, symbols, window_size, status, error, completed_at "
        "FROM option_chain_jobs ORDER BY created_at DESC, job_id DESC LIMIT ?",
        [limit],
    ).fetchall()
    return [
        JobRow(
            job_id=r[0] if isinstance(r[0], UUID) else UUID(str(r[0])),
            created_at=r[1],
            symbols=list(r[2]),
            window_size=r[3],
            status=r[4],
            error=r[5],
            completed_at=r[6],
        )
        for r in rows
    ]


# ---------- quotes ----------

def insert_quotes(conn: duckdb.DuckDBPyConnection, rows: Iterable[QuoteRow]) -> int:
    payload = [
        [
            str(r.job_id),
            r.symbol,
            r.snapshot_ts,
            r.underlying,
            r.expiry,
            r.strike,
            r.cp,
            r.bid,
            r.ask,
            r.last,
            r.volume,
            r.open_interest,
            r.iv_yahoo,
            r.iv_computed,
        ]
        for r in rows
    ]
    if not payload:
        return 0
    conn.executemany(
        """
        INSERT INTO option_quotes (job_id, symbol, snapshot_ts, underlying, expiry, strike, cp,
                                   bid, ask, last, volume, open_interest, iv_yahoo, iv_computed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (job_id, symbol, expiry, strike, cp) DO UPDATE SET
            snapshot_ts   = excluded.snapshot_ts,
            underlying    = excluded.underlying,
            bid           = excluded.bid,
            ask           = excluded.ask,
            last          = excluded.last,
            volume        = excluded.volume,
            open_interest = excluded.open_interest,
            iv_yahoo      = excluded.iv_yahoo,
            iv_computed   = excluded.iv_computed
        """,
        payload,
    )
    return len(payload)


def _row_to_quote(r) -> QuoteRow:
    return QuoteRow(
        job_id=r[0] if isinstance(r[0], UUID) else UUID(str(r[0])),
        symbol=r[1],
        snapshot_ts=r[2],
        underlying=r[3],
        expiry=r[4],
        strike=r[5],
        cp=r[6],
        bid=r[7],
        ask=r[8],
        last=r[9],
        volume=r[10],
        open_interest=r[11],
        iv_yahoo=r[12],
        iv_computed=r[13],
    )


_QUOTE_COLS = (
    "job_id, symbol, snapshot_ts, underlying, expiry, strike, cp, "
    "bid, ask, last, volume, open_interest, iv_yahoo, iv_computed"
)


def quotes_for_job(conn: duckdb.DuckDBPyConnection, job_id: UUID) -> list[QuoteRow]:
    rows = conn.execute(
        f"SELECT {_QUOTE_COLS} FROM option_quotes WHERE job_id = ? "
        f"ORDER BY symbol, expiry, strike, cp",
        [str(job_id)],
    ).fetchall()
    return [_row_to_quote(r) for r in rows]


def atm_quotes_for_job(conn: duckdb.DuckDBPyConnection, job_id: UUID) -> list[QuoteRow]:
    """Per (symbol, expiry, cp), return the single quote whose strike is closest to underlying."""
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT {_QUOTE_COLS},
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, expiry, cp
                       ORDER BY ABS(strike - underlying) ASC, strike ASC
                   ) AS rk
            FROM option_quotes
            WHERE job_id = ?
        )
        SELECT {_QUOTE_COLS} FROM ranked WHERE rk = 1 ORDER BY symbol, expiry, cp
        """,
        [str(job_id)],
    ).fetchall()
    return [_row_to_quote(r) for r in rows]


def quotes_for_symbol(
    conn: duckdb.DuckDBPyConnection,
    symbol: str,
    expiry: date | None = None,
    cp: str | None = None,
) -> list[QuoteRow]:
    where = "WHERE symbol = ?"
    params: list = [symbol]
    if expiry is not None:
        where += " AND expiry = ?"
        params.append(expiry)
    if cp is not None:
        where += " AND cp = ?"
        params.append(cp)
    rows = conn.execute(
        f"SELECT {_QUOTE_COLS} FROM option_quotes {where} "
        f"ORDER BY snapshot_ts ASC, strike ASC",
        params,
    ).fetchall()
    return [_row_to_quote(r) for r in rows]


_MOVE_COLS = (
    "symbol, earnings_date, ref_close, max_up_3d_pct, max_down_3d_pct, computed_at, "
    "window_high_3d, window_low_3d, window_close_3d, window_close_pct_3d"
)


def upsert_earnings_move(conn: duckdb.DuckDBPyConnection, row: EarningsMoveRow) -> None:
    conn.execute(
        """
        INSERT INTO earnings_moves (symbol, earnings_date, ref_close,
                                    max_up_3d_pct, max_down_3d_pct, computed_at,
                                    window_high_3d, window_low_3d,
                                    window_close_3d, window_close_pct_3d)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol, earnings_date) DO UPDATE SET
            ref_close           = excluded.ref_close,
            max_up_3d_pct       = excluded.max_up_3d_pct,
            max_down_3d_pct     = excluded.max_down_3d_pct,
            computed_at         = excluded.computed_at,
            window_high_3d      = excluded.window_high_3d,
            window_low_3d       = excluded.window_low_3d,
            window_close_3d     = excluded.window_close_3d,
            window_close_pct_3d = excluded.window_close_pct_3d
        """,
        [
            row.symbol,
            row.earnings_date,
            row.ref_close,
            row.max_up_3d_pct,
            row.max_down_3d_pct,
            row.computed_at,
            row.window_high_3d,
            row.window_low_3d,
            row.window_close_3d,
            row.window_close_pct_3d,
        ],
    )


def latest_earnings_move(
    conn: duckdb.DuckDBPyConnection, symbol: str
) -> EarningsMoveRow | None:
    rows = conn.execute(
        f"SELECT {_MOVE_COLS} FROM earnings_moves WHERE symbol = ? "
        f"ORDER BY earnings_date DESC LIMIT 1",
        [symbol],
    ).fetchall()
    if not rows:
        return None
    return EarningsMoveRow(*rows[0])


def recent_earnings_moves(
    conn: duckdb.DuckDBPyConnection, symbol: str, limit: int = 4
) -> list[EarningsMoveRow]:
    rows = conn.execute(
        f"SELECT {_MOVE_COLS} FROM earnings_moves WHERE symbol = ? "
        f"ORDER BY earnings_date DESC LIMIT ?",
        [symbol, limit],
    ).fetchall()
    return [EarningsMoveRow(*r) for r in rows]


def upsert_ohlc(conn: duckdb.DuckDBPyConnection, rows: Iterable[OHLCRow]) -> int:
    payload = [
        [r.symbol, r.trading_day, r.open, r.high, r.low, r.close] for r in rows
    ]
    if not payload:
        return 0
    conn.executemany(
        """
        INSERT INTO earnings_ohlc (symbol, trading_day, open, high, low, close)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol, trading_day) DO UPDATE SET
            open  = excluded.open,
            high  = excluded.high,
            low   = excluded.low,
            close = excluded.close
        """,
        payload,
    )
    return len(payload)


def ohlc_for_symbol(conn: duckdb.DuckDBPyConnection, symbol: str) -> list[OHLCRow]:
    rows = conn.execute(
        "SELECT symbol, trading_day, open, high, low, close FROM earnings_ohlc "
        "WHERE symbol = ? ORDER BY trading_day ASC",
        [symbol],
    ).fetchall()
    return [OHLCRow(*r) for r in rows]


def latest_atm_iv_for_symbols(
    conn: duckdb.DuckDBPyConnection,
    symbols: list[str],
    cp: str = "C",
) -> dict[str, float]:
    """Return {symbol: ATM IV} using the most recent snapshot for each symbol.

    Prefers ``iv_computed`` then ``iv_yahoo``. Symbols absent from
    ``option_quotes`` are omitted from the result.
    """
    if not symbols:
        return {}
    placeholders = ", ".join(["?"] * len(symbols))
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT symbol, MAX(snapshot_ts) AS ts
            FROM option_quotes
            WHERE cp = ? AND symbol IN ({placeholders})
            GROUP BY symbol
        ),
        ranked AS (
            SELECT q.symbol, q.iv_computed, q.iv_yahoo,
                   ROW_NUMBER() OVER (
                       PARTITION BY q.symbol
                       ORDER BY ABS(q.strike - q.underlying) ASC, q.strike ASC
                   ) AS rk
            FROM option_quotes q
            JOIN latest l ON l.symbol = q.symbol AND l.ts = q.snapshot_ts
            WHERE q.cp = ?
        )
        SELECT symbol, COALESCE(iv_computed, iv_yahoo) AS iv
        FROM ranked
        WHERE rk = 1
        """,
        [cp, *symbols, cp],
    ).fetchall()
    return {symbol: float(iv) for symbol, iv in rows if iv is not None}


def sync_last_price_from_ohlc(
    conn: duckdb.DuckDBPyConnection, symbols: list[str] | None = None
) -> int:
    """Refresh ``symbols.last_price`` from the most recent close in
    ``earnings_ohlc``. If ``symbols`` is None, syncs all symbols that have
    OHLC data. Returns the number of rows updated.
    """
    if symbols is not None and not symbols:
        return 0
    where_extra = ""
    params: list[Any] = []
    if symbols is not None:
        placeholders = ", ".join(["?"] * len(symbols))
        where_extra = f" AND symbol IN ({placeholders})"
        params.extend(symbols)
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT symbol,
                   FIRST(close ORDER BY trading_day DESC) AS latest_close
            FROM earnings_ohlc
            WHERE close IS NOT NULL{where_extra}
            GROUP BY symbol
        )
        UPDATE symbols AS s
        SET last_price = l.latest_close,
            refreshed_at = CURRENT_TIMESTAMP
        FROM latest l
        WHERE s.symbol = l.symbol
        RETURNING s.symbol
        """,
        params,
    ).fetchall()
    return len(rows)


def daily_candles_for_symbol(
    conn: duckdb.DuckDBPyConnection, symbol: str, days: int = 90
) -> list[OHLCRow]:
    """Return OHLC rows for the last ``days`` calendar days, sorted ascending."""
    rows = conn.execute(
        "SELECT symbol, trading_day, open, high, low, close "
        "FROM earnings_ohlc "
        "WHERE symbol = ? AND trading_day >= CURRENT_DATE - ? "
        "ORDER BY trading_day ASC",
        [symbol, days],
    ).fetchall()
    return [OHLCRow(*r) for r in rows]


def daily_candles_progress(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Coverage snapshot for the daily-candles ingest, used by the UI pill.

    "current" = symbols whose newest OHLC row equals the newest OHLC row in
    the whole table. Self-referential so we don't need to know about weekends
    or holidays.
    """
    row = conn.execute(
        """
        WITH max_day AS (
            SELECT MAX(trading_day) AS d FROM earnings_ohlc
        ), per_sym AS (
            SELECT symbol, MAX(trading_day) AS last_day
            FROM earnings_ohlc GROUP BY symbol
        )
        SELECT
            (SELECT COUNT(*) FROM symbols) AS total,
            (SELECT COUNT(*) FROM per_sym) AS with_data,
            (SELECT COUNT(*) FROM per_sym p, max_day m WHERE p.last_day = m.d) AS current,
            (SELECT d FROM max_day) AS latest_day
        """
    ).fetchone()
    return {
        "total": int(row[0] or 0),
        "with_data": int(row[1] or 0),
        "current": int(row[2] or 0),
        "latest_day": row[3],
    }


def expiries_for_symbol(conn: duckdb.DuckDBPyConnection, symbol: str) -> list[date]:
    rows = conn.execute(
        "SELECT DISTINCT expiry FROM option_quotes WHERE symbol = ? ORDER BY expiry ASC",
        [symbol],
    ).fetchall()
    return [r[0] for r in rows]
