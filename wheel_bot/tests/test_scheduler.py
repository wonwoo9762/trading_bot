from __future__ import annotations

import json
import sys
import types
import unittest
from unittest import mock

from support import fresh_import, install_apscheduler_stubs

install_apscheduler_stubs()

import scheduler


class SchedulerTests(unittest.TestCase):
    def test_scheduler_starts_without_reading_uninitialized_next_run_time(self):
        class JobWithoutNextRunTime:
            pass

        class FakeScheduler:
            def __init__(self, timezone=None):
                self.jobs = []

            def add_job(self, *args, **kwargs):
                self.jobs.append(JobWithoutNextRunTime())

            def get_jobs(self):
                return self.jobs

            def start(self):
                self.started = True

            def shutdown(self, wait=False):
                pass

        with mock.patch.object(sys, "argv", ["scheduler.py", "--no-afternoon"]):
            with mock.patch.object(scheduler, "_setup_logging"):
                with mock.patch.object(scheduler, "BlockingScheduler", FakeScheduler):
                    with mock.patch.object(scheduler.signal, "signal"):
                        scheduler.main()

    def test_transaction_summary_for_submitted_order(self):
        summary = scheduler._build_transaction_summary(
            graph_state={
                "draft_ticket": json.dumps({"action": "SELL_COVERED_CALL", "ticker": "AAPL"}),
                "cro_output": json.dumps({"status": "APPROVED", "reason": "Risk reducing"}),
                "execution_output": json.dumps({"symbol": "AAPL260116C00170000"}),
            },
            order_result={
                "status": "SUBMITTED",
                "order_id": "order-123",
                "reason": "Option limit order submitted.",
            },
        )

        self.assertTrue(summary["transaction_made"])
        self.assertEqual(summary["symbol"], "AAPL260116C00170000")
        self.assertIn("CRO approved: Risk reducing", summary["why"])
        self.assertIn("Option limit order submitted.", summary["why"])

    def test_transaction_summary_for_no_trade_reason(self):
        summary = scheduler._build_transaction_summary(
            no_transaction_reason="Not a market day."
        )

        self.assertFalse(summary["transaction_made"])
        self.assertEqual(summary["status"], "SKIPPED")
        self.assertEqual(summary["why"], "Not a market day.")

    def test_execution_attempt_blocks_without_auto_execute(self):
        result = scheduler._execution_attempt(
            {}, auto_execute=False, allow_live_trading=False
        )

        self.assertEqual(result["status"], "SKIPPED")
        self.assertIn("AUTO_EXECUTE", result["reason"])

    def test_execution_attempt_blocks_when_cro_rejects(self):
        result = scheduler._execution_attempt(
            {"cro_output": json.dumps({"status": "REJECTED", "reason": "Too risky"})},
            auto_execute=True,
            allow_live_trading=False,
        )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("CRO did not approve", result["reason"])

    def test_execution_attempt_submits_approved_ticket_through_broker(self):
        class FakeOrder:
            def to_dict(self):
                return {"status": "SUBMITTED", "order_id": "order-123", "reason": "ok"}

        class FakeBroker:
            def execute(self, draft, execution, *, human_approved, allow_live_trading):
                self.calls = (draft, execution, human_approved, allow_live_trading)
                return FakeOrder()

        import broker

        with mock.patch.object(broker, "WheelBroker", return_value=FakeBroker()):
            result = scheduler._execution_attempt(
                {
                    "cro_output": json.dumps({"status": "APPROVED"}),
                    "draft_ticket": json.dumps({"action": "SELL_CSP"}),
                    "execution_output": json.dumps({"symbol": "AAPL260116P00150000"}),
                },
                auto_execute=True,
                allow_live_trading=True,
            )

        self.assertEqual(result["status"], "SUBMITTED")
        self.assertEqual(result["order_id"], "order-123")

    def test_run_wheel_sends_email_when_market_is_closed(self):
        sent = []

        with mock.patch.object(scheduler, "is_market_day", return_value=False):
            with mock.patch.object(scheduler, "fetch_account_summary", return_value={"error": "none"}):
                with mock.patch.object(
                    scheduler,
                    "send_run_report",
                    side_effect=lambda result, **kwargs: sent.append((result, kwargs)) or True,
                ):
                    scheduler.run_wheel(ticker="AAPL", run_label="manual")

        self.assertEqual(len(sent), 1)
        result, kwargs = sent[0]
        self.assertIn("TRANSACTION_SUMMARY", result)
        self.assertFalse(kwargs["transaction_summary"]["transaction_made"])
        self.assertIn("Not a market day", kwargs["transaction_summary"]["why"])

    def test_run_wheel_dry_run_sends_no_transaction_email(self):
        sent = []

        with mock.patch.object(scheduler, "is_market_day", return_value=True):
            with mock.patch.object(
                scheduler,
                "fetch_portfolio",
                return_value=json.dumps({"ticker": "AAPL", "cash": 5000, "shares": 100, "spot": 175, "cost_basis": 170}),
            ):
                with mock.patch.object(scheduler, "fetch_macro", return_value="VIX clear"):
                    with mock.patch.object(scheduler, "fetch_fundamentals", return_value="[]"):
                        with mock.patch.object(scheduler, "fetch_options_chain", return_value="[chain]"):
                            with mock.patch.object(scheduler, "fetch_account_summary", return_value={}):
                                with mock.patch.object(
                                    scheduler,
                                    "send_run_report",
                                    side_effect=lambda result, **kwargs: sent.append((result, kwargs)) or True,
                                ):
                                    scheduler.run_wheel(dry_run=True, run_label="manual")

        self.assertEqual(len(sent), 1)
        self.assertIn("DRY_RUN", sent[0][0])
        self.assertIn("Dry run requested", sent[0][1]["transaction_summary"]["why"])

    def test_option_only_account_fetches_each_short_put_chain(self):
        portfolio = json.dumps(
            {
                "ticker": "AAPL",
                "cash": 50000,
                "nlv": 100000,
                "shares": 0,
                "spot": 0,
                "cost_basis": 0,
                "short_puts": [
                    {"underlying": "MSFT", "qty": 1},
                    {"underlying": "AAPL", "qty": 1},
                ],
            }
        )
        with mock.patch.object(scheduler, "is_market_day", return_value=True):
            with mock.patch.object(scheduler, "fetch_portfolio", return_value=portfolio):
                with mock.patch.object(scheduler, "fetch_macro", return_value="VIX clear"):
                    with mock.patch.object(scheduler, "fetch_fundamentals", return_value="[]"):
                        with mock.patch.object(
                            scheduler,
                            "fetch_options_chain",
                            side_effect=lambda ticker: f"[{ticker} chain]",
                        ) as fetch_chain:
                            with mock.patch.object(scheduler, "fetch_account_summary", return_value={}):
                                with mock.patch.object(scheduler, "send_run_report", return_value=True):
                                    scheduler.run_wheel(dry_run=True, run_label="manual")

        self.assertEqual(
            [call.args[0] for call in fetch_chain.call_args_list],
            ["AAPL", "MSFT"],
        )

    def test_run_wheel_success_email_explains_submitted_transaction(self):
        sent = []
        fake_graph = types.ModuleType("agents.graph")
        fake_graph_state = {
            "draft_ticket": json.dumps({"action": "SELL_COVERED_CALL", "ticker": "AAPL"}),
            "cro_output": json.dumps({"status": "APPROVED", "reason": "Risk reducing"}),
            "execution_output": json.dumps({"symbol": "AAPL260116C00170000"}),
        }
        fake_graph.run_trading_flow_state = lambda *args, **kwargs: fake_graph_state
        fake_graph.format_trading_flow_state = lambda state: "GRAPH:\nok"

        with mock.patch.dict(sys.modules, {"agents.graph": fake_graph}):
            with mock.patch.object(scheduler, "is_market_day", return_value=True):
                with mock.patch.object(
                    scheduler,
                    "fetch_portfolio",
                    return_value=json.dumps({"ticker": "AAPL", "cash": 5000, "shares": 100, "spot": 175, "cost_basis": 170}),
                ):
                    with mock.patch.object(scheduler, "fetch_macro", return_value="VIX clear"):
                        with mock.patch.object(scheduler, "fetch_fundamentals", return_value="[]"):
                            with mock.patch.object(scheduler, "fetch_options_chain", return_value="[chain]"):
                                with mock.patch.object(scheduler, "fetch_account_summary", return_value={}):
                                    with mock.patch.object(
                                        scheduler,
                                        "_execution_attempt",
                                        return_value={
                                            "status": "SUBMITTED",
                                            "order_id": "order-123",
                                            "reason": "Option limit order submitted.",
                                        },
                                    ):
                                        with mock.patch.object(
                                            scheduler,
                                            "send_run_report",
                                            side_effect=lambda result, **kwargs: sent.append((result, kwargs)) or True,
                                        ):
                                            scheduler.run_wheel(auto_execute=True, run_label="manual")

        self.assertEqual(len(sent), 1)
        summary = sent[0][1]["transaction_summary"]
        self.assertTrue(summary["transaction_made"])
        self.assertEqual(summary["order_id"], "order-123")
        self.assertIn("Risk reducing", summary["why"])


if __name__ == "__main__":
    unittest.main()
