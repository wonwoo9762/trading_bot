"""Account allocation limits shared by planning and submission.

The return objective is a reporting benchmark, never permission to increase risk.
"""
from __future__ import annotations

import math
from typing import Any

MAX_POSITION_PCT = 0.15
MAX_TOTAL_CSP_PCT = 0.50
CSP_MIN_ANNUALIZED_YIELD_PCT = 20.0
CSP_MAX_ANNUALIZED_YIELD_PCT = 35.0
TARGET_ANNUAL_RETURN_MIN_PCT = 25.0
TARGET_ANNUAL_RETURN_MAX_PCT = 30.0


def finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def entry_budget(portfolio: dict) -> dict:
    """Compute cash remaining without using margin or releasing open collateral."""
    nlv = finite_number(portfolio.get("nlv"), "nlv")
    cash = finite_number(portfolio.get("cash"), "cash")
    collateral = finite_number(portfolio.get("short_put_collateral", 0), "collateral")
    if nlv <= 0 or collateral < 0:
        raise ValueError("Positive NLV and nonnegative collateral required")
    puts = portfolio.get("short_puts", [])
    stocks = portfolio.get("equity_positions", [])
    calls = portfolio.get("short_calls", [])
    if not all(isinstance(items, list) for items in (puts, stocks, calls)):
        raise ValueError("Position lists are required")
    # Do not add a second wheel in a stock we already own or have option exposure to.
    occupied = set()
    for item in puts + calls:
        if not isinstance(item, dict) or not item.get("underlying"):
            raise ValueError("Option underlying missing")
        occupied.add(str(item["underlying"]).upper())
    for item in stocks:
        if not isinstance(item, dict) or not item.get("ticker"):
            raise ValueError("Equity ticker missing")
        if finite_number(item.get("shares"), "shares") != 0:
            occupied.add(str(item["ticker"]).upper())
    if finite_number(portfolio.get("shares", 0), "shares") != 0 and portfolio.get("ticker"):
        occupied.add(str(portfolio["ticker"]).upper())
    remaining = max(0.0, min(cash - collateral, MAX_TOTAL_CSP_PCT * nlv - collateral))
    return {
        "nlv": nlv, "cash": cash, "collateral": collateral,
        "remaining": remaining, "per_position": MAX_POSITION_PCT * nlv,
        "occupied_underlyings": sorted(occupied),
    }


def allocation_summary(portfolio: dict) -> dict:
    budget = entry_budget(portfolio)
    utilization = budget["collateral"] / budget["nlv"] * 100
    return {
        "annual_account_return_target_pct": [TARGET_ANNUAL_RETURN_MIN_PCT, TARGET_ANNUAL_RETURN_MAX_PCT],
        "target_basis": "Annual account return before tax; objective, not a forecast",
        "nlv": round(budget["nlv"], 2),
        "csp_collateral": round(budget["collateral"], 2),
        "csp_utilization_pct": round(utilization, 2),
        "remaining_csp_budget": round(budget["remaining"], 2),
        "per_underlying_limit_pct": MAX_POSITION_PCT * 100,
        "total_csp_limit_pct": MAX_TOTAL_CSP_PCT * 100,
        "occupied_underlyings": budget["occupied_underlyings"],
        "premium_only_full_utilization_illustration_pct": [
            MAX_TOTAL_CSP_PCT * CSP_MIN_ANNUALIZED_YIELD_PCT,
            MAX_TOTAL_CSP_PCT * CSP_MAX_ANNUALIZED_YIELD_PCT,
        ],
        "target_gap": "At the current 50% CSP cap, the 20-35% collateral yield screen corresponds to roughly 10-17.5% gross annual account premium at continuous full utilization. This is not expected net return or a hard return ceiling; closing debits, losses, fees, turnover, cash income and stock P/L change the result. The 25-30% target is unproven.",
    }


def validate_fresh_exposure(account: Any, positions: list, approved: Any) -> None:
    """Recheck real holdings under the order-journal reservation lock."""
    from data_feeds import portfolio_from_broker, _parse_occ_option_symbol

    pf = portfolio_from_broker(account, positions)
    option = _parse_occ_option_symbol(approved.symbol)
    if option is None:
        raise ValueError("Unsupported option symbol")
    underlying = str(option["underlying"])
    if approved.action == "CLOSE_SHORT_PUT":
        owned = sum(p["qty"] for p in pf["short_puts"] if p["symbol"] == approved.symbol)
        if approved.qty > owned:
            raise ValueError("POSITION_CHANGED: insufficient short puts to close")
        return
    if pf["unsupported_positions"]:
        raise ValueError("UNSUPPORTED_EXPOSURE: new risk requires manual portfolio review")
    if approved.action == "SELL_COVERED_CALL":
        shares = sum(p["shares"] for p in pf["equity_positions"] if p["ticker"] == underlying)
        if any(p["underlying"] == underlying for p in pf["short_calls"]):
            raise ValueError("POSITION_CHANGED: a short call already exists")
        if approved.qty * 100 > shares:
            raise ValueError("POSITION_CHANGED: insufficient shares for call coverage")
        return
    budget = entry_budget(pf)
    if underlying in budget["occupied_underlyings"]:
        raise ValueError("POSITION_CHANGED: underlying already has exposure")
    needed = float(option["strike"]) * 100 * approved.qty
    if needed > min(budget["per_position"], budget["remaining"]) + 0.001:
        raise ValueError("ALLOCATION_CHANGED: order exceeds fresh cash or collateral limits")
