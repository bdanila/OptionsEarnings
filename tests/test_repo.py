from datetime import date, datetime, timedelta

from options_earnings.db import repo
from options_earnings.db.repo import JobRow, QuoteRow, SymbolRow


def _sym(symbol="AAPL", company="Apple Inc.", price=180.0, mcap=2_800_000_000_000) -> SymbolRow:
    return SymbolRow(
        symbol=symbol,
        company_name=company,
        sector="Technology",
        market_cap=mcap,
        last_price=price,
        next_earnings=date(2026, 5, 1),
        earnings_when="AMC",
        refreshed_at=datetime(2026, 4, 28, 12, 0, 0),
    )


def test_upsert_and_get_symbol(conn):
    repo.upsert_symbol(conn, _sym())
    got = repo.get_symbol(conn, "AAPL")
    assert got is not None
    assert got.symbol == "AAPL"
    assert got.company_name == "Apple Inc."
    assert got.last_price == 180.0
    assert got.next_earnings == date(2026, 5, 1)


def test_upsert_overwrites(conn):
    repo.upsert_symbol(conn, _sym(price=180.0))
    repo.upsert_symbol(conn, _sym(price=185.5))
    got = repo.get_symbol(conn, "AAPL")
    assert got.last_price == 185.5


def test_upsert_preserves_existing_on_null_overwrite(conn):
    """If a refresh comes back with NULL price/mcap/earnings (yfinance rate
    limit), upsert should not wipe the previously-good values."""
    good = _sym(price=180.0, mcap=2_800_000_000_000)
    repo.upsert_symbol(conn, good)

    nulled = repo.SymbolRow(
        symbol="AAPL",
        company_name="Apple Inc.",
        sector="Technology",
        market_cap=None,
        last_price=None,
        next_earnings=None,
        earnings_when=None,
        refreshed_at=datetime(2026, 5, 12, 12, 0),
    )
    repo.upsert_symbol(conn, nulled)
    got = repo.get_symbol(conn, "AAPL")
    assert got.last_price == 180.0
    assert got.market_cap == 2_800_000_000_000
    assert got.next_earnings == date(2026, 5, 1)
    assert got.refreshed_at == datetime(2026, 5, 12, 12, 0)


def test_list_symbols_pagination_and_sort(conn):
    rows = [
        _sym(symbol="AAPL", company="Apple Inc.", price=180.0),
        _sym(symbol="MSFT", company="Microsoft Corp.", price=420.0),
        _sym(symbol="NVDA", company="NVIDIA Corp.", price=950.0),
        _sym(symbol="GOOG", company="Alphabet Inc.", price=170.0),
    ]
    for r in rows:
        repo.upsert_symbol(conn, r)

    page1, total = repo.list_symbols(conn, page=1, size=2, sort="symbol", dir_="asc")
    assert total == 4
    assert [s.symbol for s in page1] == ["AAPL", "GOOG"]

    page2, _ = repo.list_symbols(conn, page=2, size=2, sort="symbol", dir_="asc")
    assert [s.symbol for s in page2] == ["MSFT", "NVDA"]

    by_price_desc, _ = repo.list_symbols(conn, page=1, size=10, sort="last_price", dir_="desc")
    assert [s.symbol for s in by_price_desc] == ["NVDA", "MSFT", "AAPL", "GOOG"]


def test_list_symbols_unsafe_sort_falls_back(conn):
    repo.upsert_symbol(conn, _sym())
    rows, _ = repo.list_symbols(conn, sort="; DROP TABLE symbols; --", dir_="asc")
    assert len(rows) == 1


def test_list_symbols_filter_q_matches_symbol_or_company(conn):
    repo.upsert_symbol(conn, _sym(symbol="MSFT", company="Microsoft Corp.", price=420.0))
    repo.upsert_symbol(conn, _sym(symbol="AAPL", company="Apple Inc.", price=180.0))
    repo.upsert_symbol(conn, _sym(symbol="GOOG", company="Alphabet Inc.", price=170.0))

    rows, total = repo.list_symbols(conn, q="msft")
    assert total == 1
    assert rows[0].symbol == "MSFT"

    rows, total = repo.list_symbols(conn, q="apple")
    assert total == 1
    assert rows[0].symbol == "AAPL"

    rows, total = repo.list_symbols(conn, q="inc")
    assert total == 2


def test_list_symbols_filter_min_mcap(conn):
    repo.upsert_symbol(conn, _sym(symbol="SMALL", mcap=500_000_000))
    repo.upsert_symbol(conn, _sym(symbol="MID", mcap=50_000_000_000))
    repo.upsert_symbol(conn, _sym(symbol="LARGE", mcap=1_500_000_000_000))

    rows, total = repo.list_symbols(conn, min_mcap=10_000_000_000)
    assert total == 2
    assert {r.symbol for r in rows} == {"MID", "LARGE"}


