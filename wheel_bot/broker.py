"""Alpaca broker adapter with explicit execution gates.

No order can leave this process without ``human_approved=True``.  Live
accounts require the separate ``allow_live_trading=True`` opt-in too.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderResult:
    """Immutable, serialisable record of an order attempt."""

    status: str  # SUBMITTED | DRY_RUN | BLOCKED | FAILED
    order_id: str | None = None
    reason: str = ""
    ticket: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "order_id": self.order_id,
            "reason": self.reason,
            "ticket": self.ticket,
        }


class WheelBroker:
    """Wraps ``alpaca-py`` ``TradingClient`` behind an explicit approval gate.

    This is the **sole** code path that can move real (or paper) money.

    Usage::

        broker = WheelBroker()            # reads keys from .env via config.py
        info = broker.review(draft, exec)  # human inspects
        result = broker.execute(draft, exec, human_approved=True)
    """

    def __init__(self, *, paper: bool | None = None) -> None:
        from config import ALPACA_PAPER_TRADE, get_alpaca_trading_client

        self._paper: bool = paper if paper is not None else ALPACA_PAPER_TRADE
        self._client = get_alpaca_trading_client(paper=self._paper)

    @property
    def paper(self) -> bool:
        return self._paper

    # ── Review (no side-effects) ──────────────────────────────────────

    def review(
        self, draft_ticket: str, execution_params: str
    ) -> dict[str, Any]:
        """Parse and display a ticket + broker params for human inspection."""
        try:
            ticket = json.loads(draft_ticket)
        except Exception:
            ticket = {"raw": draft_ticket}
        try:
            params = json.loads(execution_params)
        except Exception:
            params = {"raw": execution_params}
        return {
            "ticket": ticket,
            "execution_params": params,
            "paper": self._paper,
            "action_required": (
                "Call  broker.execute(draft_ticket, execution_params, "
                "human_approved=True)  to submit."
            ),
        }

    # ── Execute (side-effects gated on approval) ──────────────────────

    def execute(
        self,
        draft_ticket: str,
        execution_params: str,
        *,
        human_approved: bool = False,
        allow_live_trading: bool = False,
    ) -> OrderResult:
        """Submit the trade to Alpaca.  **Requires** ``human_approved=True``."""
        if not human_approved:
            return OrderResult(
                status="BLOCKED",
                reason=(
                    "HUMAN_APPROVAL_REQUIRED. "
                    "Pass human_approved=True after reviewing the ticket."
                ),
                ticket={"draft_ticket": draft_ticket},
            )

        if not self._paper and not allow_live_trading:
            return OrderResult(
                status="BLOCKED",
                reason=(
                    "LIVE_TRADING_NOT_ALLOWED. Set "
                    "WHEEL_BOT_ALLOW_LIVE_TRADING=True only after paper validation."
                ),
                ticket={"draft_ticket": draft_ticket},
            )

        try:
            ticket = json.loads(draft_ticket)
        except Exception as exc:
            return OrderResult(
                status="FAILED", reason=f"Invalid ticket JSON: {exc}"
            )

        action = str(ticket.get("action", "")).upper()

        if action == "LIQUIDATE":
            return self._liquidate(ticket)
        if action in {"SELL_CSP", "SELL_COVERED_CALL", "ROLL", "SPREAD"}:
            return self._place_options_order(ticket, execution_params)
        if action == "NO_TRADE":
            return OrderResult(
                status="DRY_RUN",
                reason="NO_TRADE action; nothing to execute.",
                ticket=ticket,
            )
        return OrderResult(
            status="FAILED",
            reason=f"Unrecognised action: {action}",
            ticket=ticket,
        )

    # ── Private helpers ───────────────────────────────────────────────

    def _resolve_symbol(self, ticket: dict[str, Any]) -> str:
        """Best-effort symbol extraction from various ticket shapes."""
        symbol = ticket.get("ticker") or ticket.get("symbol") or ""
        if symbol:
            return str(symbol)
        ps = ticket.get("portfolio_state")
        if isinstance(ps, str):
            try:
                ps = json.loads(ps)
            except Exception:
                return ""
        if isinstance(ps, dict):
            return str(ps.get("ticker", ""))
        return ""

    def _liquidate(self, ticket: dict[str, Any]) -> OrderResult:
        symbol = self._resolve_symbol(ticket)
        if not symbol:
            return OrderResult(
                status="FAILED",
                reason="No symbol found in ticket",
                ticket=ticket,
            )
        try:
            self._client.close_position(symbol)
            logger.info("Liquidated %s (paper=%s)", symbol, self._paper)
            return OrderResult(
                status="SUBMITTED",
                reason=f"Closed position {symbol}",
                ticket=ticket,
            )
        except Exception as exc:
            return OrderResult(
                status="FAILED", reason=str(exc), ticket=ticket
            )

    def _place_options_order(
        self, ticket: dict[str, Any], execution_params_json: str
    ) -> OrderResult:
        try:
            params = json.loads(execution_params_json)
        except Exception as exc:
            return OrderResult(
                status="FAILED",
                reason=f"Invalid execution params JSON: {exc}",
                ticket=ticket,
            )

        if not isinstance(params, dict):
            return OrderResult(
                status="FAILED",
                reason="Execution params must be a JSON object",
                ticket=ticket,
            )

        if params.get("error"):
            return OrderResult(
                status="BLOCKED",
                reason=f"Execution broker returned error: {params.get('error')}",
                ticket={"ticket": ticket, "execution_params": params},
            )

        if params.get("legs"):
            return self._place_multi_leg_options_order(ticket, params)

        symbol = self._resolve_option_symbol(ticket, params)
        side = self._resolve_option_side(ticket, params)
        qty = self._resolve_option_qty(ticket, params)
        limit_price = self._resolve_limit_price(params)

        missing = [
            name
            for name, value in {
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "limit_price": limit_price,
            }.items()
            if value in (None, "", 0)
        ]
        if missing:
            return OrderResult(
                status="BLOCKED",
                reason=f"Missing executable option order fields: {', '.join(missing)}",
                ticket={"ticket": ticket, "execution_params": params},
            )
        if float(limit_price) <= 0:
            return OrderResult(
                status="BLOCKED",
                reason="Simple option limit_price must be positive.",
                ticket={"ticket": ticket, "execution_params": params},
            )

        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import LimitOrderRequest

            order_data = LimitOrderRequest(
                symbol=str(symbol),
                qty=int(qty),
                side=OrderSide(str(side)),
                time_in_force=TimeInForce.DAY,
                limit_price=float(limit_price),
                client_order_id=self._client_order_id(),
            )
            submitted = self._client.submit_order(order_data=order_data)
            order_id = self._extract_order_id(submitted)
            logger.info(
                "Submitted option order %s %s x%s @ %s (paper=%s)",
                side,
                symbol,
                qty,
                limit_price,
                self._paper,
            )
            return OrderResult(
                status="SUBMITTED",
                order_id=order_id,
                reason="Option limit order submitted.",
                ticket={"ticket": ticket, "execution_params": params},
            )
        except Exception as exc:
            logger.exception("Option order submission failed")
            return OrderResult(
                status="FAILED",
                reason=str(exc),
                ticket={"ticket": ticket, "execution_params": params},
            )

    def _place_multi_leg_options_order(
        self, ticket: dict[str, Any], params: dict[str, Any]
    ) -> OrderResult:
        legs_payload = params.get("legs")
        if not isinstance(legs_payload, list) or len(legs_payload) < 2:
            return OrderResult(
                status="BLOCKED",
                reason="Multi-leg option order requires at least two legs.",
                ticket={"ticket": ticket, "execution_params": params},
            )

        qty = self._resolve_option_qty(ticket, params)
        limit_price = self._resolve_limit_price(params)
        if qty is None or limit_price is None:
            return OrderResult(
                status="BLOCKED",
                reason="Multi-leg option order missing qty or limit_price.",
                ticket={"ticket": ticket, "execution_params": params},
            )

        action = str(ticket.get("action", "")).upper()
        if action in {"ROLL", "SPREAD"} and limit_price > 0:
            limit_price = -abs(limit_price)

        try:
            from alpaca.trading.enums import (
                OrderClass,
                OrderSide,
                PositionIntent,
                TimeInForce,
            )
            from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

            legs = []
            for leg in legs_payload:
                if not isinstance(leg, dict) or not leg.get("symbol"):
                    raise ValueError("Each leg must include an option symbol")
                side_value = leg.get("side")
                intent_value = leg.get("position_intent")
                legs.append(
                    OptionLegRequest(
                        symbol=str(leg["symbol"]),
                        ratio_qty=float(leg.get("ratio_qty") or 1),
                        side=OrderSide(str(side_value)) if side_value else None,
                        position_intent=(
                            PositionIntent(str(intent_value))
                            if intent_value
                            else None
                        ),
                    )
                )

            order_data = LimitOrderRequest(
                qty=int(qty),
                order_class=OrderClass.MLEG,
                legs=legs,
                time_in_force=TimeInForce.DAY,
                limit_price=float(limit_price),
                client_order_id=self._client_order_id(),
            )
            submitted = self._client.submit_order(order_data=order_data)
            order_id = self._extract_order_id(submitted)
            logger.info(
                "Submitted multi-leg option order x%s @ %s (paper=%s)",
                qty,
                limit_price,
                self._paper,
            )
            return OrderResult(
                status="SUBMITTED",
                order_id=order_id,
                reason="Multi-leg option limit order submitted.",
                ticket={"ticket": ticket, "execution_params": params},
            )
        except Exception as exc:
            logger.exception("Multi-leg option order submission failed")
            return OrderResult(
                status="FAILED",
                reason=str(exc),
                ticket={"ticket": ticket, "execution_params": params},
            )

    def _resolve_option_symbol(
        self, ticket: dict[str, Any], params: dict[str, Any]
    ) -> str:
        for key in ("symbol", "option_symbol", "contract_symbol"):
            value = params.get(key) or ticket.get(key)
            if value:
                return str(value)
        return ""

    def _resolve_option_side(
        self, ticket: dict[str, Any], params: dict[str, Any]
    ) -> str:
        side = str(params.get("side") or ticket.get("side") or "").strip().lower()
        if side in {"buy", "sell"}:
            return side
        action = str(ticket.get("action", "")).upper()
        if action in {"SELL_CSP", "SELL_COVERED_CALL"}:
            return "sell"
        return ""

    def _resolve_option_qty(
        self, ticket: dict[str, Any], params: dict[str, Any]
    ) -> int | None:
        raw_qty = params.get("qty") or params.get("quantity") or ticket.get("qty")
        if raw_qty is None:
            raw_portfolio = ticket.get("portfolio_state")
            if isinstance(raw_portfolio, str):
                try:
                    raw_portfolio = json.loads(raw_portfolio)
                except Exception:
                    raw_portfolio = None
            if isinstance(raw_portfolio, dict):
                shares = int(float(raw_portfolio.get("shares") or 0))
                if shares >= 100:
                    return max(1, shares // 100)
            return 1
        try:
            qty = int(float(raw_qty))
        except (TypeError, ValueError):
            return None
        return qty if qty > 0 else None

    def _resolve_limit_price(self, params: dict[str, Any]) -> float | None:
        raw = params.get("limit_price")
        if raw is None:
            raw = params.get("initial_limit")
        try:
            price = float(raw)
        except (TypeError, ValueError):
            return None
        return price if price != 0 else None

    def _client_order_id(self) -> str:
        return f"wheelbot-{uuid4().hex[:24]}"

    def _extract_order_id(self, submitted: Any) -> str | None:
        if isinstance(submitted, dict):
            value = submitted.get("id") or submitted.get("order_id")
            return str(value) if value else None
        value = getattr(submitted, "id", None)
        return str(value) if value else None
