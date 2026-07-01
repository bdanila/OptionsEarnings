from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from options_earnings.config import Settings
from options_earnings.db import repo
from options_earnings.db.connection import open_memory
from options_earnings.db.repo import SymbolRow
from options_earnings.jobs import scheduler as sched_mod


def _sym(symbol: str, earnings: date | None) -> SymbolRow:
    return SymbolRow(
        symbol=symbol,
        company_name=f"{symbol} Corp",
        sector="Tech",
        market_cap=1_000_000_000,
        last_price=100.0,
        next_earnings=earnings,
        earnings_when=None,
        refreshed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def test_watchlist_filters_by_window(tmp_path, monkeypatch):
    db_path = tmp_path / "test.duckdb"
    today = date.today()
    rows = [
        _sym("NEAR", today + timedelta(days=2)),
        _sym("FAR", today + timedelta(days=60)),
        _sym("PAST", today - timedelta(days=5)),
        _sym("NULL_E", None),
        _sym("EDGE", today + timedelta(days=14)),
    ]
    from options_earnings.db.connection import open_db
    conn = open_db(db_path)
    for r in rows:
        repo.upsert_symbol(conn, r)
    conn.close()

    out = sched_mod._watchlist_symbols(db_path, days=14)
    assert set(out) == {"NEAR", "EDGE"}


def test_start_scheduler_disabled_returns_none():
    s = Settings(scheduler_enabled=False)
    assert sched_mod.start_scheduler(s) is None
