from __future__ import annotations

import logging
import math
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

import pandas as pd
import yfinance as yf

from options_earnings.db.repo import QuoteRow
from options_earnings.options.iv import implied_vol

logger = logging.getLogger(__name__)


def _slice_strikes(strikes: list[float], underlying: float, window: int) -> list[float]:
    if not strikes:
        return []
    sorted_strikes = sorted(strikes)
    if window <= 0:
        return []
    pos = bisect_left(sorted_strikes, underlying)
    if pos >= len(sorted_strikes):
        atm_idx = len(sorted_strikes) - 1
    elif pos == 0:
        atm_idx = 0
    else:
        before = sorted_strikes[pos - 1]
        after = sorted_strikes[pos]
        atm_idx = pos - 1 if abs(before - underlying) <= abs(after - underlying) else pos
    half = window // 2
    lo = max(0, atm_idx - half)
    hi = min(len(sorted_strikes), atm_idx + half + 1)
    return sorted_strikes[lo:hi]


def pick_expiry(ticker: yf.Ticker, target: date | None) -> str | None:
    """Return the first expiry strictly after ``target``.

    For earnings plays an expiry on the earnings date itself isn't useful when
    the company reports after market close — the option dies hours before the
    news. Strict ``>`` keeps the rule unambiguous across BMO/AMC.
    """
    try:
        expiries = list(ticker.options or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("ticker.options failed: %s", exc)
        return None
    if not expiries:
        return None
    expiries = sorted(expiries)
    if target is None:
        return expiries[0]
    target_iso = target.isoformat()
    after = [e for e in expiries if e > target_iso]
    if after:
        return after[0]
    return None


def _mid_or_last(bid: float | None, ask: float | None, last: float | None) -> float | None:
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if last is not None and last > 0:
        return last
    return None


def _opt(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


@dataclass
class _ChainPayload:
    calls: pd.DataFrame
    puts: pd.DataFrame


def _parse_chain_frame(
    payload: _ChainPayload,
    *,
    symbol: str,
    underlying: float,
    expiry: date,
    snapshot_ts: datetime,
    risk_free_rate: float,
    window: int,
    job_id: UUID,
) -> list[QuoteRow]:
    call_strikes = [float(s) for s in payload.calls["strike"].tolist()] if "strike" in payload.calls else []
    put_strikes = [float(s) for s in payload.puts["strike"].tolist()] if "strike" in payload.puts else []
    all_strikes = sorted(set(call_strikes) | set(put_strikes))
    sliced = set(_slice_strikes(all_strikes, underlying, window))
    if not sliced:
        return []

    days = (expiry - snapshot_ts.date()).days
    T = days / 365.0
    rows: list[QuoteRow] = []

    for cp_label, frame in (("C", payload.calls), ("P", payload.puts)):
        if frame is None or frame.empty:
            continue
        for record in frame.to_dict(orient="records"):
            strike_val = _opt(record.get("strike"))
            if strike_val is None:
                continue
            strike = float(strike_val)
            if strike not in sliced:
                continue
            bid = _opt(record.get("bid"))
            ask = _opt(record.get("ask"))
            last = _opt(record.get("lastPrice"))
            volume = _opt(record.get("volume"))
            open_interest = _opt(record.get("openInterest"))
            iv_yahoo = _opt(record.get("impliedVolatility"))

            bid_f = float(bid) if bid is not None else None
            ask_f = float(ask) if ask is not None else None
            last_f = float(last) if last is not None else None
            mid = _mid_or_last(bid_f, ask_f, last_f)

            iv_computed: float | None
            if T <= 0 or mid is None:
                iv_computed = None
            else:
                iv_computed = implied_vol(mid, underlying, strike, T, risk_free_rate, cp_label)

            rows.append(
                QuoteRow(
                    job_id=job_id,
                    symbol=symbol,
                    snapshot_ts=snapshot_ts,
                    underlying=underlying,
                    expiry=expiry,
                    strike=strike,
                    cp=cp_label,
                    bid=bid_f,
                    ask=ask_f,
                    last=last_f,
                    volume=int(volume) if volume is not None else None,
                    open_interest=int(open_interest) if open_interest is not None else None,
                    iv_yahoo=float(iv_yahoo) if iv_yahoo is not None else None,
                    iv_computed=iv_computed,
                )
            )
    return rows


def fetch_chain_slice(
    symbol: str,
    *,
    window: int,
    target_expiry: date | None,
    risk_free_rate: float,
    snapshot_ts: datetime,
    job_id: UUID,
) -> list[QuoteRow]:
    ticker = yf.Ticker(symbol)
    expiry_str = pick_expiry(ticker, target_expiry)
    if expiry_str is None:
        logger.info("no expiries available for %s", symbol)
        return []
    expiry = date.fromisoformat(expiry_str)
    fast_info = ticker.fast_info
    underlying_raw = fast_info["last_price"]
    if underlying_raw is None or (isinstance(underlying_raw, float) and math.isnan(underlying_raw)):
        logger.info("no underlying price for %s", symbol)
        return []
    underlying = float(underlying_raw)
    chain = ticker.option_chain(expiry_str)
    payload = _ChainPayload(calls=chain.calls, puts=chain.puts)
    return _parse_chain_frame(
        payload,
        symbol=symbol,
        underlying=underlying,
        expiry=expiry,
        snapshot_ts=snapshot_ts,
        risk_free_rate=risk_free_rate,
        window=window,
        job_id=job_id,
    )
