from datetime import date
from importlib import resources
from math import ceil
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import duckdb
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from options_earnings.config import get_settings
from options_earnings.db import repo
from options_earnings.db.connection import open_db


def _templates_dir() -> Path:
    return Path(str(resources.files("options_earnings.web").joinpath("templates")))


def human_mcap(value: float | int | None) -> str:
    if value is None:
        return ""
    v = float(value)
    if v >= 1e12:
        return f"{v / 1e12:.3f}T"
    if v >= 1e9:
        return f"{v / 1e9:.3f}B"
    if v >= 1e6:
        return f"{v / 1e6:.3f}M"
    if v >= 1e3:
        return f"{v / 1e3:.3f}K"
    return f"{v:.0f}"


def _parse_date(text: str | None) -> date | None:
    if text is None:
        return None
    s = text.strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def parse_mcap_input(text: str | None) -> float | None:
    """Parse user mcap input. Accepts "10B", "1.5T", "500M", "100K", or a plain number.

    Returns None on empty/invalid input.
    """
    if text is None:
        return None
    s = text.strip().upper().replace(",", "").replace("$", "")
    if not s:
        return None
    suffix_mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    mult = 1.0
    if s[-1] in suffix_mult:
        mult = suffix_mult[s[-1]]
        s = s[:-1].strip()
    try:
        return float(s) * mult
    except ValueError:
        return None


