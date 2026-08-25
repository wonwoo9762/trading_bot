from __future__ import annotations

import json
import sys
import types
import unittest
from unittest import mock

from support import install_alpaca_stubs

install_alpaca_stubs(
    option_quotes={
        "AAPL260116C00170000": types.SimpleNamespace(bid_price=1.1, ask_price=1.2)
    }
)

import config
import data_feeds


class FakeClient:
    def __init__(self, positions=None, account=None, contracts=None):
        self.positions = positions or []
        self.account = account or types.SimpleNamespace(
            cash="5000",
            buying_power="10000",
            portfolio_value="25000",
            equity="25000",
        )
        self.contracts = contracts or []

    def get_account(self):
        return self.account

    def get_all_positions(self):
        return self.positions

    def get_option_contracts(self, request):
        self.contract_request = request
        return types.SimpleNamespace(option_contracts=self.contracts)


class DataFeedTests(unittest.TestCase):
    def test_candidate_universe_and_fundamentals_default_to_seed_data(self):
        universe = json.loads(data_feeds.fetch_candidate_universe())
        fundamentals = json.loads(data_feeds.fetch_fundamentals())

        self.assertGreaterEqual(len(universe), 3)
        self.assertEqual(universe[0]["source"], "STATIC_SEED_NOT_LIVE")
        self.assertEqual([row["ticker"] for row in fundamentals[:2]], ["AAPL", "MSFT"])

    def test_candidate_universe_respects_requested_tickers(self):
        universe = json.loads(data_feeds.fetch_candidate_universe(["MSFT", "ZZZZ"]))

        self.assertEqual([row["ticker"] for row in universe], ["MSFT", "ZZZZ"])
        self.assertEqual(universe[1]["source"], "USER_CONFIGURED_NO_STATIC_METRICS")

    def test_fetch_portfolio_selects_largest_position(self):
        client = FakeClient(
            positions=[
                types.SimpleNamespace(
                    symbol="AAPL",
                    market_value="12000",
                    current_price="180",
                    avg_entry_price="170",
                    qty="100",
                ),
                types.SimpleNamespace(
                    symbol="MSFT",
                    market_value="25000",
                    current_price="500",
                    avg_entry_price="450",
                    qty="50",
                ),
            ]
        )

        with mock.patch.object(data_feeds, "_get_trading_client", return_value=client):
            payload = json.loads(data_feeds.fetch_portfolio())

        self.assertEqual(payload["ticker"], "MSFT")
        self.assertEqual(payload["cash"], 5000.0)
        self.assertEqual(payload["shares"], 50)

    def test_fetch_portfolio_returns_cash_state_when_ticker_missing(self):
        client = FakeClient(positions=[])

        with mock.patch.object(data_feeds, "_get_trading_client", return_value=client):
            payload = json.loads(data_feeds.fetch_portfolio("AAPL"))

        self.assertEqual(payload["ticker"], "AAPL")
        self.assertEqual(payload["shares"], 0)
        self.assertEqual(payload["spot"], 0)

    def test_fetch_portfolio_tracks_short_put_without_treating_it_as_stock(self):
        client = FakeClient(
            positions=[
                types.SimpleNamespace(
                    symbol="AAPL260826P00307500",
                    market_value="-75",
                    current_price="0.75",
                    avg_entry_price="0.76",
                    qty="-1",
                )
            ],
            account=types.SimpleNamespace(
                cash="1000075",
                buying_power="3000000",
                portfolio_value="1000000",
                equity="1000000",
            ),
        )

        with mock.patch.object(data_feeds, "_get_trading_client", return_value=client):
            payload = json.loads(data_feeds.fetch_portfolio())

        self.assertEqual(payload["ticker"], "AAPL")
        self.assertEqual(payload["shares"], 0)
        self.assertEqual(payload["nlv"], 1000000)
        self.assertEqual(payload["short_put_collateral"], 30750)
        self.assertEqual(payload["short_puts"][0]["underlying"], "AAPL")
        self.assertEqual(payload["short_puts"][0]["entry_credit"], 0.76)
        self.assertEqual(payload["short_puts"][0]["current_price"], 0.75)
        self.assertIn("dte", payload["short_puts"][0])

    def test_fetch_portfolio_tracks_short_call_for_duplicate_protection(self):
        client = FakeClient(
            positions=[
                types.SimpleNamespace(
                    symbol="AAPL",
                    market_value="10000",
                    current_price="100",
                    avg_entry_price="95",
                    qty="100",
                ),
                types.SimpleNamespace(
                    symbol="AAPL260930C00105000",
                    market_value="-100",
                    current_price="1.00",
                    avg_entry_price="1.50",
                    qty="-1",
                ),
            ]
        )

        with mock.patch.object(data_feeds, "_get_trading_client", return_value=client):
            payload = json.loads(data_feeds.fetch_portfolio())

        self.assertEqual(payload["ticker"], "AAPL")
        self.assertEqual(payload["short_calls"][0]["underlying"], "AAPL")
        self.assertEqual(payload["short_calls"][0]["qty"], 1)

    def test_fetch_account_summary_success_and_failure(self):
        position = types.SimpleNamespace(
            symbol="AAPL",
            qty="100",
            avg_entry_price="170",
            current_price="180",
            market_value="18000",
            unrealized_pl="1000",
        )
        client = FakeClient(positions=[position])

        with mock.patch.object(data_feeds, "_get_trading_client", return_value=client):
            summary = data_feeds.fetch_account_summary()

        self.assertEqual(summary["cash"], 5000.0)
        self.assertEqual(summary["positions"][0]["symbol"], "AAPL")
        self.assertEqual(summary["positions"][0]["unrealized_pct"], 5.88)

        with self.assertLogs(data_feeds.logger, level="ERROR"):
            with mock.patch.object(
                data_feeds, "_get_trading_client", side_effect=RuntimeError("boom")
            ):
                failure = data_feeds.fetch_account_summary()

        self.assertIn("error", failure)

    def test_fetch_options_chain_includes_contract_symbol_and_spreads(self):
        install_alpaca_stubs(
            option_quotes={
                "AAPL260116C00170000": types.SimpleNamespace(
                    bid_price=1.1, ask_price=1.2
                )
            }
        )
        contract = types.SimpleNamespace(
            type=types.SimpleNamespace(value="call"),
            strike_price="170",
            expiration_date="2026-01-16",
            symbol="AAPL260116C00170000",
            close_price="1.15",
            open_interest="123",
        )
        client = FakeClient(contracts=[contract])

        fake_config = types.ModuleType("config")
        fake_config.get_alpaca_credentials = lambda: ("key", "secret")
        fake_config.get_alpaca_trading_client = lambda: client
        with mock.patch.dict(sys.modules, {"config": fake_config}):
            chain = data_feeds.fetch_options_chain("AAPL")

        self.assertIn("Symbol: AAPL260116C00170000", chain)
        self.assertIn("Underlying: AAPL", chain)
        self.assertIn("Bid: 1.1", chain)
        self.assertIn("Ask: 1.2", chain)
        self.assertEqual(client.contract_request.limit, 10000)

    def test_fetch_options_chain_includes_snapshot_delta_and_pop(self):
        install_alpaca_stubs(
            option_snapshots={
                "AAPL260116P00150000": types.SimpleNamespace(
                    latest_quote=types.SimpleNamespace(bid_price=1.0, ask_price=1.1),
                    greeks=types.SimpleNamespace(delta=-0.25),
                    implied_volatility=0.32,
                )
            }
        )
        contract = types.SimpleNamespace(
            type=types.SimpleNamespace(value="put"),
            strike_price="150",
            expiration_date="2026-01-16",
            symbol="AAPL260116P00150000",
            close_price="1.05",
            open_interest="456",
        )
        client = FakeClient(contracts=[contract])

        fake_config = types.ModuleType("config")
        fake_config.get_alpaca_credentials = lambda: ("key", "secret")
        fake_config.get_alpaca_trading_client = lambda: client
        with mock.patch.dict(sys.modules, {"config": fake_config}):
            chain = data_feeds.fetch_options_chain(
                "AAPL", contract_type="put", min_dte=10, max_dte=18
            )

        self.assertEqual(client.contract_request.type.value, "put")
        self.assertEqual(
            client.contract_request.expiration_date_gte,
            (data_feeds.date.today() + data_feeds.timedelta(days=10)).isoformat(),
        )
        self.assertEqual(
            client.contract_request.expiration_date_lte,
            (data_feeds.date.today() + data_feeds.timedelta(days=18)).isoformat(),
        )
        self.assertIn("Delta: -0.2500", chain)
        self.assertIn("POP: 75.0%", chain)
        self.assertIn("IV: 0.3200", chain)

    def test_liquidation_snapshot(self):
        snapshot = data_feeds.compute_liquidation_snapshot(
            json.dumps(
                {
                    "ticker": "AAPL",
                    "shares": 100,
                    "spot": 160,
                    "cost_basis": 170,
                    "cash": 5000,
                }
            )
        )

        self.assertIn("loss of $1,000.00", snapshot)
        self.assertIn("$21,000.00 cash", snapshot)


if __name__ == "__main__":
    unittest.main()
