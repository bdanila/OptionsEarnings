from __future__ import annotations

from datetime import date

import duckdb


def iv_history_rolling_atm(
    conn: duckdb.DuckDBPyConnection,
    symbol: str,
    *,
    expiry: date | None = None,
    cp: str = "C",
) -> list[dict]:
    where = "WHERE symbol = ? AND cp = ?"
    params: list = [symbol, cp]
    if expiry is not None:
        where += " AND expiry = ?"
        params.append(expiry)
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT snapshot_ts, iv_computed, strike, underlying,
                   ROW_NUMBER() OVER (
                       PARTITION BY snapshot_ts
                       ORDER BY ABS(strike - underlying) ASC, strike ASC
                   ) AS rk
            FROM option_quotes
            {where}
        )
        SELECT snapshot_ts, iv_computed, strike, underlying
        FROM ranked
        WHERE rk = 1
        ORDER BY snapshot_ts ASC
        """,
        params,
    ).fetchall()
    return [
        {
            "snapshot_ts": r[0],
            "iv_computed": r[1],
            "strike": r[2],
            "underlying": r[3],
        }
        for r in rows
    ]


def iv_history_fixed_strike(
    conn: duckdb.DuckDBPyConnection,
    symbol: str,
    strike: float,
    *,
    expiry: date | None = None,
    cp: str = "C",
) -> list[dict]:
    where = "WHERE symbol = ? AND cp = ? AND strike = ?"
    params: list = [symbol, cp, strike]
    if expiry is not None:
        where += " AND expiry = ?"
        params.append(expiry)
    rows = conn.execute(
        f"""
        SELECT snapshot_ts, iv_computed, strike, underlying
        FROM option_quotes
        {where}
        ORDER BY snapshot_ts ASC
        """,
        params,
    ).fetchall()
    return [
        {
            "snapshot_ts": r[0],
            "iv_computed": r[1],
            "strike": r[2],
            "underlying": r[3],
        }
        for r in rows
    ]


def nearest_strike_today(
    conn: duckdb.DuckDBPyConnection,
    symbol: str,
    *,
    expiry: date | None = None,
    cp: str = "C",
) -> float | None:
    where = "WHERE symbol = ? AND cp = ?"
    params: list = [symbol, cp]
    if expiry is not None:
        where += " AND expiry = ?"
        params.append(expiry)
    latest = conn.execute(
        f"""
        SELECT underlying
        FROM option_quotes
        {where}
        ORDER BY snapshot_ts DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if latest is None:
        return None
    underlying = latest[0]
    row = conn.execute(
        f"""
        SELECT strike
        FROM option_quotes
        {where}
        ORDER BY ABS(strike - ?) ASC, strike ASC
        LIMIT 1
        """,
        params + [underlying],
    ).fetchone()
    if row is None:
        return None
    return float(row[0])
