#!/usr/bin/env python
"""Daily run entrypoint. Point cron / Task Scheduler / a cloud function at this.

    python run_daily.py                     # full run
    python run_daily.py --offline           # fixture data, no network
    python run_daily.py --only MSFT,KO      # subset
    python run_daily.py --no-news           # skip the sentiment leg entirely
    python run_daily.py --dry-run           # no push notification

The phone is a display client. It never runs this.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from stockbot.config import load_config  # noqa: E402
from stockbot.logging_setup import setup_logging  # noqa: E402
from stockbot.output.report import render_terminal  # noqa: E402
from stockbot.pipeline import run_daily  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily portfolio & stock analysis run")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--offline", action="store_true", help="use fixture market data")
    parser.add_argument("--only", default=None, help="comma-separated ticker subset")
    parser.add_argument("--no-news", action="store_true", help="skip the news/sentiment leg")
    parser.add_argument("--dry-run", action="store_true", help="do not send a push notification")
    parser.add_argument("--local-portfolio", action="store_true", help="force data/portfolio.json")
    parser.add_argument("--quiet", action="store_true", help="warnings and errors only")
    args = parser.parse_args()

    setup_logging(logging.WARNING if args.quiet else logging.INFO)
    cfg = load_config(args.config)

    only = [t.strip() for t in args.only.split(",")] if args.only else None

    report = run_daily(
        cfg,
        offline=args.offline,
        only=only,
        skip_news=args.no_news,
        skip_push=args.dry_run,
        prefer_local_portfolio=args.local_portfolio,
    )

    print(render_terminal(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