def test_list_symbols_filter_earnings_range(conn):
    rows = [
        SymbolRow("PAST", "Past", "Tech", 1, 10.0, date(2026, 1, 5), None, datetime(2026, 4, 28)),
        SymbolRow("EARLY", "Early", "Tech", 1, 10.0, date(2026, 5, 5), None, datetime(2026, 4, 28)),
        SymbolRow("MID", "Mid", "Tech", 1, 10.0, date(2026, 5, 20), None, datetime(2026, 4, 28)),
        SymbolRow("LATE", "Late", "Tech", 1, 10.0, date(2026, 6, 30), None, datetime(2026, 4, 28)),
        SymbolRow("NULLE", "Null", "Tech", 1, 10.0, None, None, datetime(2026, 4, 28)),
    ]
    for r in rows:
        repo.upsert_symbol(conn, r)

    out, total = repo.list_symbols(conn, earnings_from=date(2026, 5, 1), earnings_to=date(2026, 5, 31))
    assert {r.symbol for r in out} == {"EARLY", "MID"}
    assert total == 2

    out, _ = repo.list_symbols(conn, earnings_from=date(2026, 6, 1))
    assert {r.symbol for r in out} == {"LATE"}

    out, _ = repo.list_symbols(conn, earnings_to=date(2026, 4, 30))
    assert {r.symbol for r in out} == {"PAST"}


def test_stale_iv_monitored_symbols_orders_never_first_then_oldest(conn):
    from datetime import datetime, timedelta, timezone

    repo.upsert_symbol(conn, _sym(symbol="NEVER"))
    repo.upsert_symbol(conn, _sym(symbol="OLD"))
    repo.upsert_symbol(conn, _sym(symbol="RECENT"))
    repo.upsert_symbol(conn, _sym(symbol="NOTMON"))  # not monitored, ignored
    repo.set_iv_monitored(conn, ["NEVER", "OLD", "RECENT"], True)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    def _q(symbol, hours_ago):
        j = repo.create_job(conn, [symbol], window_size=5)
        return QuoteRow(
            job_id=j, symbol=symbol, snapshot_ts=now - timedelta(hours=hours_ago),
            underlying=100.0, expiry=date(2026, 12, 31), strike=100.0, cp="C",
            bid=1.0, ask=1.1, last=1.05, volume=1, open_interest=1,
            iv_yahoo=0.24, iv_computed=0.25,
        )
    repo.insert_quotes(conn, [_q("OLD", 48), _q("RECENT", 1)])

    stale = repo.stale_iv_monitored_symbols(conn, limit=10)
    assert stale == ["NEVER", "OLD", "RECENT"]

    assert repo.stale_iv_monitored_symbols(conn, limit=1) == ["NEVER"]
    # NOTMON is never returned
    assert "NOTMON" not in stale


def test_set_iv_monitored_and_monitored_symbols(conn):
    repo.upsert_symbol(conn, _sym(symbol="AAPL"))
    repo.upsert_symbol(conn, _sym(symbol="MSFT"))
    repo.upsert_symbol(conn, _sym(symbol="NVDA"))
    assert repo.monitored_symbols(conn) == []

    repo.set_iv_monitored(conn, ["AAPL", "NVDA"], True)
    assert repo.monitored_symbols(conn) == ["AAPL", "NVDA"]
    # list_symbols should reflect the flag
    rows, _ = repo.list_symbols(conn)
    by_sym = {r.symbol: r for r in rows}
    assert by_sym["AAPL"].iv_monitored is True
    assert by_sym["MSFT"].iv_monitored is False
    assert by_sym["NVDA"].iv_monitored is True

    # Disabling sticks
    repo.set_iv_monitored(conn, ["AAPL"], False)
    assert repo.monitored_symbols(conn) == ["NVDA"]


def test_list_symbols_filter_iv_monitored(conn):
    repo.upsert_symbol(conn, _sym(symbol="AAPL"))
    repo.upsert_symbol(conn, _sym(symbol="MSFT"))
    repo.upsert_symbol(conn, _sym(symbol="NVDA"))
    repo.set_iv_monitored(conn, ["AAPL", "NVDA"], True)

    yes_rows, yes_total = repo.list_symbols(conn, iv_monitored=True)
    assert yes_total == 2
    assert {r.symbol for r in yes_rows} == {"AAPL", "NVDA"}

    no_rows, no_total = repo.list_symbols(conn, iv_monitored=False)
    assert no_total == 1
    assert no_rows[0].symbol == "MSFT"

    any_rows, any_total = repo.list_symbols(conn, iv_monitored=None)
    assert any_total == 3


