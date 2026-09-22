"""Offline integration checks using real LangGraph and Alpaca request models.

Run in a fresh process: the legacy unit suite installs library stubs globally.
Only agent responses, market data, and the broker transport are replaced.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def deny_network(*args, **kwargs):
    raise RuntimeError("Network disabled by execution safety integration tests")


# Block connections even if a future refactor accidentally invokes a service.
socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network

from alpaca.trading.enums import PositionIntent
from alpaca.trading.requests import LimitOrderRequest
from alpaca.common.exceptions import APIError
from agents import graph
from broker import WheelBroker
from models import (
    AssessorOutput, CandidateSelectorOutput, CROOutput, MacroSentinelOutput,
    OrchestratorOutput, QuantOutput, ScreenerOutput,
)


class FakeTransport:
    def __init__(self):
        self.orders = []
        self.broker_orders = {}

    def get_account(self):
        return SimpleNamespace(id="offline-account")

    def get_orders(self, filter):
        return []

    def get_order_by_client_id(self, client_id):
        if client_id in self.broker_orders:
            return self.broker_orders[client_id]
        raise APIError('{"code":40410000,"message":"Not found"}',
                       SimpleNamespace(response=SimpleNamespace(status_code=404)))

    def submit_order(self, order_data):
        self.orders.append(order_data)
        order = SimpleNamespace(id="offline-order", client_order_id=order_data.client_order_id, status="new")
        self.broker_orders[order_data.client_order_id] = order
        return order


class ExecutionSafetyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.expiry = date.today() + timedelta(days=14)
        self.put = f"AAPL{self.expiry:%y%m%d}P00100000"
        self.call = f"AAPL{self.expiry:%y%m%d}C00105000"
        self.universe = json.dumps([{
            "ticker": "AAPL", "fcf": 100, "debt_to_equity": 0.5,
            "mkt_cap": 1_000_000_000_000, "source": "OFFLINE_FIXTURE",
        }])
        self.responses = {
            MacroSentinelOutput: MacroSentinelOutput(status="CLEAR", reason="Fixture"),
            OrchestratorOutput: OrchestratorOutput(route_to="CASH", action="Fixture"),
            CandidateSelectorOutput: CandidateSelectorOutput(selected_tickers=["AAPL"]),
            ScreenerOutput: ScreenerOutput(approved_tickers=["AAPL"]),
            CROOutput: CROOutput(status="APPROVED", reason="Fixture"),
        }
        self.called = []
        self.broker = object.__new__(WheelBroker)
        self.broker._paper = True
        self.broker._client = FakeTransport()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.broker._journal_path = Path(directory.name) / "orders.sqlite3"

    def chain(self, *, call=False, bid=1.0, ask=1.1):
        return (
            f"[{'Call 105' if call else 'Put 100'} Exp {self.expiry.isoformat()} "
            f"Underlying: AAPL Symbol: {self.call if call else self.put} "
            f"Bid: {bid} Ask: {ask} Delta: {0.25 if call else -0.25} "
            "POP: 75.0% IV: 0.30 OI: 500]"
        )

    def invoke(self, schema, *_args):
        self.called.append(schema)
        self.assertIn(schema, self.responses, f"Unexpected agent call: {schema}")
        return None, self.responses[schema]

    def run_flow(self, portfolio=None, **inputs):
        portfolio = portfolio or {"cash": 1_000_000, "shares": 0, "nlv": 1_000_000}
        defaults = dict(
            macro_input="Offline fixture: VIX 15, no imminent events",
            candidate_universe_input=self.universe,
            fundamentals_input=self.universe,
        )
        defaults.update(inputs)
        with patch.object(graph, "_invoke_structured", side_effect=self.invoke), patch(
            "data_feeds.fetch_options_chain", return_value=self.chain()
        ):
            return graph.run_trading_flow_state(json.dumps(portfolio), **defaults)

    def submit(self, state):
        result = self.broker.execute(
            state["draft_ticket"], state["execution_output"], human_approved=True
        )
        self.assertEqual(result.status, "SUBMITTED", result.reason)
        order = self.broker._client.orders[-1]
        self.assertIsInstance(order, LimitOrderRequest)
        return order

    def test_cash_flow_preserves_approved_order_and_rejects_substitution(self):
        state = self.run_flow()
        ticket = json.loads(state["draft_ticket"])
        self.assertEqual(ticket["action"], "SELL_CSP")
        order = self.submit(state)
        self.assertEqual((order.symbol, order.qty, order.limit_price), (self.put, 15, 1.05))
        self.assertEqual(order.position_intent, PositionIntent.SELL_TO_OPEN)
        params = json.loads(state["execution_output"])
        for change in (
            {"symbol": self.call}, {"qty": 50}, {"limit_price": 0.01},
            {"legs": [{"symbol": self.call, "ratio_qty": 50, "side": "sell"}]},
        ):
            with self.subTest(change=change):
                result = self.broker.execute(
                    state["draft_ticket"], json.dumps(params | change), human_approved=True
                )
                self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(len(self.broker._client.orders), 1)

    def test_empty_or_failed_selection_stops_full_cash_flow(self):
        for response in (CandidateSelectorOutput(selected_tickers=[]), None):
            with self.subTest(response=response):
                self.called.clear()
                self.responses[CandidateSelectorOutput] = response
                state = self.run_flow()
                self.assertEqual(json.loads(state["draft_ticket"])["action"], "NO_TRADE")
                self.assertNotIn(ScreenerOutput, self.called)
                self.assertNotIn(CROOutput, self.called)
                self.assertNotIn("execution_output", state)

    def test_distressed_repairs_never_reach_execution(self):
        self.responses[AssessorOutput] = AssessorOutput(decision="APPROVE_ROLL", reason="Fixture")
        for action in ("ROLL", "SPREAD"):
            with self.subTest(action=action):
                self.responses[QuantOutput] = QuantOutput(action=action, est_credit=2)
                state = self.run_flow(
                    {"ticker": "AAPL", "shares": 100, "spot": 80, "cost_basis": 100, "cash": 990_000},
                    options_chain_input=self.chain(), liquidation_input="Offline fixture",
                )
                ticket = json.loads(state["draft_ticket"])
                self.assertEqual(ticket["action"], "NO_TRADE")
                self.assertIn("AUTOMATED_REPAIR_DISABLED", ticket["reason"])
                self.assertNotIn("execution_output", state)
                result = self.broker.execute(
                    self.responses[QuantOutput].model_dump_json(), "{}", human_approved=True
                )
                self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(self.broker._client.orders, [])

    def test_profitable_put_can_close_during_macro_halt(self):
        self.responses[MacroSentinelOutput] = MacroSentinelOutput(status="HALT", reason="Fixture")
        state = self.run_flow(
            {"cash": 990_000, "shares": 0, "short_put_collateral": 10_000, "short_puts": [{
                "symbol": self.put, "underlying": "AAPL", "qty": 1,
                "collateral": 10_000, "entry_credit": 2, "dte": 14,
            }]}, options_chain_input=self.chain(bid=0.8, ask=0.9),
        )
        self.assertEqual(json.loads(state["draft_ticket"])["action"], "CLOSE_SHORT_PUT")
        order = self.submit(state)
        self.assertEqual((order.qty, order.limit_price), (1, 0.85))
        self.assertEqual(order.position_intent, PositionIntent.BUY_TO_CLOSE)

    def test_covered_call_flow_retains_opening_intent(self):
        state = self.run_flow(
            {"ticker": "AAPL", "cash": 990_000, "shares": 100, "spot": 100, "cost_basis": 100},
            options_chain_input=self.chain(call=True),
        )
        self.assertEqual(json.loads(state["draft_ticket"])["action"], "SELL_COVERED_CALL")
        order = self.submit(state)
        self.assertEqual((order.symbol, order.qty), (self.call, 1))
        self.assertEqual(order.position_intent, PositionIntent.SELL_TO_OPEN)

    def test_retry_after_acceptance_uses_existing_order(self):
        state = self.run_flow()
        original_submit = self.broker._client.submit_order

        def accepted_then_timeout(order_data):
            original_submit(order_data)
            raise TimeoutError("Response lost")

        with patch.object(self.broker._client, "submit_order", side_effect=accepted_then_timeout):
            result = self.broker.execute(state["draft_ticket"], state["execution_output"], human_approved=True)
        self.assertEqual(result.status, "RECONCILED")
        self.broker._client.broker_orders[result.client_order_id].status = "filled"
        retried = self.broker.execute(state["draft_ticket"], state["execution_output"], human_approved=True)
        self.assertEqual(retried.status, "BLOCKED")
        self.assertEqual(len(self.broker._client.orders), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
