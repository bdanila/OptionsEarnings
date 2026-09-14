from __future__ import annotations

from datetime import date, timedelta

import pytest

from options_earnings.db import repo
from options_earnings.db.connection import open_memory
from options_earnings.ingest import runner
from options_earnings.ingest.sp500 import normalize_symbol


def test_normalize_symbol_brk_b() -> None:
    assert normalize_symbol("BRK.B") == "BRK-B"
    assert normalize_symbol("BF.B") == "BF-B"
    assert normalize_symbol("aapl") == "AAPL"
    assert normalize_symbol(" msft ") == "MSFT"


def test_refresh_all_with_mocked_fetchers(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_memory()
    try:
        constituents = [
            {"symbol": "AAPL", "company_name": "Apple Inc.", "sector": "Technology"},
            {"symbol": "MSFT", "company_name": "Microsoft", "sector": "Technology"},
            {"symbol": "BAD", "company_name": "Bad Co", "sector": "Test"},
            {"symbol": "BRK-B", "company_name": "Berkshire", "sector": "Financials"},
        ]
        quotes = {
            "AAPL": {"symbol": "AAPL", "last_price": 190.0, "market_cap": 3_000_000_000},
            "MSFT": {"symbol": "MSFT", "last_price": 410.5, "market_cap": 3_100_000_000},
            "BRK-B": {"symbol": "BRK-B", "last_price": 405.0, "market_cap": 900_000_000},
        }
        earnings = {
            "AAPL": (date(2026, 5, 1), "AMC"),
            "MSFT": (date(2026, 4, 30), "BMO"),
            "BRK-B": (None, None),
        }

        def fake_constituents() -> list[dict]:
            return list(constituents)

        def fake_quote(sym: str) -> dict:
            if sym == "BAD":
                raise RuntimeError("network kaboom")
            return quotes[sym]

        def fake_earnings(sym: str) -> tuple[date | None, str | None]:
            return earnings.get(sym, (None, None))

        monkeypatch.setattr(runner, "fetch_sp500_constituents", fake_constituents)
        monkeypatch.setattr(runner, "fetch_quote", fake_quote)
        monkeypatch.setattr(runner, "fetch_next_earnings", fake_earnings)

        count = runner.refresh_all(conn, max_workers=4, fetch_chains=False)
        assert count == 3

        aapl = repo.get_symbol(conn, "AAPL")
        assert aapl is not None
        assert aapl.company_name == "Apple Inc."
        assert aapl.sector == "Technology"
        assert aapl.last_price == 190.0
        assert aapl.market_cap == 3_000_000_000
        assert aapl.next_earnings == date(2026, 5, 1)
        assert aapl.earnings_when == "AMC"

        msft = repo.get_symbol(conn, "MSFT")
        assert msft is not None
        assert msft.next_earnings == date(2026, 4, 30)
        assert msft.earnings_when == "BMO"

        brk = repo.get_symbol(conn, "BRK-B")
        assert brk is not None
        assert brk.next_earnings is None
        assert brk.earnings_when is None

        bad = repo.get_symbol(conn, "BAD")
        assert bad is None
    finally:
        conn.close()


def test_large_cap_symbols_filters_and_sorts() -> None:
    from datetime import datetime
    conn = open_memory()
    try:
        def s(sym: str, mcap: int | None) -> repo.SymbolRow:
            return repo.SymbolRow(
                symbol=sym, company_name=sym, sector="Tech",
                market_cap=mcap, last_price=10.0,
                next_earnings=None, earnings_when=None,
                refreshed_at=datetime(2026, 5, 12, 12, 0),
            )
        for sr in [
            s("MEGA", 3_000_000_000_000),
            s("BIG", 250_000_000_000),
            s("EDGE", 200_000_000_000),
            s("MID", 50_000_000_000),
            s("SMALL", 500_000_000),
            s("NULL", None),
        ]:
            repo.upsert_symbol(conn, sr)
        out = runner.large_cap_symbols(conn, 200_000_000_000)
        assert out == ["MEGA", "BIG", "EDGE"]
        assert runner.large_cap_symbols(conn, 5_000_000_000_000) == []
    finally:
        conn.close()


def test_refresh_all_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_memory()
    try:
        constituents = [
            {"symbol": f"S{i}", "company_name": f"Co {i}", "sector": "X"}
            for i in range(10)
        ]

        def fake_constituents() -> list[dict]:
            return list(constituents)

        def fake_quote(sym: str) -> dict:
            return {"symbol": sym, "last_price": 1.0, "market_cap": 10}

        def fake_earnings(sym: str) -> tuple[date | None, str | None]:
            return None, None

        monkeypatch.setattr(runner, "fetch_sp500_constituents", fake_constituents)
        monkeypatch.setattr(runner, "fetch_quote", fake_quote)
        monkeypatch.setattr(runner, "fetch_next_earnings", fake_earnings)

        count = runner.refresh_all(conn, limit=2, max_workers=2, fetch_chains=False)
        assert count == 2

        total = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        assert total == 2
    finally:
        conn.close()


def _sym_row(sym: str, ne: date | None) -> repo.SymbolRow:
    from datetime import datetime
    return repo.SymbolRow(
        symbol=sym, company_name=sym, sector="Tech",
        market_cap=1_000_000_000, last_price=10.0,
        next_earnings=ne, earnings_when=None,
        refreshed_at=datetime(2026, 5, 12, 12, 0),
    )


def test_stale_earnings_symbols_scopes() -> None:
    conn = open_memory()
    try:
        today = date.today()
        for sr in [
            _sym_row("NONE", None),
            _sym_row("PAST", today - timedelta(days=3)),
            _sym_row("TODAY", today),
            _sym_row("FUTURE", today + timedelta(days=30)),
        ]:
            repo.upsert_symbol(conn, sr)
        assert runner.stale_earnings_symbols(conn, scope="missing") == ["NONE"]
        assert runner.stale_earnings_symbols(conn, scope="stale") == ["NONE", "PAST"]
        assert runner.stale_earnings_symbols(conn, scope="all") == [
            "FUTURE", "NONE", "PAST", "TODAY",
        ]
        with pytest.raises(ValueError):
            runner.stale_earnings_symbols(conn, scope="bogus")
    finally:
        conn.close()


def test_refresh_earnings_dates_updates_stale_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = open_memory()
    try:
        today = date.today()
        future = today + timedelta(days=30)
        for sr in [
            _sym_row("NONE", None),
            _sym_row("PAST", today - timedelta(days=3)),
            _sym_row("FUTURE", future),
        ]:
            repo.upsert_symbol(conn, sr)

        called: list[str] = []
        new_date = today + timedelta(days=45)

        def fake_earnings(sym: str) -> tuple[date | None, str | None]:
            called.append(sym)
            return new_date, "AMC"

        monkeypatch.setattr(runner, "fetch_next_earnings", fake_earnings)

        updated, candidates = runner.refresh_earnings_dates(conn, max_workers=2)
        assert (updated, candidates) == (2, 2)
        assert sorted(called) == ["NONE", "PAST"]

        assert repo.get_symbol(conn, "NONE").next_earnings == new_date
        assert repo.get_symbol(conn, "PAST").next_earnings == new_date
        assert repo.get_symbol(conn, "PAST").earnings_when == "AMC"
        # untouched: its date is still in the future
        assert repo.get_symbol(conn, "FUTURE").next_earnings == future
    finally:
        conn.close()


def test_refresh_earnings_dates_keeps_old_date_when_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = open_memory()
    try:
        past = date.today() - timedelta(days=2)
        repo.upsert_symbol(conn, _sym_row("PAST", past))

        def fake_earnings(sym: str) -> tuple[date | None, str | None]:
            return None, None

        monkeypatch.setattr(runner, "fetch_next_earnings", fake_earnings)

        updated, candidates = runner.refresh_earnings_dates(
            conn, retries=0, max_workers=1
        )
        assert (updated, candidates) == (0, 1)
        assert repo.get_symbol(conn, "PAST").next_earnings == past
    finally:
        conn.close()


def test_refresh_earnings_dates_scope_all_counts_only_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = open_memory()
    try:
        keep = date.today() + timedelta(days=10)
        repo.upsert_symbol(conn, _sym_row("SAME", keep))
        repo.upsert_symbol(conn, _sym_row("MOVED", date.today() + timedelta(days=20)))
        moved_to = date.today() + timedelta(days=25)

        def fake_earnings(sym: str) -> tuple[date | None, str | None]:
            return (keep if sym == "SAME" else moved_to), "BMO"

        monkeypatch.setattr(runner, "fetch_next_earnings", fake_earnings)

        updated, candidates = runner.refresh_earnings_dates(
            conn, scope="all", max_workers=2
        )
        assert (updated, candidates) == (1, 2)
        assert repo.get_symbol(conn, "MOVED").next_earnings == moved_to
    finally:
        conn.close()