def test_list_symbols_returns_3m_stats_and_can_sort(conn):
    from options_earnings.db.repo import OHLCRow, upsert_ohlc

    # Seed 3 symbols with different last_price so pct math is easy to verify.
    repo.upsert_symbol(conn, _sym(symbol="A", price=100.0))
    repo.upsert_symbol(conn, _sym(symbol="B", price=200.0))
    repo.upsert_symbol(conn, _sym(symbol="C", price=50.0))
    # NB: no OHLC for C — should show as NULL stats.

    from datetime import date
    today = date.today()

    # A: low 80, high 120  -> min_3m_pct = -20, max_3m_pct = +20
    # B: low 180, high 250 -> min_3m_pct = -10, max_3m_pct = +25
    upsert_ohlc(conn, [
        OHLCRow("A", today, 90.0, 120.0, 80.0, 100.0),
        OHLCRow("B", today, 190.0, 250.0, 180.0, 200.0),
    ])

    rows, _ = repo.list_symbols(conn, sort="symbol")
    by = {r.symbol: r for r in rows}
    assert abs(by["A"].min_3m_pct - (-20.0)) < 1e-9
    assert abs(by["A"].max_3m_pct - 20.0) < 1e-9
    assert abs(by["B"].min_3m_pct - (-10.0)) < 1e-9
    assert abs(by["B"].max_3m_pct - 25.0) < 1e-9
    assert by["C"].min_3m_pct is None
    assert by["C"].max_3m_pct is None

    # Sort by max_3m_pct DESC — B (25) > A (20) > C (NULL)
    sorted_rows, _ = repo.list_symbols(conn, sort="max_3m_pct", dir_="desc")
    assert [r.symbol for r in sorted_rows] == ["B", "A", "C"]

    # Sort by min_3m_pct ASC — A (-20) < B (-10) < C (NULL LAST)
    sorted_rows, _ = repo.list_symbols(conn, sort="min_3m_pct", dir_="asc")
    assert [r.symbol for r in sorted_rows] == ["A", "B", "C"]

    # Range 3M % = |min_pct| + |max_pct|. A: 20+20 = 40; B: 10+25 = 35; C: NULL
    assert abs(by["A"].range_3m_pct - 40.0) < 1e-9
    assert abs(by["B"].range_3m_pct - 35.0) < 1e-9
    assert by["C"].range_3m_pct is None

    # Sort by range ASC — B (35) < A (40) < C (NULL LAST)
    r_asc, _ = repo.list_symbols(conn, sort="range_3m_pct", dir_="asc")
    assert [r.symbol for r in r_asc] == ["B", "A", "C"]

    # Filter range_3m_pct between 30 and 38 → only B (35). NULL excluded.
    filt, total = repo.list_symbols(conn, range_3m_min=30.0, range_3m_max=38.0)
    assert total == 1
    assert filt[0].symbol == "B"

    # Only min: 36+ → only A.
    filt, total = repo.list_symbols(conn, range_3m_min=36.0)
    assert total == 1
    assert filt[0].symbol == "A"

    # Only max: 38 max → only B (A above, C NULL).
    filt, total = repo.list_symbols(conn, range_3m_max=38.0)
    assert total == 1
    assert filt[0].symbol == "B"


def test_sync_last_price_from_ohlc(conn):
    from datetime import date, timedelta
    from options_earnings.db.repo import OHLCRow, sync_last_price_from_ohlc, upsert_ohlc

    repo.upsert_symbol(conn, _sym(symbol="AIG", price=75.12))
    repo.upsert_symbol(conn, _sym(symbol="MSFT", price=100.0))
    repo.upsert_symbol(conn, _sym(symbol="NOOHLC", price=50.0))

    today = date.today()
    upsert_ohlc(conn, [
        OHLCRow("AIG", today - timedelta(days=1), 78.0, 79.5, 77.0, 78.5),
        OHLCRow("AIG", today, 78.5, 80.0, 77.5, 79.39),
        OHLCRow("MSFT", today, 405.0, 412.0, 400.0, 410.0),
    ])

    # Sync all
    n = sync_last_price_from_ohlc(conn)
    assert n == 2  # AIG + MSFT
    aig = repo.get_symbol(conn, "AIG")
    msft = repo.get_symbol(conn, "MSFT")
    noohlc = repo.get_symbol(conn, "NOOHLC")
    assert aig.last_price == 79.39
    assert msft.last_price == 410.0
    assert noohlc.last_price == 50.0  # untouched

    # Sync a subset (change AIG price, then re-sync only MSFT)
    conn.execute("UPDATE symbols SET last_price = 12.0 WHERE symbol = 'AIG'")
    n2 = sync_last_price_from_ohlc(conn, ["MSFT"])
    assert n2 == 1
    assert repo.get_symbol(conn, "AIG").last_price == 12.0  # not synced
    assert repo.get_symbol(conn, "MSFT").last_price == 410.0

    # Empty list is a no-op
    assert sync_last_price_from_ohlc(conn, []) == 0


