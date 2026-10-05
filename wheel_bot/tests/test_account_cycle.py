from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from support import WHEEL_BOT_DIR
from account_cycle import evaluate_account
from portfolio_policy import allocation_summary
from performance import summarize_history


class AccountCycleTests(unittest.TestCase):
    def portfolio(self, **changes):
        return dict(cash=1_000_000, nlv=1_000_000, shares=0, ticker="NONE",
                    positions_complete=True, equity_positions=[], short_puts=[],
                    short_calls=[], short_put_collateral=0, unsupported_positions=[]) | changes

    def test_held_put_does_not_hide_remaining_cash_or_existing_exposure(self):
        pf = self.portfolio(short_puts=[{"underlying": "AAPL", "qty": 1, "collateral": 20000}], short_put_collateral=20000)
        calls = []

        def run(raw, **kwargs):
            calls.append((json.loads(raw), kwargs))
            return {"draft_ticket": json.dumps({"action": "NO_TRADE", "reason": "Hold"})}

        result = evaluate_account(json.dumps(pf), run_flow=run, thread_id="scan")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0]["evaluation_scope"], "entry")
        self.assertEqual(calls[1][0]["short_puts"], pf["short_puts"])
        self.assertEqual(calls[1][0]["short_put_collateral"], 20000)
        self.assertNotEqual(calls[0][1]["thread_id"], calls[1][1]["thread_id"])
        self.assertEqual(result["allocation_summary"]["remaining_csp_budget"], 480000)

    def test_scans_other_stock_when_largest_has_no_call_opportunity(self):
        stocks = [dict(ticker=t, shares=100, spot=p, cost_basis=p, market_value=100*p)
                  for t, p in (("MSFT", 500), ("AAPL", 200))]
        seen = []

        def run(raw, **_):
            view = json.loads(raw)
            seen.append(view)
            action = "SELL_COVERED_CALL" if view["ticker"] == "AAPL" else "NO_TRADE"
            return {"draft_ticket": json.dumps({"action": action})}

        result = evaluate_account(json.dumps(self.portfolio(equity_positions=stocks)), run_flow=run)
        self.assertEqual([v["ticker"] for v in seen], ["MSFT", "AAPL"])
        self.assertEqual(seen[1]["equity_positions"], stocks)
        self.assertEqual(json.loads(result["draft_ticket"])["action"], "SELL_COVERED_CALL")

    def test_close_or_failure_stops_before_entry(self):
        pf = self.portfolio(short_puts=[{"underlying": "AAPL", "qty": 1}], short_put_collateral=20000)
        for response in (
            {"draft_ticket": '{"action":"CLOSE_SHORT_PUT"}'},
            {"abort_reason": "CRO rejected"},
            {"data_gate_status": "blocked", "data_gate_reason": "Missing quotes"},
        ):
            calls = []
            result = evaluate_account(json.dumps(pf), run_flow=lambda *a, **k: calls.append(a) or response)
            self.assertEqual(len(calls), 1)
            for key in response:
                self.assertEqual(result[key], response[key])

    def test_review_required_and_full_allocation_block_new_entries(self):
        for collateral, review in ((500000, False), (20000, True)):
            calls = []
            response = {"draft_ticket": json.dumps({"action": "NO_TRADE", "manual_review_required": review, "reason": "Hold / review"})}
            pf = self.portfolio(short_puts=[{"underlying": "AAPL", "qty": 1}], short_put_collateral=collateral)
            result = evaluate_account(json.dumps(pf), run_flow=lambda *a, **k: calls.append(a) or response)
            self.assertEqual(len(calls), 1)
            self.assertEqual(json.loads(result["draft_ticket"])["action"], "NO_TRADE")

    def test_target_is_reported_separately_from_premium_illustration(self):
        summary = allocation_summary(self.portfolio(short_put_collateral=150000))
        self.assertEqual(summary["annual_account_return_target_pct"], [25, 30])
        self.assertEqual(summary["premium_only_full_utilization_illustration_pct"], [10, 17.5])
        self.assertEqual(summary["csp_utilization_pct"], 15)


class PerformanceTests(unittest.TestCase):
    def test_uses_broker_pnl_instead_of_equity_growth_or_short_period_annualization(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = dict(timestamp=[start.timestamp(), (start+timedelta(days=30)).timestamp()],
                       equity=[100000, 151000], profit_loss=[0, 1000], profit_loss_pct=[0, .01])
        result = summarize_history(history)
        self.assertEqual(result["broker_reported_period_return_pct"], 1)
        self.assertEqual(result["observed_days"], 30)
        self.assertLess(result["compounded_objective_for_observed_days_pct"][0], 2)
        self.assertNotIn("annualized_return_pct", result)

    def test_incomplete_or_nonfinite_history_is_unavailable(self):
        for history in ({}, dict(timestamp=[1, 2], equity=[100, 100], profit_loss=[0, 0], profit_loss_pct=[0, float("nan")])):
            with self.assertRaises(ValueError):
                summarize_history(history)


if __name__ == "__main__":
    unittest.main()
