from __future__ import annotations

import argparse
import logging
import sys

from options_earnings.config import get_settings
from options_earnings.db.connection import get_conn
from options_earnings.ingest.runner import (
    refresh_all,
    refresh_missing_data,
    refresh_missing_earnings,
)

logger = logging.getLogger(__name__)


def _cmd_refresh(args: argparse.Namespace) -> int:
    settings = get_settings()
    fetch_chains = None if not args.no_chains else False
    with get_conn(settings.db_path) as conn:
        count = refresh_all(
            conn,
            max_workers=args.workers,
            limit=args.limit,
            fetch_chains=fetch_chains,
        )
    logger.info("refresh complete: %d symbols", count)
    print(f"Upserted {count} symbols.")
    return 0


def _cmd_refresh_earnings(args: argparse.Namespace) -> int:
    settings = get_settings()
    with get_conn(settings.db_path) as conn:
        n = refresh_missing_earnings(
            conn, max_workers=args.workers, retries=args.retries
        )
    print(f"Refilled earnings for {n} symbols.")
    return 0


def _cmd_refresh_missing(args: argparse.Namespace) -> int:
    settings = get_settings()
    with get_conn(settings.db_path) as conn:
        p, e = refresh_missing_data(
            conn, max_workers=args.workers, retries=args.retries
        )
    print(f"Backfilled price/mcap for {p} symbols, earnings for {e} symbols.")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    settings = get_settings()
    host = args.host or settings.web_host
    port = args.port or settings.web_port
    uvicorn.run(
        "options_earnings.web.app:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="options-earnings",
        description="S&P 500 earnings + option-chain workbench",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_refresh = sub.add_parser("refresh", help="Refresh S&P 500 symbols table")
    p_refresh.add_argument(
        "--limit", type=int, default=None, help="Process only the first N symbols"
    )
    p_refresh.add_argument(
        "--workers", type=int, default=8, help="Max parallel network workers"
    )
    p_refresh.add_argument(
        "--no-chains", action="store_true",
        help="Skip the large-cap option-chain fetch after the symbol refresh",
    )
    p_refresh.set_defaults(func=_cmd_refresh)

    p_re = sub.add_parser(
        "refresh-earnings",
        help="Retry earnings fetch for symbols with NULL next_earnings (low concurrency, with retry)",
    )
    p_re.add_argument("--workers", type=int, default=4)
    p_re.add_argument("--retries", type=int, default=2)
    p_re.set_defaults(func=_cmd_refresh_earnings)

    p_rm = sub.add_parser(
        "refresh-missing",
        help="Retry price/mcap/earnings fetch for symbols with any NULL field (low concurrency, with retry)",
    )
    p_rm.add_argument("--workers", type=int, default=4)
    p_rm.add_argument("--retries", type=int, default=2)
    p_rm.set_defaults(func=_cmd_refresh_missing)

    p_serve = sub.add_parser("serve", help="Start the FastAPI web app")
    p_serve.add_argument("--host", default=None, help="Bind host (default from settings)")
    p_serve.add_argument("--port", type=int, default=None, help="Bind port (default from settings)")
    p_serve.add_argument("--reload", action="store_true", help="Enable autoreload (dev)")
    p_serve.set_defaults(func=_cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