def test_daily_candles_for_symbol_returns_sorted_last_n_days(conn):
    from datetime import date, timedelta
    from options_earnings.db.repo import OHLCRow, daily_candles_for_symbol, upsert_ohlc

    repo.upsert_symbol(conn, _sym(symbol="A"))
    today = date.today()
    upsert_ohlc(conn, [
        OHLCRow("A", today - timedelta(days=200), 1.0, 2.0, 0.5, 1.5),  # old — filtered out
        OHLCRow("A", today - timedelta(days=50), 10.0, 11.0, 9.0, 10.5),
        OHLCRow("A", today - timedelta(days=10), 20.0, 21.0, 19.0, 20.5),
        OHLCRow("A", today, 30.0, 31.0, 29.0, 30.5),
    ])
    rows = daily_candles_for_symbol(conn, "A", days=90)
    assert [r.trading_day for r in rows] == [
        today - timedelta(days=50),
        today - timedelta(days=10),
        today,
    ]

    # Different symbol → empty
    assert daily_candles_for_symbol(conn, "MISSING", days=90) == []


def test_daily_candles_progress(conn):
    from options_earnings.db.repo import OHLCRow, upsert_ohlc

    repo.upsert_symbol(conn, _sym(symbol="A"))
    repo.upsert_symbol(conn, _sym(symbol="B"))
    repo.upsert_symbol(conn, _sym(symbol="C"))

    from datetime import date, timedelta
    today = date.today()
    yesterday = today - timedelta(days=1)

    # A has today, B has only yesterday, C has nothing.
    upsert_ohlc(conn, [
        OHLCRow("A", today, 1.0, 2.0, 0.5, 1.5),
        OHLCRow("B", yesterday, 1.0, 2.0, 0.5, 1.5),
    ])

    p = repo.daily_candles_progress(conn)
    assert p["total"] == 3
    assert p["with_data"] == 2
    assert p["current"] == 1  # only A matches the max_day (today)
    assert p["latest_day"] == today


def test_capture_iv_ranks_and_alerts(conn):
    """End-to-end: quotes -> capture_iv_ranks -> iv_rank_history persisted;
    iv_rank_alerts flags symbols whose latest rank dropped > threshold."""
    from datetime import datetime, timedelta, timezone
    from options_earnings.db.repo import capture_iv_ranks, iv_rank_alerts

    repo.upsert_symbol(conn, _sym(symbol="WAT", price=100.0))
    repo.upsert_symbol(conn, _sym(symbol="CALM", price=100.0))  # stable, no alert

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def _emit(symbol, days_ago, iv):
        ts = now - timedelta(days=days_ago)
        j = repo.create_job(conn, [symbol], window_size=5)
        repo.insert_quotes(conn, [QuoteRow(
            job_id=j, symbol=symbol, snapshot_ts=ts, underlying=100.0,
            expiry=date(2026, 12, 31), strike=100.0, cp="C",
            bid=1.0, ask=1.1, last=1.05, volume=1, open_interest=1,
            iv_yahoo=iv - 0.01, iv_computed=iv,
        )])
        capture_iv_ranks(conn, [symbol], ts)

    # WAT: rank goes 0 -> 100 (peak) -> 30 (current, big drop).
    _emit("WAT", days_ago=8, iv=0.20)
    _emit("WAT", days_ago=6, iv=0.30)
    _emit("WAT", days_ago=1, iv=0.23)

    # CALM: rank barely moves (0 -> 100 -> 95).
    _emit("CALM", days_ago=8, iv=0.40)
    _emit("CALM", days_ago=6, iv=0.50)
    _emit("CALM", days_ago=1, iv=0.495)

    # Verify persistence
    rows = conn.execute(
        "SELECT symbol, atm_iv, iv_rank_2w FROM iv_rank_history "
        "ORDER BY symbol, snapshot_ts"
    ).fetchall()
    assert len(rows) == 6

    # Alerts: WAT dropped from 100 to 30 (~70pt) -> alert.
    # CALM dropped from 100 to 95 (~5pt) -> no alert.
    alerts = iv_rank_alerts(conn, drop_threshold=10.0, lookback_days=10)
    symbols_alerted = {a["symbol"] for a in alerts}
    assert "WAT" in symbols_alerted
    assert "CALM" not in symbols_alerted
    wat = next(a for a in alerts if a["symbol"] == "WAT")
    assert wat["drop_pct"] > 10.0

    # Higher threshold -> no alerts.
    empty = iv_rank_alerts(conn, drop_threshold=90.0, lookback_days=10)
    assert empty == []


