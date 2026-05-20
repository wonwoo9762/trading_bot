"""Scheduler for the Wheel trading bot.

Runs the full LangGraph pipeline on a cron schedule aligned to market hours.

Default schedule (US/Eastern):
  - 09:45 AM  — primary scan (15 min after open; volatility settled)
  - 03:30 PM  — pre-close check (catch expirations, intraday distress)

Skips weekends and major NYSE holidays automatically.

Usage:
  uv run python scheduler.py                  # run with defaults
  uv run python scheduler.py --once           # single immediate run then exit
  uv run python scheduler.py --dry-run        # log what would happen, skip LLM
  uv run python scheduler.py --ticker AAPL    # focus on a specific underlying
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from data_feeds import (
    compute_liquidation_snapshot,
    fetch_account_summary,
    fetch_fundamentals,
    fetch_macro,
    fetch_options_chain,
    fetch_portfolio,
    is_market_day,
)
from notifier import send_run_report

ET = ZoneInfo("America/New_York")
DB_DIR = Path(__file__).resolve().parent / "data"
CHECKPOINT_DB = str(DB_DIR / "wheel_checkpoints.db")

logger = logging.getLogger("wheel_scheduler")


def _setup_logging() -> None:
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"wheel_{date.today().isoformat()}.log"

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)


def run_wheel(
    *,
    ticker: str | None = None,
    dry_run: bool = False,
    run_label: str = "scheduled",
) -> None:
    """Execute one full pass of the Wheel pipeline."""
    now = datetime.now(ET)

    if not is_market_day(now.date()):
        logger.info("Skipping %s run — not a market day (%s)", run_label, now.date())
        return

    logger.info(
        "=== Wheel %s run started at %s ET  ticker=%s  dry_run=%s ===",
        run_label,
        now.strftime("%Y-%m-%d %H:%M:%S"),
        ticker or "auto",
        dry_run,
    )

    try:
        portfolio_json = fetch_portfolio(ticker)
        logger.info("Portfolio: %s", portfolio_json)
    except Exception:
        logger.exception("Failed to fetch portfolio — aborting run")
        return

    pf = json.loads(portfolio_json)
    resolved_ticker = pf.get("ticker", "NONE")

    try:
        macro = fetch_macro()
        logger.info("Macro: %s", macro)
    except Exception:
        logger.exception("Failed to fetch macro data")
        macro = ""

    try:
        fundamentals = fetch_fundamentals(
            [resolved_ticker] if resolved_ticker != "NONE" else None
        )
    except Exception:
        logger.exception("Failed to fetch fundamentals")
        fundamentals = "[]"

    try:
        chain = fetch_options_chain(resolved_ticker) if resolved_ticker != "NONE" else ""
    except Exception:
        logger.exception("Failed to fetch options chain")
        chain = ""

    liquidation = compute_liquidation_snapshot(portfolio_json)

    if dry_run:
        logger.info("DRY RUN — inputs assembled, skipping LangGraph execution")
        logger.info("  portfolio_state: %s", portfolio_json)
        logger.info("  macro_input: %s", macro)
        logger.info("  fundamentals_input: %s", fundamentals)
        logger.info("  options_chain_input: %s", chain[:200])
        logger.info("  liquidation_input: %s", liquidation)
        return

    DB_DIR.mkdir(exist_ok=True)

    from agents.graph import run_trading_flow

    thread_id = f"wheel-{now.strftime('%Y%m%d')}-{run_label}"

    result = run_trading_flow(
        portfolio_json,
        macro_input=macro,
        fundamentals_input=fundamentals,
        options_chain_input=chain,
        liquidation_input=liquidation,
        thread_id=thread_id,
        checkpoint_db=CHECKPOINT_DB,
    )

    logger.info("=== Result ===\n%s", result)

    account = fetch_account_summary()
    send_run_report(
        result,
        run_label=run_label,
        ticker=resolved_ticker,
        account_snapshot=account,
        portfolio_json=portfolio_json,
    )

    logger.info("=== Wheel %s run complete ===", run_label)


def main() -> None:
    parser = argparse.ArgumentParser(description="Wheel trading bot scheduler")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once immediately and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch data but skip LangGraph execution",
    )
    parser.add_argument(
        "--ticker",
        type=str,
        default=None,
        help="Focus on a specific ticker (default: largest position)",
    )
    parser.add_argument(
        "--morning",
        type=str,
        default="9:45",
        help="Morning run time in HH:MM ET (default: 9:45)",
    )
    parser.add_argument(
        "--afternoon",
        type=str,
        default="15:30",
        help="Afternoon run time in HH:MM ET (default: 15:30)",
    )
    parser.add_argument(
        "--no-afternoon",
        action="store_true",
        help="Skip the afternoon run (morning only)",
    )
    args = parser.parse_args()

    _setup_logging()

    if args.once:
        logger.info("Single immediate run requested")
        run_wheel(ticker=args.ticker, dry_run=args.dry_run, run_label="manual")
        return

    scheduler = BlockingScheduler(timezone=ET)

    m_hour, m_min = args.morning.split(":")
    scheduler.add_job(
        run_wheel,
        CronTrigger(
            hour=int(m_hour),
            minute=int(m_min),
            day_of_week="mon-fri",
            timezone=ET,
        ),
        kwargs={
            "ticker": args.ticker,
            "dry_run": args.dry_run,
            "run_label": "morning",
        },
        id="morning_scan",
        name="Morning Wheel Scan",
        misfire_grace_time=300,
    )
    logger.info("Morning scan scheduled for %s ET (Mon-Fri)", args.morning)

    if not args.no_afternoon:
        a_hour, a_min = args.afternoon.split(":")
        scheduler.add_job(
            run_wheel,
            CronTrigger(
                hour=int(a_hour),
                minute=int(a_min),
                day_of_week="mon-fri",
                timezone=ET,
            ),
            kwargs={
                "ticker": args.ticker,
                "dry_run": args.dry_run,
                "run_label": "preclose",
            },
            id="preclose_scan",
            name="Pre-Close Wheel Scan",
            misfire_grace_time=300,
        )
        logger.info(
            "Pre-close scan scheduled for %s ET (Mon-Fri)", args.afternoon
        )

    def _shutdown(signum, _frame):
        logger.info("Signal %s received — shutting down scheduler", signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info(
        "Scheduler started.  Next run: %s",
        scheduler.get_jobs()[0].next_run_time,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
