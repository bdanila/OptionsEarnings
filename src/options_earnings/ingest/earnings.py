from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def _to_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        ts = pd.Timestamp(v)
    except (ValueError, TypeError):
        return None
    if pd.isna(ts):
        return None
    return ts.date()


def _extract_when(cal: Any) -> str | None:
    if not isinstance(cal, dict):
        return None
    raw = cal.get("Earnings Call Time") or cal.get("Earnings Time")
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if not text:
        return None
    if "BEFORE" in text or text == "BMO":
        return "BMO"
    if "AFTER" in text or text == "AMC":
        return "AMC"
    return None


def fetch_next_earnings(symbol: str) -> tuple[date | None, str | None]:
    try:
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
    except Exception as e:  # noqa: BLE001
        logger.warning("calendar fetch failed for %s: %s", symbol, e)
        return None, None

    today = date.today()
    next_date: date | None = None

    if isinstance(cal, dict):
        raw = cal.get("Earnings Date")
        candidates: list[Any]
        if raw is None:
            candidates = []
        elif isinstance(raw, (list, tuple)):
            candidates = list(raw)
        else:
            candidates = [raw]
        valid_dates = [d for d in (_to_date(c) for c in candidates) if d is not None]
        future_dates = [d for d in valid_dates if d >= today]
        if future_dates:
            next_date = min(future_dates)
        elif valid_dates:
            next_date = min(valid_dates)
    elif isinstance(cal, pd.DataFrame) and not cal.empty:
        try:
            row = cal.loc["Earnings Date"] if "Earnings Date" in cal.index else None
            if row is not None:
                values = list(row.values) if hasattr(row, "values") else [row]
                valid_dates = [d for d in (_to_date(v) for v in values) if d is not None]
                future_dates = [d for d in valid_dates if d >= today]
                next_date = min(future_dates) if future_dates else (
                    min(valid_dates) if valid_dates else None
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("calendar parse failed for %s: %s", symbol, e)

    when = _extract_when(cal) if isinstance(cal, dict) else None
    return next_date, when