def test_capture_iv_ranks_idempotent(conn):
    from datetime import datetime, timedelta, timezone
    from options_earnings.db.repo import capture_iv_ranks

    repo.upsert_symbol(conn, _sym(symbol="A", price=100.0))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    j = repo.create_job(conn, ["A"], window_size=5)
    repo.insert_quotes(conn, [QuoteRow(
        job_id=j, symbol="A", snapshot_ts=now, underlying=100.0,
        expiry=date(2026, 12, 31), strike=100.0, cp="C",
        bid=1.0, ask=1.1, last=1.05, volume=1, open_interest=1,
        iv_yahoo=0.24, iv_computed=0.25,
    )])
    n1 = capture_iv_ranks(conn, ["A"], now)
    n2 = capture_iv_ranks(conn, ["A"], now)
    assert n1 == 1 and n2 == 1
    count = conn.execute("SELECT COUNT(*) FROM iv_rank_history").fetchone()[0]
    assert count == 1  # ON CONFLICT DO UPDATE — still 1 row


def test_list_symbols_atm_iv_pct_2w(conn):
    """ATM IV %% 2W = (current - min) / (max - min) * 100 over the 14d window."""
    from datetime import datetime, timedelta, timezone

    repo.upsert_symbol(conn, _sym(symbol="A", price=100.0))
    repo.upsert_symbol(conn, _sym(symbol="B", price=100.0))
    repo.upsert_symbol(conn, _sym(symbol="C", price=100.0))  # no quotes

    # Three snapshots for A: IV 20%, 30%, 25% (latest). Current at 25 -> 50%
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    def _q(symbol, hours_ago, iv, strike=100.0):
        ts = now - timedelta(hours=hours_ago)
        j = repo.create_job(conn, [symbol], window_size=5)
        return QuoteRow(
            job_id=j, symbol=symbol, snapshot_ts=ts, underlying=100.0,
            expiry=date(2026, 12, 31), strike=strike, cp="C",
            bid=1.0, ask=1.1, last=1.05, volume=1, open_interest=1,
            iv_yahoo=iv - 0.01, iv_computed=iv,
        )
    repo.insert_quotes(conn, [
        _q("A", hours_ago=72, iv=0.20),   # 3 days ago
        _q("A", hours_ago=48, iv=0.30),   # 2 days ago
        _q("A", hours_ago=1,  iv=0.25),   # latest -> current a.iv
    ])

    # B: 20% -> 30%, current 29% -> 90%
    repo.insert_quotes(conn, [
        _q("B", hours_ago=72, iv=0.20),
        _q("B", hours_ago=48, iv=0.30),
        _q("B", hours_ago=1,  iv=0.29),
    ])

    rows, _ = repo.list_symbols(conn)
    by = {r.symbol: r for r in rows}
    assert abs(by["A"].atm_iv_pct_2w - 50.0) < 1e-6
    assert abs(by["B"].atm_iv_pct_2w - 90.0) < 1e-6
    assert by["C"].atm_iv_pct_2w is None  # no quotes


def test_list_symbols_atm_iv_pct_2w_null_when_flat(conn):
    """If min == max (only one snapshot), percentile is NULL."""
    from datetime import datetime, timedelta, timezone

    repo.upsert_symbol(conn, _sym(symbol="A", price=100.0))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    j = repo.create_job(conn, ["A"], window_size=5)
    repo.insert_quotes(conn, [QuoteRow(
        job_id=j, symbol="A", snapshot_ts=now - timedelta(hours=1),
        underlying=100.0, expiry=date(2026, 12, 31), strike=100.0, cp="C",
        bid=1.0, ask=1.1, last=1.05, volume=1, open_interest=1,
        iv_yahoo=0.24, iv_computed=0.25,
    )])
    rows, _ = repo.list_symbols(conn)
    a = next(r for r in rows if r.symbol == "A")
    assert a.atm_iv is not None
    assert a.atm_iv_pct_2w is None  # single snapshot -> flat range -> NULL


