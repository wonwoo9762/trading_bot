from __future__ import annotations

import json
import types
import unittest

from support import install_alpaca_stubs

install_alpaca_stubs()

import broker


class FakeTradingClient:
    def __init__(self):
        self.orders = []
        self.closed = []

    def submit_order(self, order_data):
        self.orders.append(order_data)
        return types.SimpleNamespace(id="order-123")

    def close_position(self, symbol):
        self.closed.append(symbol)


def make_broker(*, paper=True, client=None):
    b = object.__new__(broker.WheelBroker)
    b._paper = paper
    b._client = client or FakeTradingClient()
    return b


class BrokerTests(unittest.TestCase):
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

    def test_simple_option_limit_order_is_submitted(self):
        client = FakeTradingClient()
        b = make_broker(client=client)

        result = b.execute(
            json.dumps(
                {
                    "action": "SELL_COVERED_CALL",
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
        self.assertEqual(order.time_in_force.value, "day")
        self.assertEqual(order.limit_price, 1.2)

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

    def test_multi_leg_credit_order_is_submitted_with_negative_limit(self):
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

        self.assertEqual(result.status, "SUBMITTED")
        order = client.orders[0]
        self.assertEqual(order.order_class.value, "mleg")
        self.assertEqual(order.limit_price, -1.4)
        self.assertEqual(len(order.legs), 2)

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
