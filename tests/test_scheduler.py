from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from options_earnings.config import Settings
from options_earnings.db import repo
from options_earnings.db.connection import open_memory
from options_earnings.db.repo import SymbolRow
from options_earnings.jobs import scheduler as sched_mod


def _sym(symbol: str, earnings: date | None) -> SymbolRow:
    return SymbolRow(
        symbol=symbol,
        company_name=f"{symbol} Corp",
        sector="Tech",
        market_cap=1_000_000_000,
        last_price=100.0,
        next_earnings=earnings,
        earnings_when=None,
        refreshed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def test_watchlist_filters_by_window(tmp_path, monkeypatch):
    db_path = tmp_path / "test.duckdb"
    today = date.today()
    rows = [
        _sym("NEAR", today + timedelta(days=2)),
        _sym("FAR", today + timedelta(days=60)),
        _sym("PAST", today - timedelta(days=5)),
        _sym("NULL_E", None),
        _sym("EDGE", today + timedelta(days=14)),
    ]
    from options_earnings.db.connection import open_db
    conn = open_db(db_path)
    for r in rows:
        repo.upsert_symbol(conn, r)
    conn.close()

    out = sched_mod._watchlist_symbols(db_path, days=14)
    assert set(out) == {"NEAR", "EDGE"}


def test_start_scheduler_disabled_returns_none():
    s = Settings(scheduler_enabled=False)
    assert sched_mod.start_scheduler(s) is None


def test_translate_dow_maps_standard_cron_numbers() -> None:
    from options_earnings.jobs.scheduler import _translate_dow
    assert _translate_dow("1-5") == "mon-fri"
    assert _translate_dow("0") == "sun"
    assert _translate_dow("6") == "sat"
    assert _translate_dow("7") == "sun"
    assert _translate_dow("1,3,5") == "mon,wed,fri"
    assert _translate_dow("1-5/2") == "mon-fri/2"
    assert _translate_dow("*") == "*"
    assert _translate_dow("mon-fri") == "mon-fri"


def test_crontab_trigger_1_5_fires_monday_to_friday() -> None:
    """The bug this guards: APScheduler numbers 0=Monday, so a raw "1-5"
    passed to from_crontab means Tue-Sat — Monday never runs.
    """
    from datetime import datetime

    from options_earnings.jobs.scheduler import crontab_trigger

    trigger = crontab_trigger("*/10 * * * 1-5", "UTC")
    fires = []
    for day in range(14, 21):  # Mon 2026-09-14 .. Sun 2026-09-20
        base = datetime(2026, 9, day, 0, 0, tzinfo=trigger.timezone)
        nxt = trigger.get_next_fire_time(None, base)
        if nxt is not None and nxt.date() == date(2026, 9, day):
            fires.append(nxt.strftime("%a"))
    assert fires == ["Mon", "Tue", "Wed", "Thu", "Fri"]


def test_crontab_trigger_preserves_non_weekday_fields() -> None:
    from options_earnings.jobs.scheduler import crontab_trigger
    trigger = crontab_trigger("0 22 * * 1-5", "UTC")
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["hour"] == "22"
    assert fields["minute"] == "0"
    assert fields["day_of_week"] == "mon-fri"
