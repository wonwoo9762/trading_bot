from __future__ import annotations

import json
import sys
import unittest
from datetime import date, timedelta
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
        self.assertEqual(
            graph._derive_route_from_portfolio(
                json.dumps(
                    {
                        "cash": 10000,
                        "shares": 0,
                        "short_puts": [{"symbol": "AAPL_PUT", "qty": 1}],
                    }
                )
            ),
            "SHORT_PUT_OPEN",
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

    def test_macro_halt_still_allows_risk_reducing_short_put_management(self):
        state = {
            "portfolio_state": json.dumps(
                {
                    "cash": 10000,
                    "shares": 0,
                    "short_puts": [{"symbol": "AAPL_PUT", "qty": 1}],
                }
            ),
            "macro_output": json.dumps(
                {"status": "HALT", "reason": "VIX above threshold"}
            ),
        }

        self.assertEqual(graph._route_after_macro(state), "clear")

    def test_macro_halt_blocks_new_cash_entry(self):
        state = {
            "portfolio_state": json.dumps({"cash": 10000, "shares": 0}),
            "macro_output": json.dumps(
                {"status": "HALT", "reason": "VIX above threshold"}
            ),
        }

        self.assertEqual(graph._route_after_macro(state), "halt")

    def test_short_put_data_gate_does_not_require_macro_for_a_close(self):
        state = {
            "route_to": "SHORT_PUT_OPEN",
            "portfolio_state": json.dumps(
                {"short_puts": [{"symbol": "AAPL_PUT", "qty": 1}]}
            ),
            "macro_input": "",
            "options_chain_input": "[quoted put]",
        }

        result = graph.data_gate_node(state)

        self.assertEqual(result["data_gate_status"], "ok")

    def test_cash_options_chain_fetches_after_screener_selects_ticker(self):
        state = {
            "screener_output": json.dumps({"approved_tickers": ["AAPL"]}),
        }

        with mock.patch(
            "data_feeds.fetch_options_chain",
            return_value="[Put 150 Symbol: AAPL260116P00150000 Bid: 1.1 Ask: 1.2]",
        ) as fetch_options_chain:
            out = graph.cash_options_chain_node(state)

        fetch_options_chain.assert_called_once_with(
            "AAPL",
            contract_type="put",
            min_dte=graph.CSP_MIN_DTE,
            max_dte=graph.CSP_MAX_DTE,
        )
        self.assertIn("AAPL260116P00150000", out["options_chain_input"])

    def test_put_drafter_includes_options_chain_and_sizing(self):
        expiration = date.today() + timedelta(days=14)
        state = {
            "screener_output": json.dumps({"approved_tickers": ["AAPL"]}),
            "portfolio_state": json.dumps(
                {"ticker": "NONE", "cash": 10000, "shares": 0, "spot": 0, "cost_basis": 0}
            ),
            "options_chain_input": (
                f"[Put 15 Exp {expiration.isoformat()} Underlying: AAPL "
                "Symbol: AAPL270101P00015000 Bid: 0.15 Ask: 0.16 "
                "Delta: -0.2500 POP: 75.0% IV: 0.30 OI: 500]"
            ),
        }

        out = graph.put_drafter_node(state)
        ticket = json.loads(out["draft_ticket"])

        self.assertEqual(ticket["action"], "SELL_CSP")
        self.assertEqual(ticket["ticker"], "AAPL")
        self.assertEqual(ticket["symbol"], "AAPL270101P00015000")
        self.assertEqual(ticket["pop"], 75.0)
        self.assertEqual(ticket["dte"], 14)
        self.assertEqual(ticket["qty"], 1)
        self.assertGreaterEqual(ticket["annualized_yield_pct"], 20)
        self.assertLessEqual(ticket["annualized_yield_pct"], 35)
        self.assertEqual(ticket["max_risk"], 1500)
        self.assertIn("risk_adjusted_score", ticket)
        self.assertIn("AAPL270101P00015000", ticket["options_chain"])

    def test_put_drafter_no_trade_when_chain_lacks_pop(self):
        expiration = date.today() + timedelta(days=14)
        state = {
            "screener_output": json.dumps({"approved_tickers": ["AAPL"]}),
            "portfolio_state": json.dumps(
                {"ticker": "NONE", "cash": 10000, "shares": 0, "spot": 0, "cost_basis": 0}
            ),
            "options_chain_input": (
                f"[Put 15 Exp {expiration.isoformat()} Underlying: AAPL "
                "Symbol: AAPL270101P00015000 Bid: 0.15 Ask: 0.16 OI: 500]"
            ),
        }

        out = graph.put_drafter_node(state)
        ticket = json.loads(out["draft_ticket"])

        self.assertEqual(ticket["action"], "NO_TRADE")
        self.assertIn("70-85% delta-proxy POP", ticket["reason"])

    def test_put_drafter_rejects_one_day_contract(self):
        expiration = date.today() + timedelta(days=1)
        state = {
            "screener_output": json.dumps({"approved_tickers": ["AAPL"]}),
            "portfolio_state": json.dumps(
                {"ticker": "NONE", "cash": 100000, "nlv": 100000, "shares": 0}
            ),
            "options_chain_input": (
                f"[Put 150 Exp {expiration.isoformat()} Underlying: AAPL "
                "Symbol: AAPL270101P00150000 Bid: 0.75 Ask: 0.76 "
                "Delta: -0.25 POP: 75% OI: 1000]"
            ),
        }

        ticket = json.loads(graph.put_drafter_node(state)["draft_ticket"])

        self.assertEqual(ticket["action"], "NO_TRADE")
        self.assertIn("7-45 DTE", ticket["reason"])

    def test_selector_skips_underlying_with_existing_short_put(self):
        expiration = date.today() + timedelta(days=14)
        portfolio = graph._parse_portfolio(
            json.dumps(
                {
                    "cash": 100000,
                    "nlv": 100000,
                    "shares": 0,
                    "short_put_collateral": 15000,
                    "short_puts": [
                        {"underlying": "AAPL", "collateral": 15000}
                    ],
                }
            )
        )
        chain = "\n".join(
            [
                (
                    f"[Put 15 Exp {expiration.isoformat()} Underlying: AAPL "
                    "Symbol: AAPL270101P00015000 Bid: 0.15 Ask: 0.16 "
                    "Delta: -0.25 POP: 75% OI: 500]"
                ),
                (
                    f"[Put 20 Exp {expiration.isoformat()} Underlying: MSFT "
                    "Symbol: MSFT270101P00020000 Bid: 0.19 Ask: 0.20 "
                    "Delta: -0.25 POP: 75% OI: 500]"
                ),
            ]
        )

        selected, _ = graph._select_cash_secured_put_contract(chain, portfolio)

        self.assertEqual(selected["underlying"], "MSFT")
        self.assertEqual(selected["qty"], 7)

    def test_selector_has_no_hidden_fourteen_day_preference(self):
        expiration_14 = date.today() + timedelta(days=14)
        expiration_30 = date.today() + timedelta(days=30)
        portfolio = graph._parse_portfolio(
            json.dumps({"cash": 100000, "nlv": 100000, "shares": 0})
        )
        chain = "\n".join(
            [
                (
                    f"[Put 100 Exp {expiration_14.isoformat()} Underlying: AAPL "
                    "Symbol: AAPL14P00100000 Bid: 0.80 Ask: 0.82 "
                    "Delta: -0.25 POP: 75% OI: 500]"
                ),
                (
                    f"[Put 100 Exp {expiration_30.isoformat()} Underlying: AAPL "
                    "Symbol: AAPL30P00100000 Bid: 1.70 Ask: 1.72 "
                    "Delta: -0.25 POP: 75% OI: 500]"
                ),
            ]
        )

        selected, reason = graph._select_cash_secured_put_contract(chain, portfolio)

        self.assertEqual(selected["dte"], 30)
        self.assertIn("no target expiration", reason)
        self.assertGreater(
            selected["risk_adjusted_score"], selected["gamma_risk_penalty"]
        )

    def test_short_put_manager_closes_after_half_the_premium_is_captured(self):
        expiration = date.today() + timedelta(days=20)
        symbol = "AAPL270101P00100000"
        state = {
            "portfolio_state": json.dumps(
                {
                    "ticker": "AAPL",
                    "cash": 20000,
                    "nlv": 30000,
                    "shares": 0,
                    "short_put_collateral": 10000,
                    "short_puts": [
                        {
                            "symbol": symbol,
                            "underlying": "AAPL",
                            "qty": 1,
                            "collateral": 10000,
                            "entry_credit": 2.0,
                            "dte": 20,
                        }
                    ],
                }
            ),
            "options_chain_input": (
                f"[Put 100 Exp {expiration.isoformat()} Underlying: AAPL "
                f"Symbol: {symbol} Bid: 0.80 Ask: 0.90 Delta: -0.12 OI: 500]"
            ),
        }

        ticket = json.loads(graph.short_put_manager_node(state)["draft_ticket"])

        self.assertEqual(ticket["action"], "CLOSE_SHORT_PUT")
        self.assertEqual(ticket["side"], "buy")
        self.assertEqual(ticket["profit_capture_pct_at_ask"], 55.0)

    def test_short_put_manager_holds_when_close_threshold_is_not_met(self):
        expiration = date.today() + timedelta(days=20)
        symbol = "AAPL270101P00100000"
        state = {
            "portfolio_state": json.dumps(
                {
                    "ticker": "AAPL",
                    "cash": 20000,
                    "nlv": 30000,
                    "shares": 0,
                    "short_put_collateral": 10000,
                    "short_puts": [
                        {
                            "symbol": symbol,
                            "underlying": "AAPL",
                            "qty": 1,
                            "collateral": 10000,
                            "entry_credit": 2.0,
                            "dte": 20,
                        }
                    ],
                }
            ),
            "options_chain_input": (
                f"[Put 100 Exp {expiration.isoformat()} Underlying: AAPL "
                f"Symbol: {symbol} Bid: 1.40 Ask: 1.50 Delta: -0.30 OI: 500]"
            ),
        }

        ticket = json.loads(graph.short_put_manager_node(state)["draft_ticket"])

        self.assertEqual(ticket["action"], "NO_TRADE")
        self.assertIn("25.0% captured", ticket["reason"])

    def test_nominal_ticket_selects_call_above_cost_basis(self):
        expiration = date.today() + timedelta(days=30)
        state = {
            "portfolio_state": json.dumps(
                {
                    "ticker": "AAPL",
                    "cash": 1000,
                    "nlv": 11000,
                    "shares": 100,
                    "spot": 100,
                    "cost_basis": 95,
                }
            ),
            "options_chain_input": (
                f"[Call 105 Exp {expiration.isoformat()} Underlying: AAPL "
                "Symbol: AAPL30C00105000 Bid: 1.00 Ask: 1.10 "
                "Delta: 0.25 OI: 500]"
            ),
        }

        ticket = json.loads(graph.nominal_ticket_node(state)["draft_ticket"])

        self.assertEqual(ticket["action"], "SELL_COVERED_CALL")
        self.assertEqual(ticket["strike"], 105.0)
        self.assertEqual(ticket["qty"], 1)

    def test_nominal_ticket_blocks_duplicate_short_call(self):
        expiration = date.today() + timedelta(days=30)
        state = {
            "portfolio_state": json.dumps(
                {
                    "ticker": "AAPL",
                    "cash": 1000,
                    "nlv": 11000,
                    "shares": 100,
                    "spot": 100,
                    "cost_basis": 95,
                    "short_calls": [{"underlying": "AAPL", "qty": 1}],
                }
            ),
            "options_chain_input": (
                f"[Call 105 Exp {expiration.isoformat()} Underlying: AAPL "
                "Symbol: AAPL30C00105000 Bid: 1.00 Ask: 1.10 "
                "Delta: 0.25 OI: 500]"
            ),
        }

        ticket = json.loads(graph.nominal_ticket_node(state)["draft_ticket"])

        self.assertEqual(ticket["action"], "NO_TRADE")
        self.assertIn("duplicate coverage", ticket["reason"])

    def test_distressed_manual_review_never_becomes_liquidation(self):
        state = {
            "opportunity_output": json.dumps(
                {"decision": "MANUAL_REVIEW", "reason": "No verified repair"}
            ),
            "quant_output": json.dumps(
                {"action": "NO_TRADE", "reason": "INSUFFICIENT_CHAIN_DATA"}
            ),
        }

        ticket = json.loads(graph.distressed_decider_node(state)["draft_ticket"])

        self.assertEqual(ticket["action"], "NO_TRADE")
        self.assertNotEqual(ticket["action"], "LIQUIDATE")

    def test_execution_broker_constructs_approved_order_without_llm(self):
        state = {
            "cro_output": '{"status":"APPROVED","reason":"Validated"}',
            "draft_ticket": json.dumps({
                "action": "SELL_CSP", "symbol": "AAPL270101P00300000",
                "qty": 4, "bid": 2.0, "ask": 2.1,
            }),
            "options_chain_input": "Unrelated contracts must not change execution",
        }
        with mock.patch.object(graph, "_invoke_structured", side_effect=AssertionError("No execution LLM")):
            result = graph.execution_broker_node(state)
        order = json.loads(result["execution_output"])
        self.assertEqual(order["symbol"], "AAPL270101P00300000")
        self.assertEqual(order["qty"], 4)
        self.assertEqual(order["limit_price"], 2.05)
        self.assertEqual(order["position_intent"], "sell_to_open")
        self.assertEqual(order["legs"], [])

    def test_execution_broker_blocks_missing_approval_or_bad_ticket(self):
        approved = '{"status":"APPROVED","reason":"Validated"}'
        ticket = json.dumps({"action": "SELL_CSP", "symbol": "AAPL270101P00300000", "qty": 1, "bid": 2.0, "ask": 2.1})
        for state in ({"draft_ticket": ticket}, {"cro_output": approved, "draft_ticket": "[]"}, {"cro_output": approved, "draft_ticket": '{"action":"ROLL"}'}):
            with self.subTest(state=state):
                result = json.loads(graph.execution_broker_node(state)["execution_output"])
                self.assertEqual(result["error"], "ORDER_POLICY_BLOCKED")

    def test_empty_invalid_or_missing_candidate_selection_stops_screening(self):
        fundamentals = json.dumps([{"ticker": "AAPL", "fcf": 100, "debt_to_equity": 0.5, "mkt_cap": 1e12}])
        for selection in ('{"selected_tickers":[],"reason":"Adverse news"}', "invalid", ""):
            with self.subTest(selection=selection):
                with mock.patch.object(graph, "_invoke_structured", side_effect=AssertionError("Veto must stop screening")):
                    result = graph.fundamental_screener_node({"candidate_selector_output": selection, "fundamentals_input": fundamentals})
                self.assertEqual(json.loads(result["screener_output"])["approved_tickers"], [])

    def test_selector_failure_fails_closed(self):
        with mock.patch.object(graph, "_invoke_structured", return_value=(None, None)):
            result = graph.candidate_selector_node({"candidate_universe_input": '[{"ticker":"AAPL","fcf":100,"debt_to_equity":0.5,"mkt_cap":1000000000000}]'})
        selection = json.loads(result["candidate_selector_output"])
        self.assertEqual(selection["selected_tickers"], [])
        self.assertEqual(selection["reason"], "LLM_FAILURE_FAIL_CLOSED")

    def test_selector_and_screener_cannot_expand_authorized_universe(self):
        from models import CandidateSelectorOutput, ScreenerOutput
        state = {"candidate_universe_input": '[{"ticker":"AAPL"}]'}
        with mock.patch.object(graph, "_invoke_structured", return_value=(None, CandidateSelectorOutput(selected_tickers=[" aapl ", "MSFT", "AAPL"]))):
            selected = graph.candidate_selector_node(state)
        self.assertEqual(json.loads(selected["candidate_selector_output"])["selected_tickers"], ["AAPL"])
        state = {**selected, "fundamentals_input": '[{"ticker":"AAPL"},{"ticker":"MSFT"}]'}
        with mock.patch.object(graph, "_invoke_structured", return_value=(None, ScreenerOutput(approved_tickers=["MSFT", "aapl", "AAPL"]))) as invoke:
            screened = graph.fundamental_screener_node(state)
        self.assertNotIn("MSFT", invoke.call_args.args[2])
        self.assertEqual(json.loads(screened["screener_output"])["approved_tickers"], ["AAPL"])

    def test_approved_repair_remains_manual_review(self):
        for action in ("ROLL", "SPREAD"):
            with self.subTest(action=action):
                quant = json.dumps({"action": action, "est_credit": 2.5})
                result = graph.distressed_decider_node({"quant_output": quant, "opportunity_output": '{"decision":"APPROVE_ROLL","reason":"Positive credit"}'})
                ticket = json.loads(result["draft_ticket"])
                self.assertEqual(ticket["action"], "NO_TRADE")
                self.assertIn("AUTOMATED_REPAIR_DISABLED", ticket["reason"])
                validation = graph.ticket_validator_node({"draft_ticket": quant, "ticket_source": "OPTIONS_QUANT"})
                self.assertEqual(validation["ticket_validation_status"], "invalid")

    def test_ticket_validator_accepts_nominal_and_rejects_bad_cash_ticket(self):
        invalid = graph.ticket_validator_node(
            {
                "draft_ticket": json.dumps({"action": "SELL_CSP"}),
                "ticket_source": "PUT_DRAFTER",
            }
        )
        valid = graph.ticket_validator_node(
            {
                "draft_ticket": json.dumps(
                    {
                        "action": "SELL_COVERED_CALL",
                        "symbol": "AAPL30C00170000",
                        "strike": 170,
                        "qty": 1,
                        "bid": 1.0,
                        "ask": 1.1,
                        "dte": 30,
                        "delta": 0.25,
                        "open_interest": 500,
                        "spread_pct": 9.52,
                        "cost_basis": 160,
                        "spot": 165,
                    }
                ),
                "ticket_source": "ASSET_NOMINAL_DRAFTER",
            }
        )

        self.assertEqual(invalid["ticket_validation_status"], "invalid")
        self.assertEqual(valid["ticket_validation_status"], "valid")

    def test_ticket_validator_accepts_exact_short_put_close(self):
        result = graph.ticket_validator_node(
            {
                "draft_ticket": json.dumps(
                    {
                        "action": "CLOSE_SHORT_PUT",
                        "symbol": "AAPL270101P00100000",
                        "qty": 1,
                        "bid": 0.8,
                        "ask": 0.9,
                        "entry_credit": 2.0,
                        "dte": 20,
                    }
                ),
                "ticket_source": "SHORT_PUT_MANAGER",
            }
        )

        self.assertEqual(result["ticket_validation_status"], "valid")

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
        self.assertIn("No transaction will be made", out["abort_reason"])

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
