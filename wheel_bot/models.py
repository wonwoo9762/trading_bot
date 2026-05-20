"""Pydantic schemas for every structured LLM output in the Wheel strategy.

These models are used with ``ChatOpenAI.with_structured_output()`` so the API
enforces the schema via function-calling; downstream code never needs regex.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class MacroSentinelOutput(BaseModel):
    """Macro Sentinel verdict."""

    status: Literal["CLEAR", "HALT"]
    reason: str = Field(description="Short justification")


class OrchestratorOutput(BaseModel):
    """Orchestrator routing directive."""

    route_to: Literal["CASH", "ASSET_NOMINAL", "ASSET_DISTRESSED"]
    action: str = Field(description="Human-readable rationale for the route")


class ScreenerOutput(BaseModel):
    """Fundamental Screener result."""

    approved_tickers: list[str] = Field(
        default_factory=list,
        description="Ticker symbols that pass all fundamental criteria (empty list if none qualify)",
    )


class QuantLeg(BaseModel):
    """Single leg of an options trade."""

    strike: float
    exp: str = Field(description="Expiration date, e.g. '04/15'")


class QuantOutput(BaseModel):
    """Options Quant trade ticket."""

    action: Literal["ROLL", "SPREAD", "NO_TRADE"]
    buy_to_close: Optional[QuantLeg] = None
    sell_to_open: Optional[QuantLeg] = None
    est_credit: Optional[float] = Field(
        default=None, description="Estimated net credit in dollars (per-share or total)"
    )
    reason: Optional[str] = None


class AssessorOutput(BaseModel):
    """Opportunity Cost Assessor decision."""

    decision: Literal["LIQUIDATE", "APPROVE_ROLL"]
    reason: str


class CROOutput(BaseModel):
    """Chief Risk Officer verdict."""

    status: Literal["APPROVED", "REJECTED"]
    reason: str


class BrokerOutput(BaseModel):
    """Execution Broker limit-order parameters."""

    initial_limit: Optional[float] = None
    step_down: Optional[float] = None
    floor_price: Optional[float] = None
    error: Optional[str] = Field(
        default=None,
        description="Set when spread data is missing or ticket is unexecutable",
    )
    note: Optional[str] = None
