from __future__ import annotations

from datetime import date

import pandas as pd

from options_earnings.ingest.earnings_history import _last_past_earnings_date, compute_move


def _hist(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    """rows = [(date_str, close, high, low), ...]"""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _, _, _ in rows])
    return pd.DataFrame(
        {
            "Close": [r[1] for r in rows],
            "High":  [r[2] for r in rows],
            "Low":   [r[3] for r in rows],
        },
        index=idx,
    )


def test_compute_move_basic():
    earnings = date(2026, 5, 1)
    hist = _hist([
        ("2026-04-29", 100.0, 100.5, 99.0),
        ("2026-04-30", 102.0, 103.0, 101.0),
        ("2026-05-01", 108.0, 110.0, 105.0),
        ("2026-05-04", 112.0, 114.0, 109.0),
        ("2026-05-05", 110.0, 115.0, 108.0),
    ])
    move = compute_move("AAPL", earnings, hist)
    assert move is not None
    assert move.ref_close == 102.0
    assert abs(move.max_up_3d_pct - (115.0 - 102.0) / 102.0 * 100.0) < 1e-9
    assert abs(move.max_down_3d_pct - (105.0 - 102.0) / 102.0 * 100.0) < 1e-9
    assert move.window_high_3d == 115.0
    assert move.window_low_3d == 105.0
    # 3-day window: 5/1, 5/4, 5/5 -> last close 110.0
    assert move.window_close_3d == 110.0
    assert abs(move.window_close_pct_3d - (110.0 - 102.0) / 102.0 * 100.0) < 1e-9


def test_compute_move_negative_max_down():
    earnings = date(2026, 5, 1)
    hist = _hist([
        ("2026-04-30", 100.0, 101.0, 99.0),
        ("2026-05-01", 95.0, 99.0, 92.0),
        ("2026-05-04", 93.0, 95.0, 90.0),
        ("2026-05-05", 94.0, 96.0, 91.0),
    ])
    move = compute_move("XYZ", earnings, hist)
    assert move is not None
    assert move.ref_close == 100.0
    assert abs(move.max_up_3d_pct - (99.0 - 100.0) / 100.0 * 100.0) < 1e-9
    assert abs(move.max_down_3d_pct - (90.0 - 100.0) / 100.0 * 100.0) < 1e-9
    assert move.max_down_3d_pct < 0


def test_compute_move_no_prior_day_returns_none():
    earnings = date(2026, 5, 1)
    hist = _hist([
        ("2026-05-01", 100.0, 102.0, 98.0),
        ("2026-05-04", 101.0, 103.0, 99.0),
    ])
    assert compute_move("X", earnings, hist) is None


def test_compute_move_empty_returns_none():
    assert compute_move("X", date(2026, 5, 1), pd.DataFrame()) is None


def test_compute_move_window_takes_first_three():
    earnings = date(2026, 5, 1)
    hist = _hist([
        ("2026-04-30", 100.0, 100.0, 100.0),
        ("2026-05-01", 100.0, 110.0, 95.0),
        ("2026-05-04", 100.0, 112.0, 94.0),
        ("2026-05-05", 100.0, 113.0, 93.0),
        ("2026-05-06", 100.0, 999.0, 1.0),
    ])
    move = compute_move("X", earnings, hist)
    assert move is not None
    assert abs(move.max_up_3d_pct - 13.0) < 1e-9
    assert abs(move.max_down_3d_pct - (-7.0)) < 1e-9


def test_last_past_earnings_date_filters_future(monkeypatch):
    import options_earnings.ingest.earnings_history as mod
    monkeypatch.setattr(mod, "date", type("D", (), {"today": staticmethod(lambda: date(2026, 5, 5))}))
    idx = pd.DatetimeIndex([pd.Timestamp("2026-02-01"), pd.Timestamp("2026-05-04"), pd.Timestamp("2026-08-01")])
    df = pd.DataFrame({"EPS": [1, 2, 3]}, index=idx)
    assert _last_past_earnings_date(df) == date(2026, 5, 4)


def test_last_past_earnings_date_returns_none_when_empty():
    assert _last_past_earnings_date(None) is None
    assert _last_past_earnings_date(pd.DataFrame()) is None


def test_compute_move_from_ohlc_window_2():
    from datetime import date
    from options_earnings.db.repo import OHLCRow
    from options_earnings.ingest.earnings_history import compute_move_from_ohlc

    ohlc = [
        OHLCRow("X", date(2026, 4, 30), 100.0, 102.0, 99.0, 101.0),
        OHLCRow("X", date(2026, 5, 1), 101.0, 110.0, 95.0, 108.0),
        OHLCRow("X", date(2026, 5, 4), 108.0, 112.0, 105.0, 110.0),
        OHLCRow("X", date(2026, 5, 5), 110.0, 120.0, 90.0, 92.0),
        OHLCRow("X", date(2026, 5, 6), 92.0, 999.0, 1.0, 94.0),
    ]
    move = compute_move_from_ohlc("X", date(2026, 5, 1), window_trading_days=2, ohlc=ohlc)
    assert move is not None
    assert move.ref_close == 101.0
    # window = first 2 trading days >= 2026-05-01: 5/1 and 5/4
    # high = max(110, 112) = 112; low = min(95, 105) = 95; close = 5/4 close = 110
    assert move.window_high_3d == 112.0
    assert move.window_low_3d == 95.0
    assert move.window_close_3d == 110.0
    assert abs(move.max_up_3d_pct - (112.0 - 101.0) / 101.0 * 100.0) < 1e-9
    assert abs(move.max_down_3d_pct - (95.0 - 101.0) / 101.0 * 100.0) < 1e-9
    assert abs(move.window_close_pct_3d - (110.0 - 101.0) / 101.0 * 100.0) < 1e-9


def test_compute_move_from_ohlc_no_prior_returns_none():
    from datetime import date
    from options_earnings.db.repo import OHLCRow
    from options_earnings.ingest.earnings_history import compute_move_from_ohlc

    ohlc = [OHLCRow("X", date(2026, 5, 1), 100.0, 110.0, 90.0, 105.0)]
    assert compute_move_from_ohlc("X", date(2026, 5, 1), 3, ohlc) is None


def test_compute_move_from_ohlc_zero_window_returns_none():
    from datetime import date
    from options_earnings.db.repo import OHLCRow
    from options_earnings.ingest.earnings_history import compute_move_from_ohlc

    ohlc = [
        OHLCRow("X", date(2026, 4, 30), None, 102.0, 99.0, 101.0),
        OHLCRow("X", date(2026, 5, 1), None, 110.0, 95.0, 108.0),
    ]
    assert compute_move_from_ohlc("X", date(2026, 5, 1), 0, ohlc) is None
