"""Alpaca broker adapter with explicit execution gates.

No order can leave this process without ``human_approved=True``.  Live
accounts require the separate ``allow_live_trading=True`` opt-in too.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from execution_guard import DEFAULT_JOURNAL_PATH, ExecutionGuard, lookup_order
from order_policy import ApprovedOptionOrder, REPAIR_DISABLED_REASON

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderResult:
    """Immutable, serialisable record of an order attempt."""

    status: str  # SUBMITTED | RECONCILED | UNKNOWN | DRY_RUN | BLOCKED | FAILED
    order_id: str | None = None
    client_order_id: str | None = None
    reason: str = ""
    ticket: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
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

        if not isinstance(ticket, dict):
            return OrderResult(status="BLOCKED", reason="Approved ticket must be a JSON object")

        action = str(ticket.get("action", "")).upper()

        if (
            action == "SELL_CSP"
            and not self._paper
            and ticket.get("candidate_data_live") is not True
        ):
            return OrderResult(
                status="BLOCKED",
                reason=(
                    "LIVE_CANDIDATE_DATA_REQUIRED. The current fundamental/news "
                    "candidate source is static or unknown."
                ),
                ticket=ticket,
            )

        if action == "LIQUIDATE":
            return self._liquidate(ticket)
        if action in {"ROLL", "SPREAD"}:
            return OrderResult(status="BLOCKED", reason=REPAIR_DISABLED_REASON, ticket=ticket)
        if action in {
            "SELL_CSP",
            "SELL_COVERED_CALL",
            "CLOSE_SHORT_PUT",
        }:
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
            return OrderResult(status="BLOCKED", reason="Execution params must be a JSON object", ticket=ticket)
        if params.get("error"):
            return OrderResult(status="BLOCKED", reason=f"Execution broker returned error: {params['error']}", ticket=ticket)

        try:
            approved = ApprovedOptionOrder.from_ticket(ticket)
            limit_price = approved.validate_execution(params)
        except ValueError as exc:
            return OrderResult(
                status="BLOCKED",
                reason=str(exc),
                ticket={"ticket": ticket, "execution_params": params},
            )

        guard = ExecutionGuard(getattr(self, "_journal_path", DEFAULT_JOURNAL_PATH))
        try:
            from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
            from alpaca.trading.requests import LimitOrderRequest

            # Identity and quantity always come from the approved ticket.
            order_data = LimitOrderRequest(
                symbol=approved.symbol,
                qty=approved.qty,
                side=OrderSide(approved.side),
                position_intent=PositionIntent(approved.position_intent),
                time_in_force=TimeInForce.DAY,
                limit_price=float(limit_price),
            )
            client_id = guard.reserve(
                self._client, paper=self._paper, approved=approved, limit_price=limit_price
            )
            order_data.client_order_id = client_id
        except Exception as exc:
            return OrderResult(status="BLOCKED", reason=f"EXECUTION_PREFLIGHT: {exc}", ticket=ticket)

        try:
            submitted = self._client.submit_order(order_data=order_data)
            guard.record(client_id, submitted)
            order_id = self._extract_order_id(submitted)
            logger.info(
                "Submitted option order %s %s x%s @ %s (paper=%s)",
                approved.side,
                approved.symbol,
                approved.qty,
                limit_price,
                self._paper,
            )
            return OrderResult(
                status="SUBMITTED",
                order_id=order_id,
                client_order_id=client_id,
                reason="Option limit order submitted; fill not confirmed.",
                ticket={"ticket": ticket, "execution_params": params},
            )
        except Exception as exc:
            # A timeout can occur after Alpaca accepted the order. Look it up
            # with the same ID; never mint another ID or submit again here.
            try:
                existing = lookup_order(self._client, client_id)
                if existing is not None:
                    guard.record(client_id, existing)
                    return OrderResult(
                        status="RECONCILED", order_id=self._extract_order_id(existing),
                        client_order_id=client_id,
                        reason="Submission response failed, but Alpaca has this order. No resubmission made; inspect its broker status.",
                        ticket={"ticket": ticket, "execution_params": params},
                    )
            except Exception:
                logger.exception("Unable to reconcile option submission")
            logger.warning("Option submission outcome uncertain: %s", client_id)
            return OrderResult(
                status="UNKNOWN",
                client_order_id=client_id,
                reason=f"Submission outcome uncertain: {exc}. Reservation retained; reconcile before retrying.",
                ticket={"ticket": ticket, "execution_params": params},
            )

    def _extract_order_id(self, submitted: Any) -> str | None:
        if isinstance(submitted, dict):
            value = submitted.get("id") or submitted.get("order_id")
            return str(value) if value else None
        value = getattr(submitted, "id", None)
        return str(value) if value else None
