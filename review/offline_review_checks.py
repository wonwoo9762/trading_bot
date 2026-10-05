"""Reproduce review findings without broker, email, or LLM network access.

Historical reproduction for commit 6c79945, before the execution fixes.
Do not use this as a regression test on the current tree: assertions deliberately
expect the old unsafe behavior. Captured results are preserved in the adjacent JSON.
Current safety tests: wheel_bot/tests/integration/execution_safety_scenarios.py.
These are observations of the reviewed version, not passing safety tests.
Real LangGraph and Alpaca request models are used; external services are fakes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wheel_bot"))


def deny_network(*args, **kwargs):
    raise RuntimeError("Network disabled by offline review")


socket.socket.connect = deny_network
socket.create_connection = deny_network

import broker
import config
import data_feeds
import scheduler
from agents import graph
from models import BrokerOutput, CandidateSelectorOutput, CROOutput, MacroSentinelOutput
from models import OrchestratorOutput, QuantOutput, ScreenerOutput


class FakeBroker:
    def __init__(self):
        self.orders = []

    def submit_order(self, order_data):
        self.orders.append(order_data)
        return SimpleNamespace(id=f"fake-order-{len(self.orders)}")


def make_broker():
    result = object.__new__(broker.WheelBroker)
    result._paper = True
    result._client = FakeBroker()
    return result


def main():
    findings = {}
    expiration = date.today() + timedelta(days=14)
    symbol = f"AAPL{expiration:%y%m%d}P00100000"
    chain = (
        f"[Put 100 Exp {expiration} Underlying: AAPL Symbol: {symbol} "
        "Bid: 1.00 Ask: 1.10 Delta: -0.25 POP: 75% IV: 0.30 OI: 500]"
    )
    approved_ticket = {
        "action": "SELL_CSP", "symbol": symbol, "qty": 1,
        "bid": 1.0, "ask": 1.1,
    }
    single_params = {"symbol": symbol, "qty": 1, "side": "sell", "limit_price": 1.05}

    # A model-valid multi-leg response takes a branch before single-leg checks.
    b = make_broker()
    substituted = BrokerOutput(
        qty=50, limit_price=-1.0,
        legs=[
            {"symbol": f"MSFT{expiration:%y%m%d}P00200000", "side": "sell",
             "position_intent": "sell_to_open"},
            {"symbol": f"MSFT{expiration:%y%m%d}P00190000", "side": "buy",
             "position_intent": "buy_to_open"},
        ],
    )
    result = b.execute(json.dumps(approved_ticket), substituted.model_dump_json(), human_approved=True)
    assert result.status == "SUBMITTED"
    findings["single_leg_approval_bypassed_by_legs"] = {
        "approved_qty": 1, "submitted_qty": b._client.orders[0].qty,
        "approved_symbol": symbol,
        "submitted_symbols": [leg.symbol for leg in b._client.orders[0].legs],
        "result": result.status,
    }

    # The optional Quant schema and source-based validator accept an empty roll.
    quant = QuantOutput(action="ROLL", est_credit=-5.0)
    validation = graph.ticket_validator_node({
        "draft_ticket": quant.model_dump_json(), "ticket_source": "OPTIONS_QUANT",
    })
    assert validation["ticket_validation_status"] == "valid"
    findings["incomplete_negative_credit_roll_validated"] = validation

    b = make_broker()
    for _ in range(2):
        result = b.execute(json.dumps(approved_ticket), json.dumps(single_params), human_approved=True)
        assert result.status == "SUBMITTED"
    ids = [order.client_order_id for order in b._client.orders]
    assert ids[0] != ids[1]
    findings["duplicate_submission_not_deduplicated"] = {
        "submitted_orders": len(ids), "different_client_order_ids": True,
        "single_leg_position_intent": b._client.orders[0].position_intent,
    }

    # Empty candidate selection does not remove candidates from screener input.
    fundamentals = json.dumps([{
        "ticker": "AAPL", "fcf": 100, "debt_to_equity": 0.5,
        "mkt_cap": 100_000_000_000, "news": "Unresolved adverse event",
    }])
    with patch.object(graph, "_invoke_structured", return_value=(None, ScreenerOutput(approved_tickers=["AAPL"]))) as invoke:
        screened = graph.fundamental_screener_node({
            "candidate_selector_output": CandidateSelectorOutput(selected_tickers=[], reason="Reject all").model_dump_json(),
            "fundamentals_input": fundamentals,
        })
    assert "AAPL" in invoke.call_args.args[2]
    findings["empty_candidate_veto_lost"] = json.loads(screened["screener_output"])

    # Real graph: a first put uses at most 15%; subsequent runs take no entry path.
    first_portfolio = {"ticker": "NONE", "cash": 1_000_000, "nlv": 1_000_000, "shares": 0}
    first = json.loads(graph.put_drafter_node({
        "portfolio_state": json.dumps(first_portfolio),
        "screener_output": '{"approved_tickers":["AAPL"]}',
        "options_chain_input": chain,
    })["draft_ticket"])
    assert first["total_collateral"] == 150_000
    existing = {
        "ticker": "AAPL", "cash": 1_001_500, "nlv": 1_000_000, "shares": 0,
        "short_put_collateral": 150_000,
        "short_puts": [{"symbol": symbol, "underlying": "AAPL", "qty": 15,
                        "collateral": 150_000, "entry_credit": 1.0, "dte": 14}],
    }
    invoked = []

    def fake_llm(schema, role_prompt, human_content):
        invoked.append(schema.__name__)
        if schema is MacroSentinelOutput:
            return None, MacroSentinelOutput(status="CLEAR", reason="Synthetic input")
        if schema is OrchestratorOutput:
            return None, OrchestratorOutput(route_to="CASH", action="Try another entry")
        raise AssertionError(f"Unexpected model call: {schema.__name__}")

    with patch.object(graph, "_invoke_structured", side_effect=fake_llm):
        terminal = graph.run_trading_flow_state(
            json.dumps(existing), macro_input="Synthetic macro", options_chain_input=chain,
        )
    assert terminal["route_to"] == "SHORT_PUT_OPEN"
    assert json.loads(terminal["draft_ticket"])["action"] == "NO_TRADE"
    findings["real_graph_stops_new_entries_after_first_put"] = {
        "first_collateral": first["total_collateral"], "nlv": 1_000_000,
        "next_route": terminal["route_to"], "model_roles_called": invoked,
        "next_ticket": json.loads(terminal["draft_ticket"]),
    }

    # A stale source timestamp is discarded before deterministic selection.
    contract = SimpleNamespace(
        type=SimpleNamespace(value="put"), strike_price="100", expiration_date=expiration,
        symbol=symbol, close_price="1.0", open_interest="500",
    )
    client = SimpleNamespace(get_option_contracts=lambda req: SimpleNamespace(option_contracts=[contract]))
    snapshot = SimpleNamespace(
        latest_quote=SimpleNamespace(bid_price=1.0, ask_price=1.1,
                                    timestamp=datetime(2000, 1, 1, tzinfo=timezone.utc)),
        greeks=SimpleNamespace(delta=-0.25), implied_volatility=0.30,
    )
    with patch.object(config, "get_alpaca_credentials", return_value=("fake", "fake")), \
         patch.object(config, "get_alpaca_trading_client", return_value=client), \
         patch.object(data_feeds, "_fetch_option_snapshots", return_value={symbol: snapshot}), \
         patch.object(data_feeds, "_fetch_option_latest_quotes", return_value={}):
        stale_chain = data_feeds.fetch_options_chain("AAPL")
    stale_ticket = json.loads(graph.put_drafter_node({
        "portfolio_state": json.dumps(first_portfolio),
        "screener_output": '{"approved_tickers":["AAPL"]}',
        "options_chain_input": stale_chain,
    })["draft_ticket"])
    assert stale_ticket["action"] == "SELL_CSP"
    findings["stale_quote_still_produces_entry"] = {
        "source_quote_year": 2000, "timestamp_survived": "2000" in stale_chain,
        "action": stale_ticket["action"],
    }

    # Actual calendar errors in the review year.
    data_feeds._NYSE_HOLIDAYS.clear()
    days = [date(2026, 11, 26), date(2026, 11, 27), date(2026, 7, 3), date(2026, 9, 1)]
    findings["calendar_classification"] = {str(day): data_feeds.is_market_day(day) for day in days}
    assert findings["calendar_classification"]["2026-11-26"] is True
    assert findings["calendar_classification"]["2026-11-27"] is False

    # Portfolio reporting: actual option premium P&L is +50%, reported -5000%.
    account = SimpleNamespace(cash="1000000", buying_power="1000000", portfolio_value="1000000", equity="1000000")
    position = SimpleNamespace(symbol=symbol, qty="-2", avg_entry_price="2", current_price="1", market_value="-200", unrealized_pl="200")
    account_client = SimpleNamespace(get_account=lambda: account, get_all_positions=lambda: [position])
    with patch.object(data_feeds, "_get_trading_client", return_value=account_client):
        summary = data_feeds.fetch_account_summary()
    assert summary["positions"][0]["unrealized_pct"] == -5000.0
    findings["option_pnl_percent_wrong_sign_and_multiplier"] = summary["positions"][0]

    # A submission acknowledgment alone is rendered as a transaction made.
    findings["submitted_is_reported_as_transaction_made"] = scheduler._build_transaction_summary(
        order_result={"status": "SUBMITTED", "order_id": "unfilled-fake-order"},
    )
    assert findings["submitted_is_reported_as_transaction_made"]["transaction_made"] is True

    # Verify the actual LangGraph retry loop, which the existing suite stubs out.
    calls = {"CROOutput": 0}

    def rejected_llm(schema, role_prompt, human_content):
        if schema is MacroSentinelOutput:
            return None, MacroSentinelOutput(status="CLEAR", reason="Synthetic")
        if schema is OrchestratorOutput:
            return None, OrchestratorOutput(route_to="CASH", action="Synthetic")
        if schema is CandidateSelectorOutput:
            return None, CandidateSelectorOutput(selected_tickers=["AAPL"])
        if schema is ScreenerOutput:
            return None, ScreenerOutput(approved_tickers=["AAPL"])
        if schema is CROOutput:
            calls["CROOutput"] += 1
            return None, CROOutput(status="REJECTED", reason="Synthetic rejection")
        raise AssertionError(f"Unexpected schema: {schema}")

    with patch.object(graph, "_invoke_structured", side_effect=rejected_llm), \
         patch.object(data_feeds, "fetch_options_chain", return_value=chain):
        try:
            rejected = graph.run_trading_flow_state(
                json.dumps(first_portfolio), macro_input="Synthetic", fundamentals_input=fundamentals,
                candidate_universe_input=fundamentals,
            )
            findings["real_graph_cro_retries"] = {"calls": calls, "abort_reason": rejected.get("abort_reason")}
        except Exception as exc:
            findings["real_graph_cro_retries"] = {"calls": calls, "error_type": type(exc).__name__, "message": str(exc)}

    # Construction only: current LangChain removes unsupported temperature=0.
    from langchain_openai import ChatOpenAI
    model = ChatOpenAI(model="gpt-5-mini", temperature=0, api_key="offline-test-key")
    findings["current_model_temperature_normalized"] = {"effective_temperature": model.temperature}

    payload = json.dumps(findings, indent=2, default=str)
    Path(__file__).with_name("offline_review_results.json").write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
