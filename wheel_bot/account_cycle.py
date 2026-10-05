"""Evaluate position management and then unused capital, with one order per scan."""
from __future__ import annotations

import json
from portfolio_policy import allocation_summary, entry_budget


def evaluate_account(portfolio_json: str, *, run_flow, focus_ticker=None, **inputs) -> dict:
    portfolio = json.loads(portfolio_json)
    # Legacy callers without a complete account snapshot keep the original route.
    if portfolio.get("positions_complete") is not True:
        return run_flow(portfolio_json, **inputs)
    summary = allocation_summary(portfolio)
    evaluations = []
    blockers = list(portfolio.get("unsupported_positions") or [])
    views = []
    if portfolio.get("short_puts"):
        views.append(("short_put_management", portfolio))
    for stock in sorted(portfolio["equity_positions"], key=lambda p: -abs(p["market_value"])):
        if stock["shares"] <= 0:
            continue
        if stock["spot"] < stock["cost_basis"] * 0.95:
            blockers.append(f"{stock['ticker']}: distressed shares require manual review")
        if focus_ticker and stock["ticker"] != focus_ticker.upper():
            continue
        views.append((f"equity_{stock['ticker']}", portfolio | stock | {"evaluation_scope": "equity"}))

    def finish(state):
        return dict(state) | {"account_evaluations": evaluations, "allocation_summary": summary}

    def evaluate(label, view):
        scoped_inputs = dict(inputs)
        if inputs.get("thread_id"):
            scoped_inputs["thread_id"] = f"{inputs['thread_id']}-{label}"
        state = run_flow(json.dumps(view), **scoped_inputs)
        ticket = json.loads(state.get("draft_ticket") or "{}")
        macro = json.loads(state.get("macro_output") or "{}")
        evaluations.append({
            "scope": label, "action": ticket.get("action", "NONE"),
            "reason": state.get("abort_reason") or state.get("data_gate_reason") or ticket.get("reason") or ticket.get("selection_reason") or macro.get("reason", ""),
        })
        if ticket.get("manual_review_required"):
            blockers.append(str(ticket.get("reason") or "Position needs review"))
        return state, ticket

    for label, view in views:
        state, ticket = evaluate(label, view)
        # Never route around an approval/data failure, or reuse a snapshot after submission.
        if state.get("abort_reason") or state.get("data_gate_status") == "blocked" or ticket.get("action") != "NO_TRADE":
            return finish(state)

    budget = entry_budget(portfolio)
    if blockers or budget["remaining"] <= 0:
        reason = ("New entries blocked for manual review: " + "; ".join(blockers)) if blockers else "No cash-secured-put allocation remains within cash and portfolio limits."
        return finish({"draft_ticket": json.dumps({"action": "NO_TRADE", "reason": reason})})

    state, _ = evaluate("entry", portfolio | {"evaluation_scope": "entry"})
    return finish(state)
