from __future__ import annotations

import math

from options_earnings.options.iv import bs_price, implied_vol


def test_bs_price_at_the_money_call_put_parity():
    S = 100.0
    K = 100.0
    r = 0.05
    T = 0.5
    sigma = 0.25
    call = bs_price(S, K, T, r, sigma, "C")
    put = bs_price(S, K, T, r, sigma, "P")
    parity_lhs = call - put
    parity_rhs = S - K * math.exp(-r * T)
    assert abs(parity_lhs - parity_rhs) < 1e-8


def test_implied_vol_recovers_input():
    S = 100.0
    K = 105.0
    r = 0.03
    T = 0.4
    sigma = 0.25
    price_c = bs_price(S, K, T, r, sigma, "C")
    price_p = bs_price(S, K, T, r, sigma, "P")
    iv_c = implied_vol(price_c, S, K, T, r, "C")
    iv_p = implied_vol(price_p, S, K, T, r, "P")
    assert iv_c is not None and abs(iv_c - sigma) < 1e-4
    assert iv_p is not None and abs(iv_p - sigma) < 1e-4


def test_implied_vol_returns_none_for_invalid():
    # below intrinsic
    S = 100.0
    K = 80.0
    r = 0.05
    T = 0.5
    intrinsic = S - K * math.exp(-r * T)
    below = max(0.0, intrinsic - 5.0)
    assert implied_vol(below, S, K, T, r, "C") is None
    # T <= 0
    assert implied_vol(5.0, 100.0, 100.0, 0.0, 0.05, "C") is None
    assert implied_vol(5.0, 100.0, 100.0, -0.1, 0.05, "C") is None
    # invalid cp
    assert implied_vol(5.0, 100.0, 100.0, 0.5, 0.05, "X") is None
    # non-zero dividend yield (v1 unsupported)
    assert implied_vol(5.0, 100.0, 100.0, 0.5, 0.05, "C", q=0.02) is None
