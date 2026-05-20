"""Send one test email with Account Overview + Strategy input + Pipeline (optional).

If Alpaca credentials are valid, uses live data. Otherwise uses demo JSON so you
can still verify the email template.

Usage:
  cd wheel_bot && uv run python scripts/send_test_email.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logging.getLogger("data_feeds").setLevel(logging.CRITICAL)

# wheel_bot on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_feeds import fetch_account_summary, fetch_portfolio
from notifier import send_run_report

_DEMO_PORTFOLIO = json.dumps(
    {
        "ticker": "AAPL",
        "spot": 175.0,
        "cost_basis": 170.0,
        "cash": 5000.0,
        "shares": 100,
    }
)


def main() -> None:
    portfolio_json = _DEMO_PORTFOLIO
    ticker = "AAPL"

    try:
        portfolio_json = fetch_portfolio()
        d = json.loads(portfolio_json)
        ticker = str(d.get("ticker") or ticker)
    except Exception as exc:
        print(f"fetch_portfolio failed (using demo JSON): {exc}")

    acct = fetch_account_summary()

    result = (
        "MACRO_SENTINEL:\n"
        '{"status":"CLEAR","reason":"Full template test — not a live trade."}\n\n'
        "ORCHESTRATOR:\n"
        '{"route_to":"ASSET_NOMINAL","action":"Covered call path (test)."}\n\n'
        "DRAFT_TICKET:\n"
        '{"action":"SELL_COVERED_CALL","risk_reducing":true,"position_pct":77.8,"nlv":22500.0}\n\n'
        "CHIEF_RISK_OFFICER:\n"
        '{"status":"APPROVED","reason":"Test email only."}\n\n'
        "EXECUTION_BROKER:\n"
        '{"initial_limit":1.18,"step_down":0.02,"floor_price":1.12}'
    )

    ok = send_run_report(
        result,
        run_label="full-test",
        ticker=ticker,
        account_snapshot=acct,
        portfolio_json=portfolio_json,
    )
    print("send_run_report:", ok)
    if not ok:
        sys.exit(1)
    print("Check your inbox (and spam) for: Wheel Bot [FULL-TEST]")


if __name__ == "__main__":
    main()
