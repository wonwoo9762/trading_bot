"""Deterministic boundaries for approved single-leg option orders.

This module validates order identity and price bounds. It does not replace
portfolio risk checks, quote freshness checks, or broker reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any


SINGLE_LEG_ACTIONS = {
    "SELL_CSP": ("sell", "sell_to_open", "P"),
    "SELL_COVERED_CALL": ("sell", "sell_to_open", "C"),
    "CLOSE_SHORT_PUT": ("buy", "buy_to_close", "P"),
}
REPAIR_DISABLED_REASON = (
    "AUTOMATED_REPAIR_DISABLED: ROLL and SPREAD require manual review until "
    "positions, all legs, collateral, and maximum loss are validated in code."
)
_OPTION_SYMBOL = re.compile(r"[A-Z]{1,6}\d{6}([CP])\d{8}")
_CENT = Decimal("0.01")


def _number(value: Any, name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not number.is_finite():
        raise ValueError(f"{name} must be a finite number")
    return number


def _quantity(value: Any) -> int:
    qty = _number(value, "qty")
    if qty <= 0 or qty != qty.to_integral_value():
        raise ValueError("qty must be a positive whole number of contracts")
    return int(qty)


@dataclass(frozen=True)
class ApprovedOptionOrder:
    action: str
    symbol: str
    qty: int
    side: str
    position_intent: str
    bid: Decimal
    ask: Decimal

    @classmethod
    def from_ticket(cls, ticket: dict[str, Any]) -> ApprovedOptionOrder:
        if not isinstance(ticket, dict):
            raise ValueError("Approved ticket must be a JSON object")
        action = str(ticket.get("action") or "").upper()
        if action in {"ROLL", "SPREAD"}:
            raise ValueError(REPAIR_DISABLED_REASON)
        if action not in SINGLE_LEG_ACTIONS:
            raise ValueError(f"Unsupported automated option action: {action}")
        if ticket.get("legs") not in (None, []):
            raise ValueError("SINGLE_LEG_REQUIRED: approved ticket cannot contain legs")
        side, intent, option_type = SINGLE_LEG_ACTIONS[action]
        symbol = ticket.get("symbol") or ticket.get("contract_symbol")
        match = _OPTION_SYMBOL.fullmatch(symbol) if isinstance(symbol, str) else None
        if not match or match.group(1) != option_type:
            raise ValueError(f"Approved {action} symbol must identify a standard {option_type} option")
        for key in ("symbol", "contract_symbol", "option_symbol"):
            if ticket.get(key) is not None and ticket[key] != symbol:
                raise ValueError("Approved ticket contains conflicting contract symbols")
        qty = _quantity(ticket.get("qty"))
        bid = _number(ticket.get("bid"), "bid")
        ask = _number(ticket.get("ask"), "ask")
        if bid < 0 or ask <= 0 or ask < bid or (side == "sell" and bid == 0):
            raise ValueError("Approved bid/ask bounds are not executable")
        if ticket.get("side") not in (None, side):
            raise ValueError(f"Approved {action} ticket must use side={side}")
        if ticket.get("position_intent") not in (None, intent):
            raise ValueError(f"Approved {action} ticket must use position_intent={intent}")
        return cls(action, symbol, qty, side, intent, bid, ask)

    def validate_price(self, value: Any) -> Decimal:
        price = _number(value, "limit_price")
        if price <= 0:
            raise ValueError("Simple option limit_price must be positive")
        if not self.bid <= price <= self.ask:
            raise ValueError(f"{self.action} limit price is outside the approved bid/ask spread")
        if price * 100 != (price * 100).to_integral_value():
            raise ValueError("limit_price must use whole cents")
        return price

    def midpoint(self) -> Decimal:
        try:
            return self.validate_price(((self.bid + self.ask) / 2).quantize(_CENT, rounding=ROUND_HALF_UP))
        except DecimalException:
            raise ValueError("Approved bid/ask cannot produce a valid cent-denominated price") from None

    def validate_execution(self, params: dict[str, Any]) -> Decimal:
        if not isinstance(params, dict):
            raise ValueError("Execution params must be a JSON object")
        if params.get("error"):
            raise ValueError(f"Execution broker returned error: {params['error']}")
        if params.get("legs") not in (None, []):
            raise ValueError("SINGLE_LEG_REQUIRED: execution parameters cannot add legs")
        for key in ("symbol", "option_symbol", "contract_symbol"):
            if params.get(key) is not None and params[key] != self.symbol:
                raise ValueError(f"Execution parameters changed the CRO-approved {self.action} symbol or quantity")
        for key in ("qty", "quantity"):
            if params.get(key) is not None and _quantity(params[key]) != self.qty:
                raise ValueError(f"Execution parameters changed the CRO-approved {self.action} symbol or quantity")
        if params.get("side") not in (None, self.side):
            raise ValueError(f"{self.action} must use side={self.side}")
        if params.get("position_intent") not in (None, self.position_intent):
            raise ValueError(f"{self.action} must use position_intent={self.position_intent}")
        price = params.get("limit_price")
        if price is None:
            price = params.get("initial_limit")
        return self.validate_price(price)
