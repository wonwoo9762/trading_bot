from __future__ import annotations

import json
import sys
import unittest
from unittest import mock

from support import install_langgraph_stubs

install_langgraph_stubs()
sys.modules.pop("agents.graph", None)

from agents import graph


class GraphTests(unittest.TestCase):
    def test_derive_route_from_portfolio(self):
        self.assertEqual(
            graph._derive_route_from_portfolio(
                json.dumps({"cash": 5000, "shares": 0, "spot": 0, "cost_basis": 0})
            ),
            "CASH",
        )
        self.assertEqual(
            graph._derive_route_from_portfolio(
                json.dumps({"cash": 1000, "shares": 100, "spot": 100, "cost_basis": 120})
            ),
            "ASSET_DISTRESSED",
        )
        self.assertEqual(
            graph._derive_route_from_portfolio(
                json.dumps({"cash": 1000, "shares": 100, "spot": 130, "cost_basis": 120})
            ),
            "ASSET_NOMINAL",
        )

    def test_data_gate_allows_cash_without_preselected_options_chain(self):
        cash_state = {
            "route_to": "CASH",
            "portfolio_state": "{}",
            "macro_input": "clear",
            "candidate_universe_input": json.dumps([{"ticker": "AAPL"}]),
            "fundamentals_input": json.dumps([{"ticker": "AAPL"}]),
            "options_chain_input": "",
        }

        result = graph.data_gate_node(cash_state)

        self.assertEqual(result["data_gate_status"], "ok")

    def test_candidate_selector_fallback_uses_static_quality_filters(self):
        universe = json.dumps(
            [
                {"ticker": "AAPL", "fcf": 100, "dte": 1.0, "mkt_cap": 1_000_000_000_000},
                {"ticker": "XYZ", "fcf": -1, "dte": 0.1, "mkt_cap": 100_000_000_000},
            ]
        )

        selected = graph._deterministic_candidate_fallback(universe)

        self.assertEqual(selected.selected_tickers, ["AAPL"])

    def test_cash_options_chain_fetches_after_screener_selects_ticker(self):
        state = {
            "screener_output": json.dumps({"approved_tickers": ["AAPL"]}),
        }

        with mock.patch(
            "data_feeds.fetch_options_chain",
            return_value="[Put 150 Symbol: AAPL260116P00150000 Bid: 1.1 Ask: 1.2]",
        ) as fetch_options_chain:
            out = graph.cash_options_chain_node(state)

        fetch_options_chain.assert_called_once_with("AAPL", contract_type="put")
        self.assertIn("AAPL260116P00150000", out["options_chain_input"])

    def test_put_drafter_includes_options_chain_and_sizing(self):
        state = {
            "screener_output": json.dumps({"approved_tickers": ["AAPL"]}),
            "portfolio_state": json.dumps(
                {"ticker": "NONE", "cash": 10000, "shares": 0, "spot": 0, "cost_basis": 0}
            ),
            "options_chain_input": (
                "[Put 15 Exp 2026-01-16 Symbol: AAPL260116P00015000 "
                "Bid: 1.1 Ask: 1.2 Delta: -0.2500 POP: 75.0%]"
            ),
        }

        out = graph.put_drafter_node(state)
        ticket = json.loads(out["draft_ticket"])

        self.assertEqual(ticket["action"], "SELL_CSP")
        self.assertEqual(ticket["ticker"], "AAPL")
        self.assertEqual(ticket["symbol"], "AAPL260116P00015000")
        self.assertEqual(ticket["pop"], 75.0)
        self.assertEqual(ticket["max_risk"], 2000)
        self.assertIn("AAPL260116P00015000", ticket["options_chain"])

    def test_put_drafter_no_trade_when_chain_lacks_pop(self):
        state = {
            "screener_output": json.dumps({"approved_tickers": ["AAPL"]}),
            "portfolio_state": json.dumps(
                {"ticker": "NONE", "cash": 10000, "shares": 0, "spot": 0, "cost_basis": 0}
            ),
            "options_chain_input": "[Put 15 Symbol: AAPL260116P00015000 Bid: 1.1 Ask: 1.2]",
        }

        out = graph.put_drafter_node(state)
        ticket = json.loads(out["draft_ticket"])

        self.assertEqual(ticket["action"], "NO_TRADE")
        self.assertIn("POP > 70%", ticket["reason"])

    def test_ticket_validator_accepts_nominal_and_rejects_bad_cash_ticket(self):
        invalid = graph.ticket_validator_node(
            {
                "draft_ticket": json.dumps({"action": "SELL_CSP"}),
                "ticket_source": "PUT_DRAFTER",
            }
        )
        valid = graph.ticket_validator_node(
            {
                "draft_ticket": json.dumps({"action": "SELL_COVERED_CALL"}),
                "ticket_source": "ASSET_NOMINAL_DRAFTER",
            }
        )

        self.assertEqual(invalid["ticket_validation_status"], "invalid")
        self.assertEqual(valid["ticket_validation_status"], "valid")

    def test_cash_cro_retry_exhaustion_aborts_instead_of_liquidating(self):
        state = {
            "active_path": "cash",
            "ticket_source": "PUT_DRAFTER",
            "cro_retries": graph.CRO_REJECT_MAX,
            "last_cro_reason": "INSUFFICIENT_DATA: missing Probability of Profit",
            "cro_output": json.dumps(
                {
                    "status": "REJECTED",
                    "reason": "INSUFFICIENT_DATA: missing Probability of Profit",
                }
            ),
        }

        route = graph._route_after_cro(state)
        out = graph.cro_rejected_abort_node(state)

        self.assertEqual(route, "abort_rejected")
        self.assertIn("No position exists to liquidate", out["abort_reason"])

    def test_format_result_includes_terminal_sections(self):
        formatted = graph.format_trading_flow_state(
            {
                "macro_output": '{"status":"CLEAR"}',
                "candidate_selector_output": '{"selected_tickers":["AAPL"],"reason":"ok"}',
                "draft_ticket": '{"action":"SELL_COVERED_CALL"}',
                "execution_output": '{"symbol":"AAPL260116C00170000"}',
            }
        )

        self.assertIn("MACRO_SENTINEL", formatted)
        self.assertIn("CANDIDATE_SELECTOR", formatted)
        self.assertIn("DRAFT_TICKET", formatted)
        self.assertIn("EXECUTION_BROKER", formatted)


if __name__ == "__main__":
    unittest.main()