def test_list_symbols_returns_atm_iv_and_can_sort(conn):
    repo.upsert_symbol(conn, _sym(symbol="A", price=100.0))
    repo.upsert_symbol(conn, _sym(symbol="B", price=100.0))
    repo.upsert_symbol(conn, _sym(symbol="C", price=100.0))

    j = repo.create_job(conn, ["A", "B"], window_size=5)
    base = datetime(2026, 5, 10, 15, 0, 0)
    repo.insert_quotes(conn, [
        QuoteRow(job_id=j, symbol="A", snapshot_ts=base, underlying=100.0,
                 expiry=date(2026, 5, 22), strike=100.0, cp="C",
                 bid=1.0, ask=1.1, last=1.05, volume=100, open_interest=500,
                 iv_yahoo=0.19, iv_computed=0.20),
        QuoteRow(job_id=j, symbol="B", snapshot_ts=base, underlying=100.0,
                 expiry=date(2026, 5, 22), strike=100.0, cp="C",
                 bid=1.0, ask=1.1, last=1.05, volume=100, open_interest=500,
                 iv_yahoo=0.49, iv_computed=0.50),
    ])

    rows, total = repo.list_symbols(conn, sort="atm_iv", dir_="desc")
    assert total == 3
    # B (0.50) > A (0.20); C has no iv -> NULLS LAST
    assert [r.symbol for r in rows] == ["B", "A", "C"]
    assert rows[0].atm_iv == 0.50
    assert rows[1].atm_iv == 0.20
    assert rows[2].atm_iv is None

    asc_rows, _ = repo.list_symbols(conn, sort="atm_iv", dir_="asc")
    # NULLS LAST applies to both directions in our implementation
    assert [r.symbol for r in asc_rows] == ["A", "B", "C"]


def test_list_symbols_filters_combine(conn):
    repo.upsert_symbol(conn, _sym(symbol="MSFT", company="Microsoft Corp.", mcap=3_000_000_000_000))
    repo.upsert_symbol(conn, _sym(symbol="MSTR", company="MicroStrategy Inc.", mcap=20_000_000_000))
    repo.upsert_symbol(conn, _sym(symbol="AAPL", company="Apple Inc.", mcap=2_800_000_000_000))

    rows, total = repo.list_symbols(conn, q="micro", min_mcap=1_000_000_000_000)
    assert total == 1
    assert rows[0].symbol == "MSFT"


def test_create_and_fetch_job(conn):
    job_id = repo.create_job(conn, ["AAPL", "MSFT"], window_size=20)
    got = repo.get_job(conn, job_id)
    assert isinstance(got, JobRow)
    assert got.status == "pending"
    assert got.window_size == 20
    assert got.symbols == ["AAPL", "MSFT"]
    assert got.completed_at is None


def test_update_job_status(conn):
    job_id = repo.create_job(conn, ["AAPL"], window_size=10)
    repo.update_job_status(conn, job_id, "running")
    assert repo.get_job(conn, job_id).status == "running"

    repo.update_job_status(conn, job_id, "done")
    final = repo.get_job(conn, job_id)
    assert final.status == "done"
    assert final.completed_at is not None
    assert final.error is None

    repo.update_job_status(conn, job_id, "error", error="boom")
    err = repo.get_job(conn, job_id)
    assert err.status == "error"
    assert err.error == "boom"


def test_list_jobs_orders_by_created_at_desc(conn):
    j1 = repo.create_job(conn, ["AAPL"], 10)
    j2 = repo.create_job(conn, ["MSFT"], 10)
    j3 = repo.create_job(conn, ["NVDA"], 10)
    listed = repo.list_jobs(conn, limit=10)
    assert {j.job_id for j in listed} == {j1, j2, j3}
    timestamps = [j.created_at for j in listed]
    assert timestamps == sorted(timestamps, reverse=True)


def test_list_jobs_respects_limit(conn):
    for _ in range(5):
        repo.create_job(conn, ["AAPL"], 10)
    assert len(repo.list_jobs(conn, limit=3)) == 3


def _quote(job_id, symbol="AAPL", strike=180.0, cp="C", iv=0.25, expiry=date(2026, 5, 9)) -> QuoteRow:
    return QuoteRow(
        job_id=job_id,
        symbol=symbol,
        snapshot_ts=datetime(2026, 4, 28, 15, 0, 0),
        underlying=180.0,
        expiry=expiry,
        strike=strike,
        cp=cp,
        bid=2.0,
        ask=2.1,
        last=2.05,
        volume=1000,
        open_interest=5000,
        iv_yahoo=0.24,
        iv_computed=iv,
    )


def test_insert_quotes_and_fetch_for_job(conn):
    job_id = repo.create_job(conn, ["AAPL"], 5)
    quotes = [
        _quote(job_id, strike=170.0, cp="C"),
        _quote(job_id, strike=180.0, cp="C"),
        _quote(job_id, strike=190.0, cp="C"),
        _quote(job_id, strike=180.0, cp="P"),
    ]
    n = repo.insert_quotes(conn, quotes)
    assert n == 4

    fetched = repo.quotes_for_job(conn, job_id)
    assert len(fetched) == 4
    assert sorted([q.strike for q in fetched if q.cp == "C"]) == [170.0, 180.0, 190.0]


