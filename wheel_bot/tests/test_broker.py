from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from threading import Event
import types
import unittest
from unittest import mock

from support import install_alpaca_stubs

install_alpaca_stubs()

import broker


class FakeTradingClient:
    def __init__(self):
        self.orders = []
        self.closed = []
        self.broker_orders = {}
        self.open_orders = []

    def get_account(self):
        return types.SimpleNamespace(id="offline-account")

    def get_orders(self, filter):
        return self.open_orders

    def get_order_by_client_id(self, client_id):
        if client_id in self.broker_orders:
            return self.broker_orders[client_id]
        from alpaca.common.exceptions import APIError
        raise APIError('{"code":40410000,"message":"Not found"}',
                       types.SimpleNamespace(response=types.SimpleNamespace(status_code=404)))

    def submit_order(self, order_data):
        self.orders.append(order_data)
        order = types.SimpleNamespace(id="order-123", client_order_id=order_data.client_order_id, status="new")
        self.broker_orders[order_data.client_order_id] = order
        return order

    def close_position(self, symbol):
        self.closed.append(symbol)


def make_broker(*, paper=True, client=None):
    b = object.__new__(broker.WheelBroker)
    b._paper = paper
    b._client = client or FakeTradingClient()
    return b


class BrokerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        override = mock.patch.object(broker, "DEFAULT_JOURNAL_PATH", Path(directory.name) / "orders.sqlite3")
        override.start()
        self.addCleanup(override.stop)

    def _valid_order(self, client=None, *, symbol="AAPL270101P00300000"):
        b = make_broker(client=client)
        ticket = json.dumps({"action": "SELL_CSP", "symbol": symbol, "qty": 1, "bid": 2, "ask": 2.2})
        return b, ticket, '{"limit_price":2.1}'

    def test_restart_cannot_submit_same_intent_again_even_after_fill(self):
        b, ticket, params = self._valid_order()
        first = b.execute(ticket, params, human_approved=True)
        b._client.broker_orders[first.client_order_id].status = "filled"
        restarted = make_broker(client=b._client)
        second = restarted.execute(ticket, params, human_approved=True)
        self.assertEqual(second.status, "BLOCKED")
        self.assertIn("DUPLICATE_INTENT", second.reason)
        self.assertEqual(len(b._client.orders), 1)

    def test_changed_quantity_and_price_do_not_create_another_daily_intent(self):
        b, ticket, params = self._valid_order()
        first = b.execute(ticket, params, human_approved=True)
        b._client.broker_orders[first.client_order_id].status = "filled"
        changed = json.loads(ticket) | {"qty": 2}
        second = b.execute(json.dumps(changed), '{"limit_price":2.2}', human_approved=True)
        self.assertEqual(second.status, "BLOCKED")
        self.assertIn("DUPLICATE_INTENT", second.reason)
        self.assertEqual(len(b._client.orders), 1)

    def test_simultaneous_workers_cannot_both_submit(self):
        b, ticket, params = self._valid_order()
        reserved = Event()
        release = Event()
        original = b._client.submit_order

        def slow_submit(order_data):
            reserved.set()
            if not release.wait(timeout=5):
                raise TimeoutError("Test timed out")
            return original(order_data)

        with ThreadPoolExecutor(max_workers=1) as pool, mock.patch.object(b._client, "submit_order", side_effect=slow_submit):
            first = pool.submit(b.execute, ticket, params, human_approved=True)
            try:
                self.assertTrue(reserved.wait(timeout=5))
                other = make_broker(client=b._client)
                second = other.execute(ticket, params, human_approved=True)
                self.assertEqual(second.status, "BLOCKED")
                self.assertIn("UNRESOLVED_SUBMISSION", second.reason)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5).status, "SUBMITTED")
        self.assertEqual(len(b._client.orders), 1)

    def test_uncertain_reservation_still_blocks_on_the_next_day(self):
        b, ticket, params = self._valid_order()
        with mock.patch.object(b._client, "submit_order", side_effect=TimeoutError("Unknown")):
            self.assertEqual(b.execute(ticket, params, human_approved=True).status, "UNKNOWN")
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        with mock.patch("execution_guard.datetime") as clock:
            clock.now.return_value = tomorrow
            result = b.execute(ticket, params, human_approved=True)
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("UNRESOLVED_SUBMISSION", result.reason)
        self.assertEqual(b._client.orders, [])

    def test_reconciled_terminal_order_allows_a_different_intent(self):
        b, ticket, params = self._valid_order()
        first = b.execute(ticket, params, human_approved=True)
        for status in ("partially_filled", "pending_cancel", "done_for_day", "unknown_status"):
            b._client.broker_orders[first.client_order_id].status = status
            _, different_ticket, _ = self._valid_order(b._client, symbol="MSFT270101P00300000")
            self.assertEqual(b.execute(different_ticket, params, human_approved=True).status, "BLOCKED")
        b._client.broker_orders[first.client_order_id].status = "filled"
        self.assertEqual(b.execute(different_ticket, params, human_approved=True).status, "SUBMITTED")
        self.assertEqual(len(b._client.orders), 2)

    def test_timeout_after_acceptance_is_reconciled_without_resubmission(self):
        client = FakeTradingClient()
        b, ticket, params = self._valid_order(client)
        original = client.submit_order

        def accepted_then_timeout(order_data):
            original(order_data)
            raise TimeoutError("Response lost")

        client.submit_order = accepted_then_timeout
        result = b.execute(ticket, params, human_approved=True)
        self.assertEqual(result.status, "RECONCILED")
        self.assertEqual(result.order_id, "order-123")
        self.assertEqual(len(client.orders), 1)
        self.assertEqual(b.execute(ticket, params, human_approved=True).status, "BLOCKED")
        self.assertEqual(len(client.orders), 1)

    def test_uncertain_submission_blocks_new_contract_after_restart(self):
        client = FakeTradingClient()
        b, ticket, params = self._valid_order(client)
        with mock.patch.object(client, "submit_order", side_effect=TimeoutError("Unknown outcome")) as submit:
            result = b.execute(ticket, params, human_approved=True)
            self.assertEqual(result.status, "UNKNOWN")
            restarted, different_ticket, _ = self._valid_order(client, symbol="MSFT270101P00300000")
            second = restarted.execute(different_ticket, params, human_approved=True)
            self.assertEqual(second.status, "BLOCKED")
            self.assertIn("UNRESOLVED_SUBMISSION", second.reason)
            self.assertEqual(submit.call_count, 1)

    def test_pending_manual_order_blocks_new_submission(self):
        client = FakeTradingClient()
        client.open_orders = [types.SimpleNamespace(id="manual-order", status="partially_filled")]
        b, ticket, params = self._valid_order(client)
        result = b.execute(ticket, params, human_approved=True)
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("PENDING_ORDER", result.reason)
        self.assertEqual(client.orders, [])

    def test_unavailable_or_malformed_broker_checks_fail_closed(self):
        for method, effect in (
            ("get_account", RuntimeError("Unavailable")),
            ("get_orders", RuntimeError("Unavailable")),
            ("get_order_by_client_id", RuntimeError("Unavailable")),
        ):
            with self.subTest(method=method):
                b, ticket, params = self._valid_order()
                with mock.patch.object(b._client, method, side_effect=effect):
                    result = b.execute(ticket, params, human_approved=True)
                self.assertEqual(result.status, "BLOCKED")
                self.assertEqual(b._client.orders, [])
        for method, value in (("get_account", {}), ("get_orders", {}), ("get_order_by_client_id", None)):
            with self.subTest(method=method, value=value):
                b, ticket, params = self._valid_order()
                with mock.patch.object(b._client, method, return_value=value):
                    result = b.execute(ticket, params, human_approved=True)
                self.assertEqual(result.status, "BLOCKED")
                self.assertEqual(b._client.orders, [])

    def test_journal_failure_blocks_before_submission(self):
        b, ticket, params = self._valid_order()
        with mock.patch("execution_guard.sqlite3.connect", side_effect=OSError("Disk unavailable")):
            result = b.execute(ticket, params, human_approved=True)
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(b._client.orders, [])

    def test_broker_lookup_detects_duplicate_when_journal_is_missing(self):
        b, ticket, params = self._valid_order()
        first = b.execute(ticket, params, human_approved=True)
        b._journal_path = broker.DEFAULT_JOURNAL_PATH.with_name("another-journal.sqlite3")
        result = b.execute(ticket, params, human_approved=True)
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn(first.client_order_id, result.reason)
        self.assertEqual(len(b._client.orders), 1)

    def test_execute_requires_approval(self):
        b = make_broker()

        result = b.execute("{}", "{}", human_approved=False)

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("HUMAN_APPROVAL_REQUIRED", result.reason)

    def test_live_trading_requires_second_opt_in(self):
        b = make_broker(paper=False)

        result = b.execute(
            json.dumps({"action": "SELL_COVERED_CALL"}),
            json.dumps({"symbol": "AAPL260116C00170000", "side": "sell", "limit_price": 1.2}),
            human_approved=True,
            allow_live_trading=False,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("LIVE_TRADING_NOT_ALLOWED", result.reason)

    def test_live_csp_requires_live_candidate_data(self):
        b = make_broker(paper=False)

        result = b.execute(
            json.dumps(
                {
                    "action": "SELL_CSP",
                    "candidate_data_live": False,
                    "candidate_data_source": "STATIC_SEED_NOT_LIVE",
                }
            ),
            "{}",
            human_approved=True,
            allow_live_trading=True,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("LIVE_CANDIDATE_DATA_REQUIRED", result.reason)

    def test_simple_option_limit_order_is_submitted(self):
        client = FakeTradingClient()
        b = make_broker(client=client)

        result = b.execute(
            json.dumps(
                {
                    "action": "SELL_COVERED_CALL",
                    "symbol": "AAPL260116C00170000",
                    "qty": 1,
                    "bid": 1.1,
                    "ask": 1.3,
                    "portfolio_state": json.dumps({"ticker": "AAPL", "shares": 100}),
                }
            ),
            json.dumps({"symbol": "AAPL260116C00170000", "side": "sell", "limit_price": 1.2}),
            human_approved=True,
        )

        self.assertEqual(result.status, "SUBMITTED")
        self.assertEqual(result.order_id, "order-123")
        self.assertEqual(len(client.orders), 1)
        order = client.orders[0]
        self.assertEqual(order.symbol, "AAPL260116C00170000")
        self.assertEqual(order.qty, 1)
        self.assertEqual(order.side.value, "sell")
        self.assertEqual(order.position_intent.value, "sell_to_open")
        self.assertEqual(order.time_in_force.value, "day")
        self.assertEqual(order.limit_price, 1.2)

    def test_close_short_put_is_exact_and_buy_to_close(self):
        client = FakeTradingClient()
        b = make_broker(client=client)
        ticket = {
            "action": "CLOSE_SHORT_PUT",
            "symbol": "AAPL270101P00300000",
            "qty": 2,
            "bid": 0.8,
            "ask": 0.9,
        }

        result = b.execute(
            json.dumps(ticket),
            json.dumps(
                {
                    "symbol": ticket["symbol"],
                    "side": "buy",
                    "qty": 2,
                    "limit_price": 0.85,
                }
            ),
            human_approved=True,
        )

        self.assertEqual(result.status, "SUBMITTED")
        self.assertEqual(client.orders[0].side.value, "buy")
        self.assertEqual(client.orders[0].position_intent.value, "buy_to_close")

    def test_close_short_put_cannot_be_flipped_to_sell(self):
        b = make_broker()
        ticket = {
            "action": "CLOSE_SHORT_PUT",
            "symbol": "AAPL270101P00300000",
            "qty": 1,
            "bid": 0.8,
            "ask": 0.9,
        }

        result = b.execute(
            json.dumps(ticket),
            json.dumps(
                {
                    "symbol": ticket["symbol"],
                    "side": "sell",
                    "qty": 1,
                    "limit_price": 0.85,
                }
            ),
            human_approved=True,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("side=buy", result.reason)

    def test_options_order_blocks_when_broker_returned_error(self):
        b = make_broker()

        result = b.execute(
            json.dumps({"action": "SELL_CSP"}),
            json.dumps({"error": "MISSING_SPREAD", "note": "No bid/ask"}),
            human_approved=True,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("MISSING_SPREAD", result.reason)

    def test_options_order_blocks_missing_contract_symbol(self):
        b = make_broker()

        result = b.execute(
            json.dumps({"action": "SELL_CSP"}),
            json.dumps({"side": "sell", "limit_price": 1.0}),
            human_approved=True,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("symbol", result.reason)

    def test_csp_order_uses_exact_cro_approved_symbol_qty_and_spread(self):
        client = FakeTradingClient()
        b = make_broker(client=client)
        ticket = {
            "action": "SELL_CSP",
            "symbol": "AAPL270101P00300000",
            "qty": 4,
            "bid": 2.0,
            "ask": 2.2,
        }

        result = b.execute(
            json.dumps(ticket),
            json.dumps(
                {
                    "symbol": ticket["symbol"],
                    "side": "sell",
                    "qty": 4,
                    "limit_price": 2.1,
                }
            ),
            human_approved=True,
        )

        self.assertEqual(result.status, "SUBMITTED")
        self.assertEqual(client.orders[0].qty, 4)

    def test_csp_order_blocks_llm_resize_or_contract_substitution(self):
        b = make_broker()
        ticket = {
            "action": "SELL_CSP",
            "symbol": "AAPL270101P00300000",
            "qty": 4,
            "bid": 2.0,
            "ask": 2.2,
        }

        result = b.execute(
            json.dumps(ticket),
            json.dumps(
                {
                    "symbol": "AAPL270101P00290000",
                    "side": "sell",
                    "qty": 1,
                    "limit_price": 2.1,
                }
            ),
            human_approved=True,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("changed the CRO-approved", result.reason)

    def test_csp_order_blocks_price_outside_approved_spread(self):
        b = make_broker()
        ticket = {
            "action": "SELL_CSP",
            "symbol": "AAPL270101P00300000",
            "qty": 2,
            "bid": 2.0,
            "ask": 2.2,
        }

        result = b.execute(
            json.dumps(ticket),
            json.dumps(
                {
                    "symbol": ticket["symbol"],
                    "side": "sell",
                    "qty": 2,
                    "limit_price": 1.9,
                }
            ),
            human_approved=True,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("outside the approved bid/ask", result.reason)

    def test_multi_leg_credit_repair_is_blocked_even_with_approval(self):
        client = FakeTradingClient()
        b = make_broker(client=client)

        result = b.execute(
            json.dumps({"action": "ROLL", "qty": 1}),
            json.dumps(
                {
                    "qty": 1,
                    "limit_price": 1.4,
                    "legs": [
                        {
                            "symbol": "AAPL260116C00170000",
                            "side": "buy",
                            "position_intent": "buy_to_close",
                        },
                        {
                            "symbol": "AAPL260220C00175000",
                            "side": "sell",
                            "position_intent": "sell_to_open",
                        },
                    ],
                }
            ),
            human_approved=True,
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("AUTOMATED_REPAIR_DISABLED", result.reason)
        self.assertEqual(client.orders, [])

    def test_single_leg_approval_cannot_be_replaced_by_multi_leg_order(self):
        for action in ("SELL_CSP", "SELL_COVERED_CALL", "CLOSE_SHORT_PUT"):
            with self.subTest(action=action):
                b = make_broker()
                option_type = "C" if action == "SELL_COVERED_CALL" else "P"
                ticket = {"action": action, "symbol": f"AAPL270101{option_type}00300000", "qty": 1, "bid": 2, "ask": 2.2}
                result = b.execute(json.dumps(ticket), json.dumps({
                    "qty": 50, "limit_price": -1.0,
                    "legs": [
                        {"symbol": "MSFT270101P00200000", "side": "sell", "position_intent": "sell_to_open"},
                        {"symbol": "MSFT270101P00190000", "side": "buy", "position_intent": "buy_to_open"},
                    ],
                }), human_approved=True)
                self.assertEqual(result.status, "BLOCKED")
                self.assertIn("SINGLE_LEG_REQUIRED", result.reason)
                self.assertEqual(b._client.orders, [])

    def test_invalid_or_substituted_execution_fields_cannot_reach_broker(self):
        ticket = {"action": "SELL_CSP", "symbol": "AAPL270101P00300000", "qty": 1, "bid": 2, "ask": 2.2}
        cases = [
            {"qty": 1.9}, {"qty": 0}, {"qty": -1}, {"qty": True},
            {"qty": float("nan")}, {"qty": float("inf")},
            {"qty": 1, "quantity": 2},
            {"symbol": ticket["symbol"], "contract_symbol": "MSFT270101P00300000"},
            {"side": "buy"}, {"side": "invalid"},
            {"position_intent": "buy_to_close"},
            {"limit_price": float("nan")}, {"limit_price": float("inf")},
            {"limit_price": True}, {"limit_price": -2}, {"limit_price": 2.001},
        ]
        for override in cases:
            with self.subTest(override=override):
                b = make_broker()
                params = {"symbol": ticket["symbol"], "qty": 1, "side": "sell", "limit_price": 2.1, **override}
                result = b.execute(json.dumps(ticket), json.dumps(params), human_approved=True)
                self.assertEqual(result.status, "BLOCKED")
                self.assertEqual(b._client.orders, [])

    def test_invalid_approved_ticket_is_blocked_without_guessing(self):
        ticket = {"action": "SELL_CSP", "symbol": "AAPL270101P00300000", "qty": 1, "bid": 2, "ask": 2.2}
        for override in ({"qty": None}, {"qty": 1.5}, {"bid": float("nan")}, {"ask": float("inf")}, {"bid": 3}, {"symbol": "AAPL270101C00300000"}):
            with self.subTest(override=override):
                b = make_broker()
                result = b.execute(json.dumps({**ticket, **override}), '{"limit_price":2.1}', human_approved=True)
                self.assertEqual(result.status, "BLOCKED")
                self.assertEqual(b._client.orders, [])

    def test_non_object_ticket_and_execution_params_are_blocked(self):
        b = make_broker()
        self.assertEqual(b.execute("[]", "{}", human_approved=True).status, "BLOCKED")
        ticket = json.dumps({"action": "SELL_CSP"})
        self.assertEqual(b.execute(ticket, "[]", human_approved=True).status, "BLOCKED")
        self.assertEqual(b._client.orders, [])

    def test_liquidate_closes_underlying_position(self):
        client = FakeTradingClient()
        b = make_broker(client=client)

        result = b.execute(
            json.dumps({"action": "LIQUIDATE", "ticker": "AAPL"}),
            "{}",
            human_approved=True,
        )

        self.assertEqual(result.status, "SUBMITTED")
        self.assertEqual(client.closed, ["AAPL"])


if __name__ == "__main__":
    unittest.main()
