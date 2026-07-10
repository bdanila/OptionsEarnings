from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from options_earnings.db import repo
from options_earnings.db.connection import open_memory
from options_earnings.db.repo import QuoteRow, SymbolRow
from options_earnings.web.app import create_app


@pytest.fixture
def conn():
    c = open_memory()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def client(conn):
    app = create_app(conn)
    with TestClient(app, follow_redirects=False) as tc:
        yield tc


def _sym(symbol: str, price: float = 100.0, mcap: int = 1_000_000_000) -> SymbolRow:
    return SymbolRow(
        symbol=symbol,
        company_name=f"{symbol} Corp.",
        sector="Tech",
        market_cap=mcap,
        last_price=price,
        next_earnings=date(2026, 5, 1),
        earnings_when="AMC",
        refreshed_at=datetime(2026, 4, 28, 12, 0, 0),
    )


def _quote(job_id: UUID, *, symbol="AAPL", strike=180.0, cp="C", iv=0.25,
           snapshot_ts=None, underlying=180.0, expiry=date(2026, 5, 9)) -> QuoteRow:
    return QuoteRow(
        job_id=job_id,
        symbol=symbol,
        snapshot_ts=snapshot_ts or datetime(2026, 4, 28, 15, 0, 0),
        underlying=underlying,
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


def test_index_lists_symbols_paginated(conn, client):
    for i in range(60):
        repo.upsert_symbol(conn, _sym(f"SYM{i:02d}", price=float(i)))
    r = client.get("/?size=25&page=2")
    assert r.status_code == 200
    body = r.text
    # Each row has a checkbox; count of checkboxes is the displayed count.
    assert body.count('<input type="checkbox" name="symbols"') == 25
    # Pagination links present.
    assert "Prev" in body and "Next" in body
    assert "page 2 of" in body


def test_index_filter_q(conn, client):
    repo.upsert_symbol(conn, _sym("MSFT"))
    repo.upsert_symbol(conn, _sym("AAPL"))
    repo.upsert_symbol(conn, _sym("GOOG"))
    r = client.get("/?q=MSFT")
    assert r.status_code == 200
    body = r.text
    assert body.count('<input type="checkbox" name="symbols"') == 1
    assert "MSFT" in body
    assert "AAPL" not in body or 'value="AAPL"' not in body


def test_index_filter_min_mcap_with_suffix(conn, client):
    repo.upsert_symbol(conn, _sym("SMALL", mcap=500_000_000))
    repo.upsert_symbol(conn, _sym("MID", mcap=50_000_000_000))
    repo.upsert_symbol(conn, _sym("BIG", mcap=2_000_000_000_000))
    r = client.get("/?min_mcap=10B")
    assert r.status_code == 200
    body = r.text
    assert body.count('<input type="checkbox" name="symbols"') == 2
    assert 'value="MID"' in body and 'value="BIG"' in body
    assert 'value="SMALL"' not in body


def test_index_accepts_empty_earnings_params(conn, client):
    repo.upsert_symbol(conn, _sym("AAPL"))
    r = client.get("/?page=2&size=50&q=&min_mcap=&earnings_from=&earnings_to=&sort=symbol&dir=asc")
    assert r.status_code == 200


def test_index_accepts_invalid_earnings_params(conn, client):
    repo.upsert_symbol(conn, _sym("AAPL"))
    r = client.get("/?earnings_from=not-a-date&earnings_to=2026/13/40")
    assert r.status_code == 200


def test_index_filter_earnings_range(conn, client):
    def s(symbol, earn):
        return SymbolRow(
            symbol=symbol, company_name=f"{symbol} Co", sector="Tech",
            market_cap=1, last_price=10.0, next_earnings=earn,
            earnings_when=None, refreshed_at=datetime(2026, 4, 28),
        )
    repo.upsert_symbol(conn, s("EARLY", date(2026, 5, 5)))
    repo.upsert_symbol(conn, s("MID", date(2026, 5, 20)))
    repo.upsert_symbol(conn, s("LATE", date(2026, 6, 30)))

    r = client.get("/?earnings_from=2026-05-01&earnings_to=2026-05-31")
    assert r.status_code == 200
    body = r.text
    assert 'value="EARLY"' in body and 'value="MID"' in body
    assert 'value="LATE"' not in body


def test_index_renders_atm_iv_column(conn, client):
    repo.upsert_symbol(conn, _sym("AAPL"))
    repo.upsert_symbol(conn, _sym("NOIV"))
    job_id = repo.create_job(conn, ["AAPL"], window_size=5)
    repo.insert_quotes(conn, [
        QuoteRow(
            job_id=job_id, symbol="AAPL",
            snapshot_ts=datetime(2026, 5, 10, 15, 0),
            underlying=100.0, expiry=date(2026, 5, 22),
            strike=100.0, cp="C",
            bid=1.0, ask=1.1, last=1.05, volume=100, open_interest=500,
            iv_yahoo=0.24, iv_computed=0.2543,
        ),
    ])
    r = client.get("/?sort=symbol&dir=asc")
    assert r.status_code == 200
    body = r.text
    assert "ATM IV" in body
    # 0.2543 -> 25.4%
    assert "25.4%" in body


def test_candles_page_renders_chart(conn, client):
    from datetime import date, timedelta
    from options_earnings.db.repo import OHLCRow

    repo.upsert_symbol(conn, _sym("AAPL", price=180.0))
    today = date.today()
    repo.upsert_ohlc(conn, [
        OHLCRow("AAPL", today - timedelta(days=5), 175.0, 178.0, 173.0, 176.0),
        OHLCRow("AAPL", today - timedelta(days=1), 176.0, 181.0, 175.0, 180.0),
    ])
    r = client.get("/symbols/AAPL/candles")
    assert r.status_code == 200
    body = r.text
    assert 'id="chart"' in body
    assert "lightweight-charts" in body
    assert "175.0" in body and "181.0" in body
    assert "AAPL" in body


def test_dismiss_iv_alerts_endpoint(conn, client):
    from datetime import datetime, timedelta, timezone
    from options_earnings.db.repo import capture_iv_ranks, iv_rank_alerts

    repo.upsert_symbol(conn, _sym("WAT", price=100.0))
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for days, iv in [(6, 0.20), (4, 0.30), (1, 0.22)]:
        ts = now - timedelta(days=days)
        j = repo.create_job(conn, ["WAT"], window_size=5)
        repo.insert_quotes(conn, [QuoteRow(
            job_id=j, symbol="WAT", snapshot_ts=ts, underlying=100.0,
            expiry=date(2026, 12, 31), strike=100.0, cp="C",
            bid=1.0, ask=1.1, last=1.05, volume=1, open_interest=1,
            iv_yahoo=iv - 0.01, iv_computed=iv,
        )])
        capture_iv_ranks(conn, ["WAT"], ts)

    alerts = iv_rank_alerts(conn, drop_threshold=10.0, lookback_days=10)
    assert len(alerts) == 1
    a = alerts[0]

    # POST dismiss
    r = client.post(
        "/iv-alerts/dismiss",
        data={"alert": [f"{a['symbol']}|{a['snapshot_ts'].isoformat()}"]},
    )
    assert r.status_code == 303
    assert iv_rank_alerts(conn, drop_threshold=10.0, lookback_days=10) == []


def test_alert_lookback_query_param_reflected_in_ui(conn, client):
    from options_earnings.db.repo import capture_iv_ranks
    from datetime import datetime, timedelta, timezone
    # Seed a WAT alert so the column header renders with the lookback value.
    repo.upsert_symbol(conn, _sym("WAT", price=100.0))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for days, iv in [(3, 0.20), (2, 0.30), (1, 0.22)]:
        ts = now - timedelta(days=days)
        j = repo.create_job(conn, ["WAT"], window_size=5)
        repo.insert_quotes(conn, [QuoteRow(
            job_id=j, symbol="WAT", snapshot_ts=ts, underlying=100.0,
            expiry=date(2026, 12, 31), strike=100.0, cp="C",
            bid=1.0, ask=1.1, last=1.05, volume=1, open_interest=1,
            iv_yahoo=iv - 0.01, iv_computed=iv,
        )])
        capture_iv_ranks(conn, ["WAT"], ts)

    r = client.get("/?alert_lookback=5")
    body = r.text
    assert 'value="5"' in body
    # Column header uses the user-provided lookback
    assert "ATM IV Max Rank (5 days)" in body


def test_favicon_link_in_head(conn, client):
    repo.upsert_symbol(conn, _sym("AAPL"))
    r = client.get("/")
    body = r.text
    assert 'rel="icon"' in body
    assert 'href="/static/favicon.png"' in body


def test_candles_page_has_external_links(conn, client):
    repo.upsert_symbol(conn, _sym("WBD"))
    r = client.get("/symbols/WBD/candles")
    body = r.text
    assert 'https://www.tradingview.com/chart/WrZaSQaL/?symbol=WBD' in body
    assert 'https://seekingalpha.com/symbol/WBD' in body
    # Both should open in new tab
    assert 'target="_blank"' in body
    assert 'rel="noopener noreferrer"' in body


def test_candles_page_no_data(conn, client):
    repo.upsert_symbol(conn, _sym("XXX"))
    r = client.get("/symbols/XXX/candles")
    assert r.status_code == 200
    assert "No candles yet" in r.text


def test_candles_page_unknown_symbol_404(conn, client):
    r = client.get("/symbols/NOSUCHSYM/candles")
    assert r.status_code == 404


def test_index_symbol_links_to_candles(conn, client):
    repo.upsert_symbol(conn, _sym("MSFT"))
    r = client.get("/")
    assert 'href="/symbols/MSFT/candles"' in r.text


def test_index_renders_3m_columns_and_progress_pill(conn, client):
    from options_earnings.db.repo import OHLCRow

    repo.upsert_symbol(conn, _sym("AAPL", price=100.0))
    from datetime import date
    today = date.today()
    repo.upsert_ohlc(conn, [
        OHLCRow("AAPL", today, 95.0, 120.0, 80.0, 100.0),
    ])
    r = client.get("/")
    body = r.text
    # Column headers
    assert "Min 3M" in body
    assert "Max 3M" in body
    # Values: min = -20.00%, max = +20.00% (low 80 vs price 100, high 120 vs 100)
    assert "-20.00%" in body
    assert "+20.00%" in body
    # Progress pill
    assert "Daily candles" in body
    assert "up to date" in body
    assert str(today) in body  # latest_day shown


def test_index_human_mcap_rendering(conn, client):
    repo.upsert_symbol(conn, _sym("MSFT", mcap=3_260_383_008_800))
    r = client.get("/?q=MSFT")
    assert "3.260T" in r.text


def test_parse_mcap_input():
    from options_earnings.web.app import parse_mcap_input
    assert parse_mcap_input("10B") == 10_000_000_000
    assert parse_mcap_input("1.5T") == 1_500_000_000_000
    assert parse_mcap_input("500m") == 500_000_000
    assert parse_mcap_input("100k") == 100_000
    assert parse_mcap_input("12345") == 12345.0
    assert parse_mcap_input("$1.5B") == 1_500_000_000
    assert parse_mcap_input("") is None
    assert parse_mcap_input(None) is None
    assert parse_mcap_input("not a number") is None


def test_index_sort_by_price_desc(conn, client):
    repo.upsert_symbol(conn, _sym("AAA", price=10.0))
    repo.upsert_symbol(conn, _sym("BBB", price=30.0))
    repo.upsert_symbol(conn, _sym("CCC", price=20.0))
    r = client.get("/?sort=last_price&dir=desc")
    assert r.status_code == 200
    body = r.text
    # Order in HTML should be BBB, CCC, AAA.
    iB = body.index("BBB")
    iC = body.index("CCC")
    iA = body.index("AAA")
    assert iB < iC < iA


def test_post_monitor_iv_enables_and_stop_disables(conn, client, monkeypatch):
    repo.upsert_symbol(conn, _sym("AAPL"))
    repo.upsert_symbol(conn, _sym("MSFT"))
    repo.upsert_symbol(conn, _sym("NVDA"))

    from fastapi import BackgroundTasks
    monkeypatch.setattr(BackgroundTasks, "add_task", lambda self, *a, **kw: None)

    # Without a Referer, falls back to "/"
    r = client.post("/monitor-iv", data={"symbols": ["AAPL", "NVDA"]})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert repo.monitored_symbols(conn) == ["AAPL", "NVDA"]
    jobs = repo.list_jobs(conn)
    assert len(jobs) == 1
    assert jobs[0].symbols == ["AAPL", "NVDA"]

    # With Referer carrying filters, redirect preserves them
    filtered = "http://testserver/?min_mcap=10B&earnings_from=2026-06-01&sort=atm_iv&dir=desc"
    r = client.post(
        "/monitor-iv/stop",
        data={"symbols": ["AAPL"]},
        headers={"Referer": filtered},
    )
    assert r.status_code == 303
    assert r.headers["location"] == filtered
    assert repo.monitored_symbols(conn) == ["NVDA"]
    jobs_after = repo.list_jobs(conn)
    assert len(jobs_after) == 1  # stop didn't trigger a new chain job


def test_index_filter_iv_monitored(conn, client):
    repo.upsert_symbol(conn, _sym("AAPL"))
    repo.upsert_symbol(conn, _sym("MSFT"))
    repo.upsert_symbol(conn, _sym("NVDA"))
    repo.set_iv_monitored(conn, ["AAPL", "NVDA"], True)

    r = client.get("/?iv_monitored=yes")
    body = r.text
    assert 'value="AAPL"' in body and 'value="NVDA"' in body
    assert 'value="MSFT"' not in body

    r = client.get("/?iv_monitored=no")
    body = r.text
    assert 'value="MSFT"' in body
    assert 'value="AAPL"' not in body and 'value="NVDA"' not in body


def test_index_renders_iv_monitored_column_and_buttons(conn, client):
    repo.upsert_symbol(conn, _sym("AAPL"))
    repo.set_iv_monitored(conn, ["AAPL"], True)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "IV Monitored" in body
    assert 'id="select-all"' in body
    assert 'formaction="/monitor-iv"' in body
    assert 'formaction="/monitor-iv/stop"' in body


def test_post_jobs_creates_job_and_redirects(conn, client, monkeypatch):
    repo.upsert_symbol(conn, _sym("AAPL"))
    repo.upsert_symbol(conn, _sym("MSFT"))

    from fastapi import BackgroundTasks
    monkeypatch.setattr(BackgroundTasks, "add_task", lambda self, *a, **kw: None)

    r = client.post("/jobs", data={"symbols": ["AAPL", "MSFT"]})
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/jobs/")
    job_id = UUID(loc.rsplit("/", 1)[1])
    job = repo.get_job(conn, job_id)
    assert job is not None
    assert job.symbols == ["AAPL", "MSFT"]


def test_get_job_pending_renders_status(conn, client):
    job_id = repo.create_job(conn, ["AAPL"], window_size=10)
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    assert "pending" in r.text
    assert "badge-pending" in r.text


def test_get_job_done_renders_atm_summary(conn, client):
    job_id = repo.create_job(conn, ["AAPL"], window_size=5)
    repo.update_job_status(conn, job_id, "done")
    repo.insert_quotes(conn, [
        _quote(job_id, strike=170.0, cp="C", iv=0.30),
        _quote(job_id, strike=180.0, cp="C", iv=0.25),
        _quote(job_id, strike=190.0, cp="C", iv=0.28),
        _quote(job_id, strike=180.0, cp="P", iv=0.27),
    ])
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    body = r.text
    assert "ATM Summary" in body
    # ATM strike 180.00 should appear (closest to underlying 180).
    assert "180.00" in body
    # Both call and put rows should be present.
    assert ">C<" in body
    assert ">P<" in body
    # IV values should be rendered (computed IV at ATM is 0.25 / 0.27).
    assert "0.2500" in body
    assert "0.2700" in body
    # Earnings move columns present even without data
    assert "Last Earnings" in body
    assert "Max Up 3d" in body
    assert "Max Down 3d" in body


def test_get_job_done_renders_earnings_move_columns(conn, client):
    from options_earnings.db.repo import EarningsMoveRow

    job_id = repo.create_job(conn, ["AAPL"], window_size=5)
    repo.update_job_status(conn, job_id, "done")
    repo.insert_quotes(conn, [
        _quote(job_id, strike=180.0, cp="C", iv=0.25),
        _quote(job_id, strike=180.0, cp="P", iv=0.27),
    ])
    repo.upsert_earnings_move(conn, EarningsMoveRow(
        symbol="AAPL",
        earnings_date=date(2026, 2, 1),
        ref_close=180.0,
        max_up_3d_pct=4.25,
        max_down_3d_pct=-2.50,
        computed_at=datetime(2026, 2, 5, 12, 0),
    ))
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    body = r.text
    assert "2026-02-01" in body
    assert "+4.25" in body
    assert "-2.50" in body


def test_chain_detail_renders_recent_earnings_moves(conn, client):
    from options_earnings.db.repo import EarningsMoveRow

    job_id = repo.create_job(conn, ["AAPL"], window_size=5)
    repo.update_job_status(conn, job_id, "done")
    # underlying=180 (default), strikes 170/180/190, ATM=180 with last=2.05 for both C and P
    repo.insert_quotes(conn, [
        _quote(job_id, strike=170.0, cp="C", iv=0.30),
        _quote(job_id, strike=180.0, cp="C", iv=0.25),
        _quote(job_id, strike=190.0, cp="C", iv=0.28),
        _quote(job_id, strike=180.0, cp="P", iv=0.27),
    ])
    seed_dates = [
        date(2024, 2, 1), date(2024, 5, 1), date(2024, 8, 1), date(2024, 11, 1),
        date(2025, 2, 1), date(2025, 5, 1), date(2025, 8, 1), date(2025, 11, 1),
        date(2026, 2, 1), date(2026, 5, 1),
    ]
    for i, d in enumerate(seed_dates):
        repo.upsert_earnings_move(conn, EarningsMoveRow(
            symbol="AAPL", earnings_date=d, ref_close=100.0 + i, max_up_3d_pct=4.25,
            max_down_3d_pct=-2.50,
            computed_at=datetime(2026, 5, 6, 12, 0),
            window_high_3d=105.0 + i, window_low_3d=95.0 + i,
        ))

    r = client.get(f"/jobs/{job_id}/AAPL")
    assert r.status_code == 200
    body = r.text
    assert "Last 8 earnings moves" in body
    assert "Close before ($)" in body
    assert "High 3d after ($, %)" in body
    assert "Low 3d after ($, %)" in body
    assert "2026-05-01" in body
    assert "+4.25%" in body
    assert "-2.50%" in body
    expected_desc = [date(2026, 5, 1), date(2026, 2, 1), date(2025, 11, 1), date(2025, 8, 1),
                     date(2025, 5, 1), date(2025, 2, 1), date(2024, 11, 1), date(2024, 8, 1)]
    desc_positions = [body.find(d.isoformat()) for d in expected_desc]
    assert all(p > 0 for p in desc_positions)
    assert desc_positions == sorted(desc_positions)
    # the 9th and 10th oldest (2024-05-01, 2024-02-01) should NOT appear (limit=8)
    assert "2024-05-01" not in body
    assert "2024-02-01" not in body
    # bar chart canvas and Chart.js script present
    assert 'id="movesChart"' in body
    assert "type: 'bar'" in body
    assert "Max Up 3d (%)" in body
    assert "Max Down 3d (%)" in body
    # Call/Put Last-over-Strike lines: with last=2.05, strike=180 -> 1.1389% (rounds to 1.14)
    expected_pct = round(2.05 / 180.0 * 100.0, 2)
    assert f"{expected_pct:.2f}%" in body
    assert "Call Last / Strike (%)" in body
    assert "Put Last / Strike (%, mirrored)" in body


def test_chain_detail_dynamic_window_from_expiry_gap(conn, client):
    from options_earnings.db.repo import EarningsMoveRow, OHLCRow

    # Seed a symbol with next_earnings on 2026-05-20.
    repo.upsert_symbol(conn, SymbolRow(
        symbol="NVDA", company_name="NVIDIA", sector="Tech",
        market_cap=5_000_000_000_000, last_price=900.0,
        next_earnings=date(2026, 5, 20), earnings_when=None,
        refreshed_at=datetime(2026, 5, 10, 12, 0),
    ))

    # Job whose chosen expiry is 2026-05-22 (gap of 2 calendar days).
    job_id = repo.create_job(conn, ["NVDA"], window_size=5)
    repo.update_job_status(conn, job_id, "done")
    repo.insert_quotes(conn, [
        QuoteRow(
            job_id=job_id, symbol="NVDA",
            snapshot_ts=datetime(2026, 5, 15, 15, 0),
            underlying=900.0, expiry=date(2026, 5, 22),
            strike=900.0, cp="C",
            bid=20.0, ask=21.0, last=20.5,
            volume=100, open_interest=500,
            iv_yahoo=0.5, iv_computed=0.48,
        ),
        QuoteRow(
            job_id=job_id, symbol="NVDA",
            snapshot_ts=datetime(2026, 5, 15, 15, 0),
            underlying=900.0, expiry=date(2026, 5, 22),
            strike=900.0, cp="P",
            bid=18.0, ask=19.0, last=18.5,
            volume=100, open_interest=500,
            iv_yahoo=0.5, iv_computed=0.48,
        ),
    ])

    # Anchor: a single past earnings on 2026-02-19.
    repo.upsert_earnings_move(conn, EarningsMoveRow(
        symbol="NVDA", earnings_date=date(2026, 2, 19),
        ref_close=800.0, max_up_3d_pct=10.0, max_down_3d_pct=-5.0,
        computed_at=datetime(2026, 2, 25, 12, 0),
        window_high_3d=880.0, window_low_3d=760.0,
    ))

    # OHLC around 2026-02-19. 2-day window starting 2026-02-19:
    # high = max(810, 830) = 830; low = min(795, 810) = 795
    repo.upsert_ohlc(conn, [
        OHLCRow("NVDA", date(2026, 2, 18), 805.0, 808.0, 800.0, 800.0),  # ref_close = 800
        OHLCRow("NVDA", date(2026, 2, 19), 800.0, 810.0, 795.0, 805.0),
        OHLCRow("NVDA", date(2026, 2, 20), 805.0, 830.0, 810.0, 825.0),
        OHLCRow("NVDA", date(2026, 2, 23), 825.0, 880.0, 760.0, 770.0),  # outside 2d window
    ])

    r = client.get(f"/jobs/{job_id}/NVDA")
    assert r.status_code == 200
    body = r.text
    # Header reflects the 2-day gap
    assert "2026-05-20" in body and "2026-05-22" in body
    assert "Gap:" in body
    assert "2 day" in body
    # Column header uses dynamic window
    assert "High 2d after" in body and "Low 2d after" in body
    # Recomputed window (NOT the cached 880/760 from the 3-day anchor)
    assert "830.00" in body
    assert "795.00" in body
    assert "880.00" not in body
    assert "760.00" not in body
    # Close at 2d after = close of 2026-02-20 = 825.00; pct = (825-800)/800 = +3.13%
    assert "Close 2d after" in body
    assert "825.00" in body
    assert "+3.12%" in body or "+3.13%" in body


def test_chain_detail_no_moves_yet(conn, client):
    job_id = repo.create_job(conn, ["XXX"], window_size=5)
    repo.update_job_status(conn, job_id, "done")
    repo.insert_quotes(conn, [_quote(job_id, symbol="XXX", strike=10.0, cp="C")])
    r = client.get(f"/jobs/{job_id}/XXX")
    assert r.status_code == 200
    assert "No earnings move history yet for XXX" in r.text


def test_iv_history_json_empty(conn, client):
    r = client.get("/symbols/AAPL/iv-history.json")
    assert r.status_code == 200
    assert r.json() == []


def test_iv_history_json_rolling_atm(conn, client):
    base = datetime(2026, 4, 28, 15, 0, 0)
    expiry = date(2026, 5, 9)
    # Three snapshots with drifting underlying; strikes 170/180/190 each snapshot.
    # One job per snapshot so the primary key (job_id, symbol, expiry, strike, cp)
    # accommodates repeating strikes across snapshots.
    snapshots = [(0, 171.0), (1, 181.0), (2, 192.0)]
    for ts_off, under in snapshots:
        ts = base + timedelta(hours=ts_off)
        jid = repo.create_job(conn, ["AAPL"], 5)
        repo.insert_quotes(conn, [
            _quote(jid, strike=170.0, cp="C", snapshot_ts=ts, underlying=under,
                   iv=0.20, expiry=expiry),
            _quote(jid, strike=180.0, cp="C", snapshot_ts=ts, underlying=under,
                   iv=0.25, expiry=expiry),
            _quote(jid, strike=190.0, cp="C", snapshot_ts=ts, underlying=under,
                   iv=0.30, expiry=expiry),
        ])

    r = client.get("/symbols/AAPL/iv-history.json?mode=rolling&cp=C")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 3
    # Closest strike per snapshot: 170, 180, 190.
    assert [d["strike"] for d in data] == [170.0, 180.0, 190.0]
