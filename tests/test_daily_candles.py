from __future__ import annotations

from datetime import date, timedelta

import pytest

from options_earnings.db import repo
from options_earnings.db.connection import open_memory
from options_earnings.db.repo import OHLCRow, SymbolRow
from options_earnings.ingest import daily_candles


@pytest.fixture
def conn():
    c = open_memory()
    try:
        yield c
    finally:
        c.close()


def _sym(symbol: str) -> SymbolRow:
    from datetime import datetime
    return SymbolRow(
        symbol=symbol, company_name=symbol, sector="Tech",
        market_cap=1_000_000_000, last_price=100.0,
        next_earnings=None, earnings_when=None,
        refreshed_at=datetime(2026, 7, 3, 12, 0),
    )


def test_stale_symbols_orders_never_first_then_oldest(conn):
    repo.upsert_symbol(conn, _sym("NEVER"))
    repo.upsert_symbol(conn, _sym("OLD"))
    repo.upsert_symbol(conn, _sym("NEWER"))
    repo.upsert_symbol(conn, _sym("CURRENT"))

    today = date.today()
    repo.upsert_ohlc(conn, [
        OHLCRow("OLD", today - timedelta(days=5), 1.0, 2.0, 0.5, 1.0),
        OHLCRow("NEWER", today - timedelta(days=1), 1.0, 2.0, 0.5, 1.0),
        OHLCRow("CURRENT", today, 1.0, 2.0, 0.5, 1.0),
    ])

    stale = daily_candles.stale_symbols(conn, limit=10)
    # NEVER first (NULL), OLD (older last_day), NEWER (newer last_day);
    # CURRENT excluded because last_day == today.
    assert stale == ["NEVER", "OLD", "NEWER"]

    assert daily_candles.stale_symbols(conn, limit=1) == ["NEVER"]


def test_stale_symbols_empty_when_all_current(conn):
    repo.upsert_symbol(conn, _sym("A"))
    repo.upsert_ohlc(conn, [
        OHLCRow("A", date.today(), 1.0, 2.0, 0.5, 1.0),
    ])
    assert daily_candles.stale_symbols(conn, limit=10) == []


def test_last_day_per_symbol(conn):
    repo.upsert_symbol(conn, _sym("A"))
    repo.upsert_symbol(conn, _sym("B"))
    today = date.today()
    yesterday = today - timedelta(days=1)
    repo.upsert_ohlc(conn, [
        OHLCRow("A", yesterday, 1.0, 2.0, 0.5, 1.0),
        OHLCRow("A", today, 1.0, 2.0, 0.5, 1.0),
        OHLCRow("B", yesterday, 1.0, 2.0, 0.5, 1.0),
    ])
    got = daily_candles._last_day_per_symbol(conn, ["A", "B", "MISSING"])
    assert got == {"A": today, "B": yesterday}


def test_run_daily_candles_batch_with_mocked_fetch(conn, monkeypatch, tmp_path):
    from options_earnings.db.connection import open_db

    db_path = tmp_path / "test.duckdb"
    real_conn = open_db(db_path)
    for s in ["A", "B", "C"]:
        repo.upsert_symbol(real_conn, _sym(s))
    today = date.today()
    repo.upsert_ohlc(real_conn, [
        OHLCRow("A", today - timedelta(days=1), 1.0, 2.0, 0.5, 1.5),
    ])
    real_conn.close()

    calls: list[tuple[str, date | None]] = []

    def fake_fetch(symbol: str, *, lookback_days: int, since: date | None):
        calls.append((symbol, since))
        return [OHLCRow(symbol, today, 10.0, 12.0, 9.0, 11.0)]

    monkeypatch.setattr(daily_candles, "fetch_daily_candles", fake_fetch)

    result = daily_candles.run_daily_candles_batch(
        db_path, batch_size=2, lookback_days=90, skip_weekends=False,
    )
    assert result["symbols"] == 2
    assert result["rows"] == 2

    # NEVER-fetched B/C should have since=None; A should have since=today (yesterday+1).
    calls_by_sym = {c[0]: c[1] for c in calls}
    processed = set(calls_by_sym.keys())
    assert processed.issubset({"A", "B", "C"}) and len(processed) == 2
    if "A" in processed:
        assert calls_by_sym["A"] == today
    for other in processed - {"A"}:
        assert calls_by_sym[other] is None


def test_run_daily_candles_batch_skips_weekend(monkeypatch, tmp_path):
    from options_earnings.db.connection import open_db

    db_path = tmp_path / "test.duckdb"
    open_db(db_path).close()

    import options_earnings.ingest.daily_candles as mod
    monkeypatch.setattr(mod, "date", type("D", (), {"today": staticmethod(lambda: date(2026, 6, 6))}))  # Saturday
    result = mod.run_daily_candles_batch(
        db_path, batch_size=10, lookback_days=90, skip_weekends=True,
    )
    assert result == {"symbols": 0, "rows": 0, "skipped_weekend": True}
