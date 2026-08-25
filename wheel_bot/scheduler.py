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
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config import (
    WHEEL_BOT_ALLOW_LIVE_TRADING,
    WHEEL_BOT_AUTO_EXECUTE,
    WHEEL_BOT_RUN_ON_START,
)
from data_feeds import (
    compute_liquidation_snapshot,
    fetch_account_summary,
    fetch_candidate_universe,
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


def _execution_attempt(
    graph_state: dict[str, Any],
    *,
    auto_execute: bool,
    allow_live_trading: bool,
) -> dict[str, Any]:
    """Optionally submit the approved graph ticket to Alpaca."""
    if graph_state.get("abort_reason"):
        return {
            "status": "BLOCKED",
            "reason": f"Graph aborted: {graph_state.get('abort_reason')}",
        }

    if not auto_execute:
        return {
            "status": "SKIPPED",
            "reason": "WHEEL_BOT_AUTO_EXECUTE is false; no order submitted.",
        }

    try:
        cro = json.loads(graph_state.get("cro_output") or "{}")
    except json.JSONDecodeError:
        cro = {}

    if str(cro.get("status", "")).upper() != "APPROVED":
        return {
            "status": "BLOCKED",
            "reason": "CRO did not approve the ticket.",
            "cro_output": graph_state.get("cro_output", ""),
        }

    draft_ticket = (graph_state.get("draft_ticket") or "").strip()
    execution_output = (graph_state.get("execution_output") or "").strip()
    if not draft_ticket or not execution_output:
        return {
            "status": "BLOCKED",
            "reason": "Missing draft_ticket or execution_output.",
        }

    try:
        from broker import WheelBroker

        broker = WheelBroker()
        order = broker.execute(
            draft_ticket,
            execution_output,
            human_approved=True,
            allow_live_trading=allow_live_trading,
        )
        return order.to_dict()
    except Exception as exc:
        logger.exception("Auto-execution failed")
        return {"status": "FAILED", "reason": str(exc)}


def _load_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _build_transaction_summary(
    *,
    graph_state: dict[str, Any] | None = None,
    order_result: dict[str, Any] | None = None,
    no_transaction_reason: str = "",
) -> dict[str, Any]:
    """Explain whether a trigger produced an order, and why."""
    graph_state = graph_state or {}
    order_result = order_result or {}

    draft_ticket = _load_json_object(graph_state.get("draft_ticket"))
    cro_output = _load_json_object(graph_state.get("cro_output"))
    execution_output = _load_json_object(graph_state.get("execution_output"))

    status = str(order_result.get("status") or "SKIPPED")
    transaction_made = status == "SUBMITTED"
    action = str(draft_ticket.get("action") or "N/A")
    symbol = (
        execution_output.get("symbol")
        or draft_ticket.get("symbol")
        or draft_ticket.get("ticker")
        or "N/A"
    )

    cro_reason = str(cro_output.get("reason") or "").strip()
    broker_reason = str(order_result.get("reason") or "").strip()

    if transaction_made:
        why_parts = []
        if cro_reason:
            why_parts.append(f"CRO approved: {cro_reason}")
        if broker_reason:
            why_parts.append(broker_reason)
        why = "; ".join(why_parts) or "CRO approved and Alpaca accepted the order."
    else:
        why = (
            no_transaction_reason.strip()
            or broker_reason
            or str(graph_state.get("abort_reason") or "").strip()
            or "No executable order was submitted."
        )
        if cro_reason and not broker_reason and not no_transaction_reason:
            why = f"CRO result: {cro_reason}"

    return {
        "transaction_made": transaction_made,
        "status": status,
        "action": action,
        "symbol": symbol,
        "order_id": order_result.get("order_id"),
        "why": why,
        "cro_reason": cro_reason,
        "broker_reason": broker_reason,
    }


def _format_transaction_summary(summary: dict[str, Any]) -> str:
    outcome = (
        "Transaction made"
        if summary.get("transaction_made")
        else "No transaction made"
    )
    lines = [
        f"Outcome: {outcome}",
        f"Status: {summary.get('status', 'N/A')}",
        f"Action: {summary.get('action', 'N/A')}",
        f"Symbol: {summary.get('symbol', 'N/A')}",
    ]
    if summary.get("order_id"):
        lines.append(f"Order ID: {summary['order_id']}")
    lines.append(f"Why: {summary.get('why', '')}")
    return "\n".join(lines)


def _send_trigger_report(
    result: str,
    *,
    run_label: str,
    ticker: str,
    portfolio_json: str | None = None,
    graph_state: dict[str, Any] | None = None,
    order_result: dict[str, Any] | None = None,
    no_transaction_reason: str = "",
) -> None:
    summary = _build_transaction_summary(
        graph_state=graph_state,
        order_result=order_result,
        no_transaction_reason=no_transaction_reason,
    )

    report = (
        f"{result}\n\nTRANSACTION_SUMMARY:\n"
        f"{_format_transaction_summary(summary)}"
    )
    if order_result is not None:
        report = (
            f"{report}\n\nORDER_EXECUTION:\n"
            f"{json.dumps(order_result, indent=2, sort_keys=True)}"
        )

    logger.info("=== Result ===\n%s", report)

    account = fetch_account_summary()
    send_run_report(
        report,
        run_label=run_label,
        ticker=ticker,
        account_snapshot=account,
        portfolio_json=portfolio_json,
        transaction_summary=summary,
    )


def run_wheel(
    *,
    ticker: str | None = None,
    dry_run: bool = False,
    run_label: str = "scheduled",
    auto_execute: bool = WHEEL_BOT_AUTO_EXECUTE,
    allow_live_trading: bool = WHEEL_BOT_ALLOW_LIVE_TRADING,
) -> None:
    """Execute one full pass of the Wheel pipeline."""
    now = datetime.now(ET)

    if not is_market_day(now.date()):
        reason = f"Not a market day ({now.date()}); scheduler trigger skipped trading."
        logger.info("Skipping %s run — %s", run_label, reason)
        _send_trigger_report(
            "TRIGGER:\nScheduler fired, but trading was skipped.",
            run_label=run_label,
            ticker=ticker or "auto",
            no_transaction_reason=reason,
        )
        return

    logger.info(
        (
            "=== Wheel %s run started at %s ET  ticker=%s  "
            "dry_run=%s  auto_execute=%s ==="
        ),
        run_label,
        now.strftime("%Y-%m-%d %H:%M:%S"),
        ticker or "auto",
        dry_run,
        auto_execute,
    )

    try:
        portfolio_json = fetch_portfolio(ticker)
        logger.info("Portfolio: %s", portfolio_json)
    except Exception as exc:
        logger.exception("Failed to fetch portfolio — aborting run")
        _send_trigger_report(
            "ABORT:\nFailed to fetch portfolio from Alpaca.",
            run_label=run_label,
            ticker=ticker or "auto",
            no_transaction_reason=f"Portfolio fetch failed before any LLM or broker call: {exc}",
        )
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
        candidate_tickers = [resolved_ticker] if resolved_ticker != "NONE" else None
        candidate_universe = fetch_candidate_universe(candidate_tickers)
        logger.info("Candidate universe: %s", candidate_universe)
    except Exception:
        logger.exception("Failed to fetch candidate universe")
        candidate_universe = "[]"

    try:
        fundamentals = fetch_fundamentals(candidate_tickers)
        logger.info("Fundamentals: %s", fundamentals)
    except Exception:
        logger.exception("Failed to fetch fundamentals")
        fundamentals = candidate_universe or "[]"

    try:
        short_put_tickers = {
            str(item.get("underlying") or "").upper()
            for item in (pf.get("short_puts") or [])
            if isinstance(item, dict) and item.get("underlying")
        }
        chain_tickers = sorted(short_put_tickers)
        if not chain_tickers and resolved_ticker != "NONE":
            chain_tickers = [resolved_ticker]
        chain = "\n".join(fetch_options_chain(symbol) for symbol in chain_tickers)
    except Exception:
        logger.exception("Failed to fetch options chain")
        chain = ""

    liquidation = compute_liquidation_snapshot(portfolio_json)

    if dry_run:
        reason = "Dry run requested; inputs were assembled but LLM and broker execution were skipped."
        logger.info("DRY RUN — inputs assembled, skipping LangGraph execution")
        logger.info("  portfolio_state: %s", portfolio_json)
        logger.info("  macro_input: %s", macro)
        logger.info("  candidate_universe_input: %s", candidate_universe)
        logger.info("  fundamentals_input: %s", fundamentals)
        logger.info("  options_chain_input: %s", chain[:200])
        logger.info("  liquidation_input: %s", liquidation)
        _send_trigger_report(
            (
                "DRY_RUN:\n"
                f"portfolio_state: {portfolio_json}\n"
                f"macro_input: {macro}\n"
                f"candidate_universe_input: {candidate_universe}\n"
                f"fundamentals_input: {fundamentals}\n"
                f"options_chain_input: {chain[:1000]}\n"
                f"liquidation_input: {liquidation}"
            ),
            run_label=run_label,
            ticker=resolved_ticker,
            portfolio_json=portfolio_json,
            no_transaction_reason=reason,
        )
        return

    DB_DIR.mkdir(exist_ok=True)

    from agents.graph import format_trading_flow_state, run_trading_flow_state

    thread_id = f"wheel-{now.strftime('%Y%m%d-%H%M%S')}-{run_label}"

    try:
        graph_state = run_trading_flow_state(
            portfolio_json,
            macro_input=macro,
            candidate_universe_input=candidate_universe,
            fundamentals_input=fundamentals,
            options_chain_input=chain,
            liquidation_input=liquidation,
            thread_id=thread_id,
            checkpoint_db=CHECKPOINT_DB,
        )
    except Exception as exc:
        logger.exception("LangGraph execution failed — aborting run")
        _send_trigger_report(
            "ABORT:\nLangGraph execution failed.",
            run_label=run_label,
            ticker=resolved_ticker,
            portfolio_json=portfolio_json,
            no_transaction_reason=f"LLM graph failed before a broker order could be evaluated: {exc}",
        )
        return

    result = format_trading_flow_state(graph_state)

    order_result = _execution_attempt(
        graph_state,
        auto_execute=auto_execute,
        allow_live_trading=allow_live_trading,
    )
    _send_trigger_report(
        result,
        run_label=run_label,
        ticker=resolved_ticker,
        portfolio_json=portfolio_json,
        graph_state=graph_state,
        order_result=order_result,
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
    parser.add_argument(
        "--run-on-start",
        action="store_true",
        default=WHEEL_BOT_RUN_ON_START,
        help="Run once immediately when the scheduler starts, then keep scheduling",
    )
    parser.add_argument(
        "--auto-execute",
        action="store_true",
        default=WHEEL_BOT_AUTO_EXECUTE,
        help="Submit CRO-approved tickets to Alpaca",
    )
    parser.add_argument(
        "--allow-live-trading",
        action="store_true",
        default=WHEEL_BOT_ALLOW_LIVE_TRADING,
        help="Permit auto-execution when ALPACA_PAPER_TRADE=False",
    )
    args = parser.parse_args()

    _setup_logging()

    if args.once:
        logger.info("Single immediate run requested")
        run_wheel(
            ticker=args.ticker,
            dry_run=args.dry_run,
            run_label="manual",
            auto_execute=args.auto_execute,
            allow_live_trading=args.allow_live_trading,
        )
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
            "auto_execute": args.auto_execute,
            "allow_live_trading": args.allow_live_trading,
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
                "auto_execute": args.auto_execute,
                "allow_live_trading": args.allow_live_trading,
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

    if args.run_on_start:
        logger.info("Startup run requested")
        run_wheel(
            ticker=args.ticker,
            dry_run=args.dry_run,
            run_label="startup",
            auto_execute=args.auto_execute,
            allow_live_trading=args.allow_live_trading,
        )

    logger.info(
        "Scheduler starting with %d configured job(s).",
        len(scheduler.get_jobs()),
    )
    scheduler.start()


if __name__ == "__main__":
    main()