def test_insert_quotes_upsert(conn):
    job_id = repo.create_job(conn, ["AAPL"], 5)
    repo.insert_quotes(conn, [_quote(job_id, strike=180.0, iv=0.25)])
    repo.insert_quotes(conn, [_quote(job_id, strike=180.0, iv=0.30)])
    fetched = repo.quotes_for_job(conn, job_id)
    assert len(fetched) == 1
    assert fetched[0].iv_computed == 0.30


def test_atm_quotes_for_job(conn):
    job_id = repo.create_job(conn, ["AAPL"], 5)
    repo.insert_quotes(conn, [
        _quote(job_id, strike=170.0, cp="C", iv=0.30),
        _quote(job_id, strike=178.0, cp="C", iv=0.26),
        _quote(job_id, strike=181.0, cp="C", iv=0.25),
        _quote(job_id, strike=190.0, cp="C", iv=0.28),
        _quote(job_id, strike=181.0, cp="P", iv=0.27),
    ])
    atm = repo.atm_quotes_for_job(conn, job_id)
    by_cp = {q.cp: q for q in atm}
    assert by_cp["C"].strike == 181.0
    assert by_cp["P"].strike == 181.0


def test_quotes_for_symbol_history_filtering(conn):
    j1 = repo.create_job(conn, ["AAPL"], 5)
    j2 = repo.create_job(conn, ["AAPL"], 5)
    base = datetime(2026, 4, 28, 15, 0, 0)

    q1 = _quote(j1, strike=180.0, cp="C", iv=0.25)
    q1.snapshot_ts = base
    q2 = _quote(j2, strike=180.0, cp="C", iv=0.30)
    q2.snapshot_ts = base + timedelta(hours=1)
    q3 = _quote(j2, strike=180.0, cp="P", iv=0.28)
    q3.snapshot_ts = base + timedelta(hours=1)
    repo.insert_quotes(conn, [q1, q2, q3])

    calls = repo.quotes_for_symbol(conn, "AAPL", cp="C")
    assert [q.iv_computed for q in calls] == [0.25, 0.30]

    puts = repo.quotes_for_symbol(conn, "AAPL", cp="P")
    assert len(puts) == 1
    assert puts[0].iv_computed == 0.28


def test_earnings_move_upsert_and_latest(conn):
    from options_earnings.db.repo import EarningsMoveRow, latest_earnings_move, upsert_earnings_move

    older = EarningsMoveRow(
        "AAPL", date(2026, 2, 1), 180.0, 3.5, -2.1, datetime(2026, 2, 5, 12, 0),
        window_high_3d=186.3, window_low_3d=176.22,
    )
    newer = EarningsMoveRow(
        "AAPL", date(2026, 5, 1), 200.0, 4.2, -1.8, datetime(2026, 5, 5, 12, 0),
        window_high_3d=208.4, window_low_3d=196.4,
    )
    upsert_earnings_move(conn, older)
    upsert_earnings_move(conn, newer)

    got = latest_earnings_move(conn, "AAPL")
    assert got is not None
    assert got.earnings_date == date(2026, 5, 1)
    assert got.max_up_3d_pct == 4.2
    assert got.window_high_3d == 208.4
    assert got.window_low_3d == 196.4

    updated = EarningsMoveRow(
        "AAPL", date(2026, 5, 1), 201.0, 5.0, -0.5, datetime(2026, 5, 6, 12, 0),
        window_high_3d=211.05, window_low_3d=199.995,
    )
    upsert_earnings_move(conn, updated)
    got2 = latest_earnings_move(conn, "AAPL")
    assert got2.max_up_3d_pct == 5.0
    assert got2.ref_close == 201.0
    assert got2.window_high_3d == 211.05

    assert latest_earnings_move(conn, "NOSYMBOL") is None


def test_ohlc_upsert_and_read(conn):
    from options_earnings.db.repo import OHLCRow, ohlc_for_symbol, upsert_ohlc

    rows = [
        OHLCRow("AAPL", date(2026, 4, 28), 100.0, 102.0, 99.0, 101.0),
        OHLCRow("AAPL", date(2026, 4, 29), 101.0, 105.0, 100.0, 104.0),
        OHLCRow("AAPL", date(2026, 4, 30), 104.0, 110.0, 103.0, 108.0),
        OHLCRow("MSFT", date(2026, 4, 28), 400.0, 410.0, 395.0, 405.0),
    ]
    assert upsert_ohlc(conn, rows) == 4

    aapl = ohlc_for_symbol(conn, "AAPL")
    assert [r.trading_day for r in aapl] == [date(2026, 4, 28), date(2026, 4, 29), date(2026, 4, 30)]
    assert aapl[2].high == 110.0

    upsert_ohlc(conn, [OHLCRow("AAPL", date(2026, 4, 30), 104.0, 111.0, 102.0, 109.0)])
    refreshed = ohlc_for_symbol(conn, "AAPL")
    assert refreshed[2].high == 111.0
    assert refreshed[2].close == 109.0


