from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import UUID

from options_earnings.db import repo
from options_earnings.db.repo import QuoteRow
from options_earnings.options.history import (
    iv_history_fixed_strike,
    iv_history_rolling_atm,
    nearest_strike_today,
)


def _q(
    job_id: UUID,
    *,
    snapshot_ts: datetime,
    underlying: float,
    strike: float,
    iv: float,
    cp: str = "C",
    expiry: date = date(2026, 5, 9),
) -> QuoteRow:
    return QuoteRow(
        job_id=job_id,
        symbol="AAPL",
        snapshot_ts=snapshot_ts,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        cp=cp,
        bid=1.0,
        ask=1.1,
        last=1.05,
        volume=100,
        open_interest=500,
        iv_yahoo=0.24,
        iv_computed=iv,
    )


def test_rolling_atm_picks_strike_closest_to_underlying_per_snapshot(conn):
    j0 = repo.create_job(conn, ["AAPL"], 5)
    j1 = repo.create_job(conn, ["AAPL"], 5)
    j2 = repo.create_job(conn, ["AAPL"], 5)
    t0 = datetime(2026, 4, 28, 15, 0, 0)
    t1 = t0 + timedelta(hours=1)
    t2 = t0 + timedelta(hours=2)

    rows = [
        _q(j0, snapshot_ts=t0, underlying=100.0, strike=95.0, iv=0.40),
        _q(j0, snapshot_ts=t0, underlying=100.0, strike=100.0, iv=0.20),
        _q(j0, snapshot_ts=t0, underlying=100.0, strike=105.0, iv=0.50),
        _q(j1, snapshot_ts=t1, underlying=104.0, strike=95.0, iv=0.45),
        _q(j1, snapshot_ts=t1, underlying=104.0, strike=100.0, iv=0.35),
        _q(j1, snapshot_ts=t1, underlying=104.0, strike=105.0, iv=0.22),
        _q(j2, snapshot_ts=t2, underlying=92.0, strike=95.0, iv=0.30),
        _q(j2, snapshot_ts=t2, underlying=92.0, strike=100.0, iv=0.55),
        _q(j2, snapshot_ts=t2, underlying=92.0, strike=105.0, iv=0.60),
    ]
    repo.insert_quotes(conn, rows)

    series = iv_history_rolling_atm(conn, "AAPL", cp="C")
    assert len(series) == 3
    assert [s["snapshot_ts"] for s in series] == [t0, t1, t2]
    assert [s["strike"] for s in series] == [100.0, 105.0, 95.0]
    assert [s["iv_computed"] for s in series] == [0.20, 0.22, 0.30]


def test_rolling_atm_filters_by_expiry_and_cp(conn):
    job_id = repo.create_job(conn, ["AAPL"], 5)
    t0 = datetime(2026, 4, 28, 15, 0, 0)
    e1 = date(2026, 5, 9)
    e2 = date(2026, 5, 16)

    rows = [
        _q(job_id, snapshot_ts=t0, underlying=100.0, strike=100.0, iv=0.20, cp="C", expiry=e1),
        _q(job_id, snapshot_ts=t0, underlying=100.0, strike=100.0, iv=0.30, cp="P", expiry=e1),
        _q(job_id, snapshot_ts=t0, underlying=100.0, strike=100.0, iv=0.40, cp="C", expiry=e2),
    ]
    repo.insert_quotes(conn, rows)

    s_e1_c = iv_history_rolling_atm(conn, "AAPL", expiry=e1, cp="C")
    assert len(s_e1_c) == 1
    assert s_e1_c[0]["iv_computed"] == 0.20

    s_e1_p = iv_history_rolling_atm(conn, "AAPL", expiry=e1, cp="P")
    assert len(s_e1_p) == 1
    assert s_e1_p[0]["iv_computed"] == 0.30

    s_e2_c = iv_history_rolling_atm(conn, "AAPL", expiry=e2, cp="C")
    assert len(s_e2_c) == 1
    assert s_e2_c[0]["iv_computed"] == 0.40


def test_fixed_strike_history_sorted(conn):
    j0 = repo.create_job(conn, ["AAPL"], 5)
    j1 = repo.create_job(conn, ["AAPL"], 5)
    j2 = repo.create_job(conn, ["AAPL"], 5)
    t0 = datetime(2026, 4, 28, 15, 0, 0)
    t1 = t0 + timedelta(hours=1)
    t2 = t0 + timedelta(hours=2)

    rows = [
        _q(j1, snapshot_ts=t1, underlying=104.0, strike=100.0, iv=0.35),
        _q(j0, snapshot_ts=t0, underlying=100.0, strike=100.0, iv=0.20),
        _q(j2, snapshot_ts=t2, underlying=92.0, strike=100.0, iv=0.55),
        _q(j0, snapshot_ts=t0, underlying=100.0, strike=95.0, iv=0.99),  # different strike, ignored
    ]
    repo.insert_quotes(conn, rows)

    series = iv_history_fixed_strike(conn, "AAPL", strike=100.0, cp="C")
    assert [s["snapshot_ts"] for s in series] == [t0, t1, t2]
    assert [s["iv_computed"] for s in series] == [0.20, 0.35, 0.55]


def test_nearest_strike_today_uses_latest_underlying(conn):
    j0 = repo.create_job(conn, ["AAPL"], 5)
    j1 = repo.create_job(conn, ["AAPL"], 5)
    t0 = datetime(2026, 4, 28, 15, 0, 0)
    t1 = t0 + timedelta(hours=1)

    rows = [
        _q(j0, snapshot_ts=t0, underlying=100.0, strike=100.0, iv=0.20),
        _q(j1, snapshot_ts=t1, underlying=108.0, strike=95.0, iv=0.21),
        _q(j1, snapshot_ts=t1, underlying=108.0, strike=100.0, iv=0.22),
        _q(j1, snapshot_ts=t1, underlying=108.0, strike=105.0, iv=0.23),
        _q(j1, snapshot_ts=t1, underlying=108.0, strike=110.0, iv=0.24),
    ]
    repo.insert_quotes(conn, rows)

    nearest = nearest_strike_today(conn, "AAPL", cp="C")
    assert nearest == 110.0


def test_nearest_strike_today_returns_none_when_empty(conn):
    assert nearest_strike_today(conn, "ZZZZ", cp="C") is None
