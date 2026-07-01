from __future__ import annotations

import logging
from io import StringIO
from typing import Any

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_USER_AGENT = (
    "OptionsEarnings/0.1 (https://github.com/local; contact: local) "
    "python-httpx"
)


def normalize_symbol(symbol: str) -> str:
    """Map Wikipedia ticker conventions to Yahoo's (e.g. BRK.B -> BRK-B)."""
    return symbol.strip().upper().replace(".", "-")


def _pick_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    cols = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in cols:
            return cols[cand.lower()]
    return None


def fetch_sp500_constituents() -> list[dict[str, Any]]:
    resp = httpx.get(_WIKI_URL, headers={"User-Agent": _USER_AGENT}, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    if not tables:
        raise RuntimeError("Wikipedia returned no tables for S&P 500 constituents")
    df = tables[0]

    sym_col = _pick_column(df, ["Symbol", "Ticker symbol", "Ticker"])
    name_col = _pick_column(df, ["Security", "Company"])
    sector_col = _pick_column(df, ["GICS Sector", "Sector"])
    if sym_col is None or name_col is None:
        raise RuntimeError(
            f"Unexpected Wikipedia table columns: {list(df.columns)!r}"
        )

    out: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        raw_sym = row[sym_col]
        if pd.isna(raw_sym):
            continue
        symbol = normalize_symbol(str(raw_sym))
        if not symbol:
            continue
        company_name = str(row[name_col]).strip() if not pd.isna(row[name_col]) else ""
        sector: str | None = None
        if sector_col is not None and not pd.isna(row[sector_col]):
            sector = str(row[sector_col]).strip()
        out.append(
            {"symbol": symbol, "company_name": company_name, "sector": sector}
        )
    logger.info("Fetched %d S&P 500 constituents from Wikipedia", len(out))
    return out