def test_recent_earnings_moves_returns_in_desc_order(conn):
    from options_earnings.db.repo import EarningsMoveRow, recent_earnings_moves, upsert_earnings_move

    dates = [
        date(2025, 5, 1), date(2025, 8, 1), date(2025, 11, 1),
        date(2026, 2, 1), date(2026, 5, 1),
    ]
    for i, d in enumerate(dates):
        upsert_earnings_move(conn, EarningsMoveRow(
            "AAPL", d, 100.0 + i, 1.0 * i, -1.0 * i, datetime(2026, 5, 5, 12, 0),
            window_high_3d=100.0 + i + 5, window_low_3d=100.0 + i - 5,
        ))

    moves = recent_earnings_moves(conn, "AAPL", limit=4)
    assert [m.earnings_date for m in moves] == [
        date(2026, 5, 1), date(2026, 2, 1), date(2025, 11, 1), date(2025, 8, 1),
    ]
    assert recent_earnings_moves(conn, "NOSYM") == []


def test_latest_atm_iv_for_symbols(conn):
    from options_earnings.db.repo import latest_atm_iv_for_symbols

    j_old = repo.create_job(conn, ["AAPL"], window_size=5)
    j_new = repo.create_job(conn, ["AAPL"], window_size=5)
    j_msft = repo.create_job(conn, ["MSFT"], window_size=5)
    older_ts = datetime(2026, 5, 1, 15, 0, 0)
    newer_ts = datetime(2026, 5, 10, 15, 0, 0)
    msft_ts = datetime(2026, 5, 10, 15, 0, 0)

    # AAPL — older snapshot (IV 0.20) and newer one (IV 0.30 at ATM=180).
    def q(job, *, symbol, strike, cp, iv, ts, underlying=180.0):
        return QuoteRow(
            job_id=job, symbol=symbol, snapshot_ts=ts, underlying=underlying,
            expiry=date(2026, 5, 22), strike=strike, cp=cp,
            bid=1.0, ask=1.1, last=1.05, volume=100, open_interest=500,
            iv_yahoo=iv - 0.01, iv_computed=iv,
        )
    repo.insert_quotes(conn, [
        q(j_old, symbol="AAPL", strike=180.0, cp="C", iv=0.20, ts=older_ts),
        q(j_new, symbol="AAPL", strike=170.0, cp="C", iv=0.40, ts=newer_ts),
        q(j_new, symbol="AAPL", strike=181.0, cp="C", iv=0.30, ts=newer_ts),  # ATM (underlying=180)
        q(j_new, symbol="AAPL", strike=190.0, cp="C", iv=0.35, ts=newer_ts),
        q(j_msft, symbol="MSFT", strike=420.0, cp="C", iv=0.22, ts=msft_ts, underlying=421.0),
        q(j_msft, symbol="MSFT", strike=420.0, cp="P", iv=0.99, ts=msft_ts, underlying=421.0),
    ])

    out = latest_atm_iv_for_symbols(conn, ["AAPL", "MSFT", "NOSYM"])
    assert out["AAPL"] == 0.30  # latest snapshot's ATM strike (181 closer to 180 than 170 or 190)
    assert out["MSFT"] == 0.22  # call-side IV
    assert "NOSYM" not in out

    # Falls back to iv_yahoo when iv_computed is NULL.
    repo.insert_quotes(conn, [QuoteRow(
        job_id=j_msft, symbol="MSFT", snapshot_ts=datetime(2026, 5, 11, 15, 0, 0),
        underlying=421.0, expiry=date(2026, 5, 22), strike=421.0, cp="C",
        bid=1.0, ask=1.1, last=1.05, volume=100, open_interest=500,
        iv_yahoo=0.18, iv_computed=None,
    )])
    out2 = latest_atm_iv_for_symbols(conn, ["MSFT"])
    assert out2["MSFT"] == 0.18


def test_expiries_for_symbol(conn):
    job_id = repo.create_job(conn, ["AAPL"], 5)
    repo.insert_quotes(conn, [
        _quote(job_id, strike=180.0, cp="C", expiry=date(2026, 5, 2)),
        _quote(job_id, strike=180.0, cp="C", expiry=date(2026, 5, 9)),
        _quote(job_id, strike=180.0, cp="C", expiry=date(2026, 5, 16)),
    ])
    expiries = repo.expiries_for_symbol(conn, "AAPL")
    assert expiries == [date(2026, 5, 2), date(2026, 5, 9), date(2026, 5, 16)]