def create_app(conn: duckdb.DuckDBPyConnection) -> FastAPI:
    """Build a FastAPI app with an injected DuckDB connection.

    Tests use this to get isolation; production uses the module-level `app`
    that opens a connection from settings.
    """
    settings = get_settings()
    app = FastAPI(title="OptionsEarnings")
    templates = Jinja2Templates(directory=str(_templates_dir()))
    templates.env.filters["human_mcap"] = human_mcap

    def get_conn() -> duckdb.DuckDBPyConnection:
        return conn

    Conn = Annotated[duckdb.DuckDBPyConnection, Depends(get_conn)]

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        c: Conn,
        page: int = 1,
        sort: str = "symbol",
        dir: str = "asc",
        size: int | None = None,
        q: str | None = None,
        min_mcap: str | None = None,
        earnings_from: str | None = None,
        earnings_to: str | None = None,
        iv_monitored: str | None = None,
    ) -> HTMLResponse:
        page = max(1, page)
        size = size or settings.page_size
        min_mcap_value = parse_mcap_input(min_mcap)
        q_value = (q or "").strip() or None
        earnings_from_value = _parse_date(earnings_from)
        earnings_to_value = _parse_date(earnings_to)
        iv_monitored_flag: bool | None
        if iv_monitored == "yes":
            iv_monitored_flag = True
        elif iv_monitored == "no":
            iv_monitored_flag = False
        else:
            iv_monitored_flag = None
        rows, total = repo.list_symbols(
            c, page=page, size=size, sort=sort, dir_=dir,
            q=q_value, min_mcap=min_mcap_value,
            earnings_from=earnings_from_value, earnings_to=earnings_to_value,
            iv_monitored=iv_monitored_flag,
        )
        total_pages = max(1, ceil(total / size)) if size else 1
        next_dir = "desc" if dir == "asc" else "asc"
        return templates.TemplateResponse(
            request,
            "stocks.html",
            {
                "rows": rows,
                "total": total,
                "page": page,
                "size": size,
                "sort": sort,
                "dir": dir,
                "next_dir": next_dir,
                "total_pages": total_pages,
                "q": q_value or "",
                "min_mcap_input": min_mcap or "",
                "earnings_from": earnings_from_value.isoformat() if earnings_from_value else "",
                "earnings_to": earnings_to_value.isoformat() if earnings_to_value else "",
                "iv_monitored": iv_monitored if iv_monitored in ("yes", "no") else "",
            },
        )

    def _back_to_referrer(request: Request) -> str:
        ref = request.headers.get("referer")
        return ref if ref else "/"

    @app.post("/monitor-iv")
    def post_monitor_iv(
        request: Request,
        c: Conn,
        background_tasks: BackgroundTasks,
        symbols: Annotated[list[str], Form()],
    ) -> RedirectResponse:
        if symbols:
            repo.set_iv_monitored(c, symbols, True)
            job_id = repo.create_job(c, symbols, window_size=settings.option_chain_window)
            from options_earnings.options.job import run_chain_job
            background_tasks.add_task(
                run_chain_job, str(settings.db_path), job_id,
                window=settings.option_chain_window,
            )
        return RedirectResponse(url=_back_to_referrer(request), status_code=303)

    @app.post("/monitor-iv/stop")
    def post_monitor_iv_stop(
        request: Request,
        c: Conn,
        symbols: Annotated[list[str], Form()],
    ) -> RedirectResponse:
        if symbols:
            repo.set_iv_monitored(c, symbols, False)
        return RedirectResponse(url=_back_to_referrer(request), status_code=303)

    @app.post("/jobs")
    def post_jobs(
        c: Conn,
        background_tasks: BackgroundTasks,
        symbols: Annotated[list[str], Form()],
        window: Annotated[int | None, Form()] = None,
    ) -> RedirectResponse:
        if not symbols:
            raise HTTPException(status_code=400, detail="No symbols selected")
        window_size = window if window is not None else settings.option_chain_window
        job_id = repo.create_job(c, symbols, window_size=window_size)
        from options_earnings.options.job import run_chain_job

        background_tasks.add_task(
            run_chain_job, str(settings.db_path), job_id, window=window_size
        )
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def get_job(request: Request, c: Conn, job_id: UUID) -> HTMLResponse:
        job = repo.get_job(c, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        atm: list[Any] = []
        moves: dict[str, Any] = {}
        if job.status == "done":
            atm = repo.atm_quotes_for_job(c, job_id)
            for sym in {q.symbol for q in atm}:
                moves[sym] = repo.latest_earnings_move(c, sym)
        return templates.TemplateResponse(
            request,
            "chain_results.html",
            {"job": job, "atm": atm, "moves": moves},
        )

    @app.get("/jobs/{job_id}/status", response_class=HTMLResponse)
    def get_job_status(request: Request, c: Conn, job_id: UUID) -> HTMLResponse:
        job = repo.get_job(c, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        response = templates.TemplateResponse(
            request,
            "_status_fragment.html",
            {"job": job},
        )
        if job.status in ("done", "error"):
            response.headers["HX-Refresh"] = "true"
        return response

    @app.get("/jobs/{job_id}/{symbol}", response_class=HTMLResponse)
    def get_job_symbol(
        request: Request, c: Conn, job_id: UUID, symbol: str
    ) -> HTMLResponse:
        job = repo.get_job(c, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        all_quotes = [q for q in repo.quotes_for_job(c, job_id) if q.symbol == symbol]
        # Group by expiry then strike with C/P side-by-side.
        by_expiry: dict[date, dict[float, dict[str, Any]]] = {}
        underlying = None
        for q in all_quotes:
            underlying = q.underlying
            row = by_expiry.setdefault(q.expiry, {}).setdefault(q.strike, {})
            row[q.cp] = q
        grid = []
        for exp in sorted(by_expiry.keys()):
            strikes = sorted(by_expiry[exp].keys())
            grid.append(
                {
                    "expiry": exp,
                    "rows": [{"strike": s, **by_expiry[exp][s]} for s in strikes],
                }
            )
        recent_moves_anchor = repo.recent_earnings_moves(c, symbol, limit=8)

        chosen_expiry = all_quotes[0].expiry if all_quotes else None
        sym_row = repo.get_symbol(c, symbol)
        next_earnings = sym_row.next_earnings if sym_row else None
        if chosen_expiry is not None and next_earnings is not None:
            window_days = max(1, (chosen_expiry - next_earnings).days)
        else:
            window_days = 3

        from options_earnings.ingest.earnings_history import compute_move_from_ohlc
        ohlc_rows = repo.ohlc_for_symbol(c, symbol)
        recent_moves: list[Any] = []
        for anchor in recent_moves_anchor:
            recomputed = compute_move_from_ohlc(
                symbol, anchor.earnings_date, window_days, ohlc_rows
            )
            if recomputed is not None:
                recent_moves.append(recomputed)
            else:
                # No OHLC stored yet — fall back to the cached 3-day row.
                recent_moves.append(anchor)

        atm_strike: float | None = None
        atm_call_last: float | None = None
        atm_put_last: float | None = None
        if all_quotes and underlying is not None:
            chosen_expiry = all_quotes[0].expiry
            same_expiry = [q for q in all_quotes if q.expiry == chosen_expiry]
            strikes = sorted({q.strike for q in same_expiry})
            if strikes:
                atm_strike = min(strikes, key=lambda s: abs(s - underlying))
                for q in same_expiry:
                    if q.strike == atm_strike and q.last is not None:
                        if q.cp == "C":
                            atm_call_last = q.last
                        elif q.cp == "P":
                            atm_put_last = q.last

        call_ratio_pct = (
            atm_call_last / atm_strike * 100.0
            if atm_call_last is not None and atm_strike not in (None, 0)
            else None
        )
        put_ratio_pct = (
            atm_put_last / atm_strike * 100.0
            if atm_put_last is not None and atm_strike not in (None, 0)
            else None
        )

        return templates.TemplateResponse(
            request,
            "chain_detail.html",
            {
                "job": job,
                "symbol": symbol,
                "grid": grid,
                "underlying": underlying,
                "recent_moves": recent_moves,
                "atm_strike": atm_strike,
                "atm_call_last": atm_call_last,
                "atm_put_last": atm_put_last,
                "call_ratio_pct": call_ratio_pct,
                "put_ratio_pct": put_ratio_pct,
                "next_earnings": next_earnings,
                "chosen_expiry": chosen_expiry,
                "window_days": window_days,
            },
        )

    def _iv_history_data(
        c: duckdb.DuckDBPyConnection,
        symbol: str,
        mode: str,
        cp: str,
        expiry: date | None,
    ) -> list[dict[str, Any]]:
        raw: list[dict[str, Any]] = []
        try:
            from options_earnings.options.history import (
                iv_history_fixed_strike,
                iv_history_rolling_atm,
                nearest_strike_today,
            )
            if mode == "rolling":
                raw = iv_history_rolling_atm(c, symbol, cp=cp, expiry=expiry)
            else:
                strike = nearest_strike_today(c, symbol, cp=cp, expiry=expiry)
                if strike is None:
                    return []
                raw = iv_history_fixed_strike(c, symbol, strike, cp=cp, expiry=expiry)
        except Exception:
            return []

        out: list[dict[str, Any]] = []
        for d in raw:
            ts = d.get("snapshot_ts")
            ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            out.append(
                {
                    "snapshot_ts": ts_str,
                    "iv": d.get("iv_computed"),
                    "strike": d.get("strike"),
                    "underlying": d.get("underlying"),
                }
            )
        return out

    @app.get("/symbols/{symbol}/iv-history", response_class=HTMLResponse)
    def iv_history_page(
        request: Request,
        c: Conn,
        symbol: str,
        mode: str = "rolling",
        cp: str = "C",
        expiry: date | None = None,
    ) -> HTMLResponse:
        data = _iv_history_data(c, symbol, mode, cp, expiry)
        expiries = repo.expiries_for_symbol(c, symbol)
        return templates.TemplateResponse(
            request,
            "iv_history.html",
            {
                "symbol": symbol,
                "mode": mode,
                "cp": cp,
                "expiry": expiry,
                "data": data,
                "expiries": expiries,
            },
        )

    @app.get("/symbols/{symbol}/iv-history.json")
    def iv_history_json(
        c: Conn,
        symbol: str,
        mode: str = "rolling",
        cp: str = "C",
        expiry: date | None = None,
    ) -> JSONResponse:
        data = _iv_history_data(c, symbol, mode, cp, expiry)
        return JSONResponse(content=data)

    @app.post("/refresh")
    def post_refresh(background_tasks: BackgroundTasks) -> RedirectResponse:
        from options_earnings.db.connection import get_conn as _get_conn
        from options_earnings.ingest.runner import refresh_all

        def _run_refresh() -> None:
            with _get_conn(settings.db_path) as bg_conn:
                refresh_all(bg_conn)

        background_tasks.add_task(_run_refresh)
        return RedirectResponse(url="/", status_code=303)

    return app


_app: FastAPI | None = None


def _build_production_app() -> FastAPI:
    settings = get_settings()
    conn = open_db(settings.db_path)
    app = create_app(conn)

    scheduler_holder: dict[str, Any] = {}

    @app.on_event("startup")
    def _startup() -> None:
        from options_earnings.jobs.scheduler import start_scheduler
        sched = start_scheduler(settings)
        if sched is not None:
            scheduler_holder["sched"] = sched

    @app.on_event("shutdown")
    def _shutdown() -> None:
        sched = scheduler_holder.get("sched")
        if sched is not None:
            sched.shutdown(wait=False)
        conn.close()

    return app


def __getattr__(name: str) -> Any:
    """Lazy module-level `app` so importing this module never opens a DB.

    Production servers (uvicorn) reference `options_earnings.web.app:app`,
    which triggers this and opens the connection on first access. The
    production app additionally wires APScheduler (opt-in via settings).
    """
    global _app
    if name == "app":
        if _app is None:
            _app = _build_production_app()
        return _app
    raise AttributeError(name)
