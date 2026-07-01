from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import yfinance as yf

logger = logging.getLogger(__name__)


def _coerce_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _coerce_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return int(f)


def _get_attr(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    try:
        return getattr(obj, key)
    except AttributeError:
        try:
            return obj[key]
        except (KeyError, TypeError):
            return None


def fetch_quote(symbol: str) -> dict[str, Any]:
    ticker = yf.Ticker(symbol)
    last_price: float | None = None
    market_cap: int | None = None
    try:
        fi = ticker.fast_info
        last_price = _coerce_float(
            _get_attr(fi, "last_price") or _get_attr(fi, "lastPrice")
        )
        market_cap = _coerce_int(
            _get_attr(fi, "market_cap") or _get_attr(fi, "marketCap")
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("fast_info failed for %s: %s", symbol, e)
    return {"symbol": symbol, "last_price": last_price, "market_cap": market_cap}


def fetch_quotes_batch(
    symbols: list[str], *, max_workers: int = 8
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not symbols:
        return out
    workers = max(1, min(max_workers, len(symbols)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_quote, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                out[sym] = fut.result()
            except Exception as e:  # noqa: BLE001
                logger.warning("fetch_quote failed for %s: %s", sym, e)
                out[sym] = {"symbol": sym, "last_price": None, "market_cap": None}
    return out
