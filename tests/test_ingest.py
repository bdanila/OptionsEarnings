from __future__ import annotations

from datetime import date

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
