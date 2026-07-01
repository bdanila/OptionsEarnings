from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from options_earnings.db.repo import EarningsMoveRow, OHLCRow

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def compute_move(
    symbol: str,
    earnings_date: date,
    history: pd.DataFrame,
) -> EarningsMoveRow | None:
    """Pure computation: given an OHLC DataFrame covering days around earnings_date,
    return the 3-trading-day move row. ``history`` must be indexed by DatetimeIndex
    and have columns ``Close``, ``High``, ``Low``.

    Reference: close of the last trading day strictly before earnings_date.
    Window: the first 3 trading days at-or-after earnings_date.
    """
    if history is None or history.empty:
        return None
    if not {"Close", "High", "Low"}.issubset(history.columns):
        return None

    idx_dates = [(i, ts.date() if hasattr(ts, "date") else ts) for i, ts in enumerate(history.index)]

    prior = [(i, d) for i, d in idx_dates if d < earnings_date]
    if not prior:
        return None
    ref_idx = max(prior, key=lambda x: x[1])[0]
    ref_close = float(history.iloc[ref_idx]["Close"])
    if ref_close <= 0:
        return None

    post = sorted([(i, d) for i, d in idx_dates if d >= earnings_date], key=lambda x: x[1])[:3]
    if not post:
        return None

    highs = [float(history.iloc[i]["High"]) for i, _ in post]
    lows = [float(history.iloc[i]["Low"]) for i, _ in post]
    window_high = max(highs)
    window_low = min(lows)
    window_close = float(history.iloc[post[-1][0]]["Close"])

    return EarningsMoveRow(
        symbol=symbol,
        earnings_date=earnings_date,
        ref_close=ref_close,
        max_up_3d_pct=(window_high - ref_close) / ref_close * 100.0,
        max_down_3d_pct=(window_low - ref_close) / ref_close * 100.0,
        computed_at=_utcnow_naive(),
        window_high_3d=window_high,
        window_low_3d=window_low,
        window_close_3d=window_close,
        window_close_pct_3d=(window_close - ref_close) / ref_close * 100.0,
    )


def _past_earnings_dates(earnings_dates_df: Any, limit: int) -> list[date]:
    if earnings_dates_df is None or len(earnings_dates_df) == 0:
        return []
    today = date.today()
    past: list[date] = []
    for ts in earnings_dates_df.index:
        try:
            d = ts.date() if hasattr(ts, "date") else ts
        except Exception:
            continue
        if d < today:
            past.append(d)
    past.sort(reverse=True)
    return past[:limit]


def _last_past_earnings_date(earnings_dates_df: Any) -> date | None:
    dates = _past_earnings_dates(earnings_dates_df, 1)
    return dates[0] if dates else None


def compute_last_earnings_move(symbol: str) -> EarningsMoveRow | None:
    moves = compute_recent_earnings_moves(symbol, n=1)
    return moves[0] if moves else None


def _df_to_ohlc_rows(symbol: str, df: pd.DataFrame) -> list[OHLCRow]:
    if df is None or df.empty:
        return []
    out: list[OHLCRow] = []
    for ts, row in df.iterrows():
        d = ts.date() if hasattr(ts, "date") else ts
        try:
            out.append(OHLCRow(
                symbol=symbol,
                trading_day=d,
                open=float(row["Open"]) if "Open" in row and pd.notna(row["Open"]) else None,
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def compute_recent_earnings_moves(symbol: str, n: int = 4) -> list[EarningsMoveRow]:
    """Fetch earnings dates + OHLC from yfinance and compute up to ``n`` most
    recent past earnings moves with the legacy fixed 3-day window. One
    yfinance history call per symbol.
    """
    moves, _ = compute_recent_earnings_data(symbol, n=n)
    return moves


def compute_recent_earnings_data(
    symbol: str, n: int = 8, *, ohlc_post_buffer_days: int = 35
) -> tuple[list[EarningsMoveRow], list[OHLCRow]]:
    """Fetch one big history slice covering the last ``n`` past earnings dates,
    return (3-day moves, raw OHLC rows). The OHLC buffer post-earnings is wide
    enough (default 35 calendar days) to support recomputing the move for any
    reasonable post-earnings window.
    """
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    try:
        ed = ticker.earnings_dates
    except Exception as exc:
        logger.warning("yfinance earnings_dates failed for %s: %s", symbol, exc)
        return [], []

    earnings = _past_earnings_dates(ed, limit=n)
    if not earnings:
        return [], []

    span_start = min(earnings) - timedelta(days=10)
    span_end = max(earnings) + timedelta(days=ohlc_post_buffer_days)
    try:
        hist = ticker.history(start=span_start, end=span_end, auto_adjust=False)
    except Exception as exc:
        logger.warning("yfinance history failed for %s: %s", symbol, exc)
        return [], []

    moves: list[EarningsMoveRow] = []
    for ed_date in earnings:
        move = compute_move(symbol, ed_date, hist)
        if move is not None:
            moves.append(move)
    return moves, _df_to_ohlc_rows(symbol, hist)


def compute_move_from_ohlc(
    symbol: str,
    earnings_date: date,
    window_trading_days: int,
    ohlc: list[OHLCRow],
) -> EarningsMoveRow | None:
    """Pure recompute from stored OHLC rows. ``ohlc`` must be all rows for the
    symbol, sorted ascending by trading_day.
    """
    if window_trading_days <= 0:
        return None
    prior = [r for r in ohlc if r.trading_day < earnings_date]
    if not prior:
        return None
    ref_close = prior[-1].close
    if ref_close <= 0:
        return None
    post = [r for r in ohlc if r.trading_day >= earnings_date][:window_trading_days]
    if not post:
        return None
    window_high = max(r.high for r in post)
    window_low = min(r.low for r in post)
    window_close = post[-1].close
    return EarningsMoveRow(
        symbol=symbol,
        earnings_date=earnings_date,
        ref_close=ref_close,
        max_up_3d_pct=(window_high - ref_close) / ref_close * 100.0,
        max_down_3d_pct=(window_low - ref_close) / ref_close * 100.0,
        computed_at=_utcnow_naive(),
        window_high_3d=window_high,
        window_low_3d=window_low,
        window_close_3d=window_close,
        window_close_pct_3d=(window_close - ref_close) / ref_close * 100.0,
    )
