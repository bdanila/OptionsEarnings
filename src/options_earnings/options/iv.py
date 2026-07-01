from __future__ import annotations

import logging
import math

from scipy.optimize import brentq
from scipy.stats import norm

logger = logging.getLogger(__name__)

_SIGMA_LOWER = 1e-4
_SIGMA_UPPER = 5.0


def bs_price(S: float, K: float, T: float, r: float, sigma: float, cp: str) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        if cp == "C":
            return max(S - K * math.exp(-r * T), 0.0)
        return max(K * math.exp(-r * T) - S, 0.0)
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if cp == "C":
        return float(S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2))
    if cp == "P":
        return float(K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))
    raise ValueError(f"cp must be 'C' or 'P', got {cp!r}")


def implied_vol(
    price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    cp: str,
    *,
    q: float = 0.0,
) -> float | None:
    if q != 0.0:
        # v1 only handles q=0.0; reject anything else explicitly
        return None
    if T <= 0 or price is None or price <= 0 or S <= 0 or K <= 0:
        return None
    if cp == "C":
        intrinsic = max(S - K * math.exp(-r * T), 0.0)
    elif cp == "P":
        intrinsic = max(K * math.exp(-r * T) - S, 0.0)
    else:
        return None
    if price < intrinsic - 1e-8:
        return None

    def objective(sigma: float) -> float:
        return bs_price(S, K, T, r, sigma, cp) - price

    try:
        f_lo = objective(_SIGMA_LOWER)
        f_hi = objective(_SIGMA_UPPER)
        if f_lo * f_hi > 0:
            return None
        return float(brentq(objective, _SIGMA_LOWER, _SIGMA_UPPER, maxiter=200, xtol=1e-8))
    except (ValueError, RuntimeError) as exc:
        logger.debug("implied_vol solver failed: %s", exc)
        return None
