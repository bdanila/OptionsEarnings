from __future__ import annotations

import threading
from datetime import date, datetime
from pathlib import Path

import pytest

from options_earnings.db import repo
from options_earnings.db.connection import open_db
from options_earnings.db.repo import QuoteRow, SymbolRow
from options_earnings.options import job as job_mod


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "t.duckdb"
    conn = open_db(p)
    for sym in ["AAA", "BBB", "CCC", "DDD"]:
        repo.upsert_symbol(conn, SymbolRow(
            symbol=sym, company_name=sym, sector="Tech",
            market_cap=1_000_000_000, last_price=100.0,
            next_earnings=date(2026, 10, 1), earnings_when="AMC",
            refreshed_at=datetime(2026, 9, 15, 12, 0),
        ))
    conn.close()
    return p


def _quote(job_id, symbol: str, ts: datetime) -> QuoteRow:
    return QuoteRow(
        job_id=job_id, symbol=symbol, snapshot_ts=ts, underlying=100.0,
        expiry=date(2026, 10, 16), strike=100.0, cp="C",
        bid=1.0, ask=1.1, last=1.05, volume=10, open_interest=10,
        iv_yahoo=0.2, iv_computed=0.21,
    )


def test_run_chain_job_parallel_fetches_concurrently(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With workers>1 the network half must actually overlap, and every
    symbol's rows must still land in the DB."""
    barrier = threading.Barrier(4, timeout=10)

    def fake_fetch(symbol, *, window, target_expiry, risk_free_rate, snapshot_ts, job_id):
        barrier.wait()  # deadlocks unless all four run at once
        return [_quote(job_id, symbol, snapshot_ts)]

    monkeypatch.setattr(job_mod, "fetch_chain_slice", fake_fetch)
    monkeypatch.setattr(job_mod, "_resolve_risk_free_rate", lambda x: 0.05)

    conn = open_db(db_path)
    job_id = repo.create_job(conn, ["AAA", "BBB", "CCC", "DDD"], window_size=20)
    conn.close()

    job_mod.run_chain_job(
        db_path, job_id, window=20, skip_earnings_history=True, workers=4
    )

    conn = open_db(db_path)
    try:
        assert repo.get_job(conn, job_id).status == "done"
        got = conn.execute(
            "SELECT DISTINCT symbol FROM option_quotes ORDER BY symbol"
        ).fetchall()
        assert [r[0] for r in got] == ["AAA", "BBB", "CCC", "DDD"]
    finally:
        conn.close()


def test_run_chain_job_parallel_isolates_per_symbol_failures(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_fetch(symbol, *, window, target_expiry, risk_free_rate, snapshot_ts, job_id):
        if symbol == "BBB":
            raise RuntimeError("boom")
        return [_quote(job_id, symbol, snapshot_ts)]

    monkeypatch.setattr(job_mod, "fetch_chain_slice", fake_fetch)
    monkeypatch.setattr(job_mod, "_resolve_risk_free_rate", lambda x: 0.05)

    conn = open_db(db_path)
    job_id = repo.create_job(conn, ["AAA", "BBB", "CCC"], window_size=20)
    conn.close()

    job_mod.run_chain_job(
        db_path, job_id, window=20, skip_earnings_history=True, workers=3
    )

    conn = open_db(db_path)
    try:
        j = repo.get_job(conn, job_id)
        assert j.status == "done"          # partial success is not a job failure
        assert "BBB: boom" in j.error
        got = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM option_quotes ORDER BY symbol").fetchall()]
        assert got == ["AAA", "CCC"]
    finally:
        conn.close()


def test_run_chain_job_sequential_still_works(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_fetch(symbol, *, window, target_expiry, risk_free_rate, snapshot_ts, job_id):
        calls.append(symbol)
        return [_quote(job_id, symbol, snapshot_ts)]

    monkeypatch.setattr(job_mod, "fetch_chain_slice", fake_fetch)
    monkeypatch.setattr(job_mod, "_resolve_risk_free_rate", lambda x: 0.05)

    conn = open_db(db_path)
    job_id = repo.create_job(conn, ["AAA", "BBB", "CCC"], window_size=20)
    conn.close()

    job_mod.run_chain_job(db_path, job_id, window=20, skip_earnings_history=True)
    assert calls == ["AAA", "BBB", "CCC"]  # default workers=1 preserves order


def test_run_chain_job_reports_symbols_that_produced_nothing(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty chain is not an exception — delisted tickers just return []. The
    caller needs them distinguished from real successes to stop re-picking them.
    """
    def fake_fetch(symbol, *, window, target_expiry, risk_free_rate, snapshot_ts, job_id):
        if symbol in ("BBB", "CCC"):
            return []                      # delisted / no listed options
        return [_quote(job_id, symbol, snapshot_ts)]

    monkeypatch.setattr(job_mod, "fetch_chain_slice", fake_fetch)
    monkeypatch.setattr(job_mod, "_resolve_risk_free_rate", lambda x: 0.05)

    conn = open_db(db_path)
    job_id = repo.create_job(conn, ["AAA", "BBB", "CCC", "DDD"], window_size=20)
    conn.close()

    produced = job_mod.run_chain_job(
        db_path, job_id, window=20, skip_earnings_history=True, workers=2
    )
    assert sorted(produced) == ["AAA", "DDD"]
