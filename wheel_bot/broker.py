"""Alpaca broker adapter with a mandatory human-approval gate.

This module is intentionally **not** called by the LangGraph flow.  It is
invoked by the scheduler or a human operator *after* reviewing the graph's
``execution_output``.  No order can leave this process without
``human_approved=True`` being explicitly passed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

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
        logger.warning(
            "Options order submission is a DRY_RUN.  "
            "Ticket=%s  Params=%s",
            ticket,
            execution_params_json,
        )
        return OrderResult(
            status="DRY_RUN",
            reason=(
                "Options order API integration pending.  "
                "Ticket logged for review."
            ),
            ticket=ticket,
        )
