from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

import pandas as pd

from options_earnings.options.chain import _parse_chain_frame, _slice_strikes, pick_expiry
from options_earnings.options.chain import _ChainPayload


class _FakeTicker:
    def __init__(self, options):
        self.options = options


def test_slice_strikes_centered():
    strikes = [90.0, 95.0, 100.0, 105.0, 110.0, 115.0, 120.0]
    out = _slice_strikes(strikes, underlying=105.0, window=4)
    assert out == [95.0, 100.0, 105.0, 110.0, 115.0]


def test_slice_strikes_picks_closest_atm_when_between():
    strikes = [90.0, 100.0, 110.0]
    # 104 is closer to 100 than 110
    out = _slice_strikes(strikes, underlying=104.0, window=2)
    assert 100.0 in out
    # 106 is closer to 110
    out2 = _slice_strikes(strikes, underlying=106.0, window=2)
    assert 110.0 in out2


def test_slice_strikes_atm_at_lower_edge():
    strikes = [100.0, 105.0, 110.0, 115.0, 120.0]
    out = _slice_strikes(strikes, underlying=99.0, window=4)
    # ATM clipped to 100, half=2, lo=0, hi=3
    assert out == [100.0, 105.0, 110.0]


def test_slice_strikes_atm_at_upper_edge():
    strikes = [100.0, 105.0, 110.0, 115.0, 120.0]
    out = _slice_strikes(strikes, underlying=125.0, window=4)
    assert out == [110.0, 115.0, 120.0]


def test_slice_strikes_fewer_strikes_than_window():
    strikes = [100.0, 105.0]
    out = _slice_strikes(strikes, underlying=102.0, window=20)
    assert out == [100.0, 105.0]


def test_slice_strikes_empty():
    assert _slice_strikes([], underlying=100.0, window=4) == []


def test_slice_strikes_zero_window():
    assert _slice_strikes([100.0, 105.0], underlying=100.0, window=0) == []


def test_pick_expiry_strictly_after_earnings():
    t = _FakeTicker(["2026-05-13", "2026-05-20", "2026-05-22", "2026-05-29"])
    # earnings on 2026-05-20 should pick 2026-05-22, NOT 2026-05-20 itself
    assert pick_expiry(t, date(2026, 5, 20)) == "2026-05-22"


def test_pick_expiry_skips_pre_earnings():
    t = _FakeTicker(["2026-05-13", "2026-05-22", "2026-05-29"])
    assert pick_expiry(t, date(2026, 5, 20)) == "2026-05-22"


def test_pick_expiry_none_target_returns_nearest():
    t = _FakeTicker(["2026-05-13", "2026-05-22"])
    assert pick_expiry(t, None) == "2026-05-13"


def test_pick_expiry_no_post_earnings_returns_none():
    t = _FakeTicker(["2026-05-13"])
    assert pick_expiry(t, date(2026, 5, 20)) is None


def test_pick_expiry_empty_list_returns_none():
    assert pick_expiry(_FakeTicker([]), date(2026, 5, 20)) is None
    assert pick_expiry(_FakeTicker(None), date(2026, 5, 20)) is None


def test_parse_chain_frame_builds_quote_rows_and_iv():
    job_id = uuid4()
    snapshot_ts = datetime(2026, 4, 28, 15, 0, 0)
    expiry = date(2026, 7, 28)  # ~3 months
    underlying = 100.0
    r = 0.05

    # Use a tight bid/ask around the BS price for sigma=0.25 so iv inverts.
    from options_earnings.options.iv import bs_price

    sigma = 0.25
    T = (expiry - snapshot_ts.date()).days / 365.0
    strikes = [90.0, 95.0, 100.0, 105.0, 110.0]
    call_rows = []
    put_rows = []
    for k in strikes:
        c_price = bs_price(underlying, k, T, r, sigma, "C")
        p_price = bs_price(underlying, k, T, r, sigma, "P")
        call_rows.append({
            "strike": k,
            "bid": c_price - 0.01,
            "ask": c_price + 0.01,
            "lastPrice": c_price,
            "volume": 100,
            "openInterest": 500,
            "impliedVolatility": 0.24,
        })
        put_rows.append({
            "strike": k,
            "bid": p_price - 0.01,
            "ask": p_price + 0.01,
            "lastPrice": p_price,
            "volume": 50,
            "openInterest": 250,
            "impliedVolatility": 0.26,
        })

    payload = _ChainPayload(calls=pd.DataFrame(call_rows), puts=pd.DataFrame(put_rows))
    rows = _parse_chain_frame(
        payload,
        symbol="TEST",
        underlying=underlying,
        expiry=expiry,
        snapshot_ts=snapshot_ts,
        risk_free_rate=r,
        window=4,
        job_id=job_id,
    )
    # window=4 -> half=2, ATM idx for 100 in [90,95,100,105,110] is 2 -> [0:5] all 5 strikes
    by_strike_cp = {(q.strike, q.cp): q for q in rows}
    assert len(rows) == 10
    # Recovered IV close to 0.25 for each
    for k in strikes:
        for cp in ("C", "P"):
            q = by_strike_cp[(k, cp)]
            assert q.iv_computed is not None
            assert abs(q.iv_computed - sigma) < 1e-3
            assert q.iv_yahoo is not None
            assert q.job_id == job_id
            assert q.symbol == "TEST"
            assert q.expiry == expiry
            assert q.underlying == underlying


def test_parse_chain_frame_t_zero_sets_iv_none():
    job_id = uuid4()
    snapshot_ts = datetime(2026, 4, 28, 15, 0, 0)
    expiry = date(2026, 4, 28)  # T == 0
    payload = _ChainPayload(
        calls=pd.DataFrame([{
            "strike": 100.0,
            "bid": 1.0,
            "ask": 1.1,
            "lastPrice": 1.05,
            "volume": 10,
            "openInterest": 50,
            "impliedVolatility": 0.2,
        }]),
        puts=pd.DataFrame([{
            "strike": 100.0,
            "bid": 1.0,
            "ask": 1.1,
            "lastPrice": 1.05,
            "volume": 10,
            "openInterest": 50,
            "impliedVolatility": 0.2,
        }]),
    )
    rows = _parse_chain_frame(
        payload,
        symbol="TEST",
        underlying=100.0,
        expiry=expiry,
        snapshot_ts=snapshot_ts,
        risk_free_rate=0.05,
        window=2,
        job_id=job_id,
    )
    assert all(q.iv_computed is None for q in rows)


def test_parse_chain_frame_handles_missing_quotes():
    job_id = uuid4()
    snapshot_ts = datetime(2026, 4, 28, 15, 0, 0)
    expiry = date(2026, 7, 28)
    payload = _ChainPayload(
        calls=pd.DataFrame([{
            "strike": 100.0,
            "bid": None,
            "ask": None,
            "lastPrice": None,
            "volume": None,
            "openInterest": None,
            "impliedVolatility": None,
        }]),
        puts=pd.DataFrame(columns=["strike", "bid", "ask", "lastPrice", "volume", "openInterest", "impliedVolatility"]),
    )
    rows = _parse_chain_frame(
        payload,
        symbol="TEST",
        underlying=100.0,
        expiry=expiry,
        snapshot_ts=snapshot_ts,
        risk_free_rate=0.05,
        window=2,
        job_id=job_id,
    )
    assert len(rows) == 1
    q = rows[0]
    assert q.iv_computed is None
    assert q.bid is None and q.ask is None and q.last is None
