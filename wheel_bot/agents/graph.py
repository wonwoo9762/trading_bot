"""LangGraph multi-agent flow for the Wheel options strategy.

Architecture
────────────
START → preflight → macro_sentinel → orchestrator → data_gate ─┐
  ┌────────────────────────────────────────────────────────────┘
  ├─ CASH       → candidate_selector → screener → chain → put_drafter ──┐
  ├─ NOMINAL    → nominal_ticket ──────────┤
  └─ DISTRESSED → quant → assessor → decider ──┤
                                                ↓
                              ticket_validator → CRO → broker → END
                                ↕ retry               ↕ retry / force-liq

Every LLM call uses ``with_structured_output()`` (Pydantic schemas enforced
via function-calling).  No regex JSON extraction.  Failures are fail-closed.

Persistence is supported via an optional SQLite checkpointer (pass
``thread_id`` + ``checkpoint_db`` to ``run_trading_flow``).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict, TypeVar

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel

from config import require_openai_key
from guardrails import build_agent_system, human_payload_suspicious
from models import (
    AssessorOutput,
    BrokerOutput,
    CROOutput,
    CandidateSelectorOutput,
    MacroSentinelOutput,
    OrchestratorOutput,
    QuantOutput,
    ScreenerOutput,
)
from prompts import (
    CANDIDATE_SELECTOR_PROMPT,
    CHIEF_RISK_OFFICER_PROMPT,
    EXECUTION_BROKER_PROMPT,
    FUNDAMENTAL_SCREENER_PROMPT,
    MACRO_SENTINEL_PROMPT,
    OPPORTUNITY_COST_ASSESSOR_PROMPT,
    OPTIONS_QUANT_PROMPT,
    ORCHESTRATOR_PROMPT,
)

try:
    from langgraph.checkpoint.sqlite import SqliteSaver as _SqliteSaver
except ImportError:
    _SqliteSaver = None

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

CRO_REJECT_MAX = 3
TICKET_VALIDATION_MAX = 3
MAX_POSITION_PCT = 0.20 

_T = TypeVar("_T", bound=BaseModel)


# ── State ──────────────────────────────────────────────────────────────────

class WheelState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]

    # Inputs (immutable after START)
    portfolio_state: str
    macro_input: str
    candidate_universe_input: str
    fundamentals_input: str
    options_chain_input: str
    liquidation_input: str

    # Routing / control
    active_path: str           # "cash" | "nominal" | "distressed"
    route_to: str              # "CASH" | "ASSET_NOMINAL" | "ASSET_DISTRESSED"
    cro_retries: int
    validation_retries: int
    forced_liq_attempts: int
    last_cro_reason: str

    # Gates
    data_gate_status: str      # "ok" | "blocked"
    data_gate_reason: str
    ticket_validation_status: str   # "valid" | "invalid"
    ticket_validation_reason: str

    # Outputs (serialised Pydantic JSON or abort message)
    macro_output: str
    orchestrator_output: str
    candidate_selector_output: str
    screener_output: str
    quant_output: str
    opportunity_output: str
    cro_output: str
    execution_output: str
    abort_reason: str

    draft_ticket: str
    ticket_source: str


# ── LLM helpers ────────────────────────────────────────────────────────────

_LLM: ChatOpenAI | None = None


def get_llm() -> ChatOpenAI:
    """Singleton LLM instance; validates key on first call."""
    global _LLM
    require_openai_key()
    if _LLM is None:
        _LLM = ChatOpenAI(model="gpt-5-mini", temperature=0)
    return _LLM


def _invoke_structured(
    schema: type[_T],
    role_prompt: str,
    human_content: str,
) -> tuple[AIMessage | None, _T | None]:
    """Call the LLM with structured-output enforcement.

    Returns ``(raw_message, parsed_model)``.  Either may be ``None`` on
    failure — callers **must** apply a fail-safe default when ``parsed``
    is ``None``.
    """
    structured_llm = get_llm().with_structured_output(
        schema, include_raw=True
    )
    try:
        result = structured_llm.invoke(
            [
                SystemMessage(
                    content=build_agent_system(role_prompt, structured=True)
                ),
                HumanMessage(content=human_content),
            ]
        )
    except Exception:
        logger.exception(
            "Structured LLM call failed for schema=%s", schema.__name__
        )
        return None, None

    raw: AIMessage | None = result.get("raw")
    parsed: _T | None = result.get("parsed")
    err = result.get("parsing_error")

    if err is not None or parsed is None:
        logger.warning(
            "Structured output parse failed for schema=%s  error=%s",
            schema.__name__,
            err,
        )
        return raw, None
    return raw, parsed


# ── Utility ────────────────────────────────────────────────────────────────

def _derive_route_from_portfolio(portfolio_state: str) -> str | None:
    """Deterministic routing fallback from raw portfolio JSON."""
    try:
        data = json.loads(portfolio_state)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        cash = float(data.get("cash") or 0)
        shares = float(data.get("shares") or 0)
        spot = float(data.get("spot") or 0)
        cost = float(data.get("cost_basis") or 0)
    except (TypeError, ValueError):
        return None
    if shares <= 0 and cash > 0:
        return "CASH"
    if shares > 0 and cost > 0 and spot < cost * 0.95:
        return "ASSET_DISTRESSED"
    if shares > 0:
        return "ASSET_NOMINAL"
    return "CASH"


_PATH_MAP = {
    "CASH": "cash",
    "ASSET_NOMINAL": "nominal",
    "ASSET_DISTRESSED": "distressed",
}


def _parse_portfolio(raw: str) -> dict[str, float] | None:
    """Extract numeric fields from portfolio JSON.  Returns None on failure."""
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        cash = float(data.get("cash") or 0)
        shares = float(data.get("shares") or 0)
        spot = float(data.get("spot") or 0)
        cost_basis = float(data.get("cost_basis") or 0)
    except (TypeError, ValueError):
        return None
    position_value = shares * spot
    nlv = cash + position_value
    max_deploy = MAX_POSITION_PCT * nlv if nlv > 0 else 0
    return {
        "cash": cash,
        "shares": shares,
        "spot": spot,
        "cost_basis": cost_basis,
        "position_value": position_value,
        "nlv": nlv,
        "max_deploy": max_deploy,
        "position_pct": (position_value / nlv * 100) if nlv > 0 else 0,
    }


def _parse_option_chain_contracts(options_chain: str) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    for line in (options_chain or "").splitlines():
        kind = re.search(r"\[(Call|Put)\s+([0-9.]+)", line, re.IGNORECASE)
        symbol = re.search(r"Symbol:\s+([A-Z0-9]+)", line)
        if not kind or not symbol:
            continue

        def _num(label: str) -> float | None:
            match = re.search(label + r":\s*(-?[0-9.]+)%?", line, re.IGNORECASE)
            if not match:
                return None
            try:
                return float(match.group(1))
            except ValueError:
                return None

        exp = re.search(r"Exp\s+([0-9A-Za-z_.\-/]+)", line)
        try:
            strike = float(kind.group(2))
        except ValueError:
            continue

        contracts.append(
            {
                "type": kind.group(1).lower(),
                "strike": strike,
                "expiration": exp.group(1) if exp else None,
                "symbol": symbol.group(1),
                "bid": _num("Bid"),
                "ask": _num("Ask"),
                "delta": _num("Delta"),
                "pop": _num("POP"),
            }
        )
    return contracts


def _select_cash_secured_put_contract(
    options_chain: str, max_risk: float
) -> tuple[dict[str, Any] | None, str]:
    contracts = _parse_option_chain_contracts(options_chain)
    if not contracts:
        return None, "options chain contained no parseable contracts"

    puts = [c for c in contracts if c.get("type") == "put"]
    if not puts:
        return None, "options chain contained no put contracts"

    eligible: list[dict[str, Any]] = []
    for c in puts:
        bid = c.get("bid")
        ask = c.get("ask")
        pop = c.get("pop")
        strike = c.get("strike")
        if bid is None or ask is None or pop is None or strike is None:
            continue
        if float(pop) <= 70:
            continue
        if float(strike) * 100 > max_risk:
            continue
        candidate = dict(c)
        candidate["mid"] = round((float(bid) + float(ask)) / 2, 2)
        eligible.append(candidate)

    if not eligible:
        return (
            None,
            "no put contract had Bid/Ask, POP > 70%, and strike x 100 within max_risk",
        )

    selected = max(
        eligible,
        key=lambda c: (float(c.get("bid") or 0), float(c.get("pop") or 0)),
    )
    return selected, "selected highest-bid put that passed POP and max-risk filters"


# ── Node functions (in execution order) ───────────────────────────────────

def preflight_node(state: WheelState) -> WheelState:
    """Block obvious prompt-injection before any LLM work."""
    blob = "\n".join(
        str(state.get(k) or "")
        for k in (
            "portfolio_state",
            "macro_input",
            "candidate_universe_input",
            "fundamentals_input",
            "options_chain_input",
            "liquidation_input",
        )
    )
    if human_payload_suspicious(blob):
        return {
            "abort_reason": (
                "PREFLIGHT: Heuristic prompt-injection match.  "
                "Agents were NOT invoked.  Sanitize inputs and retry."
            ),
        }
    return {}


def macro_sentinel_node(state: WheelState) -> WheelState:
    macro_in = (state.get("macro_input") or "").strip() or "NO_MACRO_DATA_PROVIDED"
    raw, parsed = _invoke_structured(
        MacroSentinelOutput,
        MACRO_SENTINEL_PROMPT,
        f"Input: {macro_in}",
    )
    if parsed is None:
        parsed = MacroSentinelOutput(
            status="HALT", reason="LLM_FAILURE_FAIL_CLOSED"
        )
    out: WheelState = {"macro_output": parsed.model_dump_json()}
    if raw is not None:
        out["messages"] = [raw]
    return out


def orchestrator_node(state: WheelState) -> WheelState:
    raw, parsed = _invoke_structured(
        OrchestratorOutput,
        ORCHESTRATOR_PROMPT,
        f"Input: {state.get('portfolio_state', '')}",
    )
    if parsed is None:
        route = (
            _derive_route_from_portfolio(state.get("portfolio_state", "") or "")
            or "CASH"
        )
        parsed = OrchestratorOutput(
            route_to=route,  # type: ignore[arg-type]
            action="DETERMINISTIC_FALLBACK: structured output failed",
        )

    active_path = _PATH_MAP.get(parsed.route_to, "cash")

    out: WheelState = {
        "orchestrator_output": parsed.model_dump_json(),
        "route_to": parsed.route_to,
        "active_path": active_path,
        "cro_retries": int(state.get("cro_retries", 0) or 0),
    }
    if raw is not None:
        out["messages"] = [raw]
    return out


def data_gate_node(state: WheelState) -> WheelState:
    """Require path-specific inputs before spending tokens on drafters."""
    route = (state.get("route_to") or "").upper()
    reasons: list[str] = []

    if not (state.get("portfolio_state") or "").strip():
        reasons.append("portfolio_state is empty")
    if not (state.get("macro_input") or "").strip():
        reasons.append("macro_input is empty")

    if route == "CASH":
        if not (state.get("candidate_universe_input") or state.get("fundamentals_input") or "").strip():
            reasons.append("candidate_universe_input required for CASH path")
        if not (state.get("fundamentals_input") or "").strip():
            reasons.append("fundamentals_input required for CASH path")
    elif route == "ASSET_NOMINAL":
        if not (state.get("options_chain_input") or "").strip():
            reasons.append("options_chain_input required for NOMINAL path")
    elif route == "ASSET_DISTRESSED":
        if not (state.get("options_chain_input") or "").strip():
            reasons.append("options_chain_input required for DISTRESSED (quant)")
        if not (state.get("liquidation_input") or "").strip():
            reasons.append("liquidation_input required for DISTRESSED (assessor)")

    if reasons:
        return {
            "data_gate_status": "blocked",
            "data_gate_reason": "; ".join(reasons),
        }
    return {"data_gate_status": "ok", "data_gate_reason": ""}


def data_blocked_node(state: WheelState) -> WheelState:
    return {
        "abort_reason": f"DATA_GATE: {state.get('data_gate_reason', 'unknown')}"
    }


def _load_json_list(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _filter_json_rows_by_tickers(raw: str, tickers: list[str]) -> str:
    wanted = {t.upper() for t in tickers}
    rows = _load_json_list(raw)
    if not rows or not wanted:
        return raw
    filtered = [row for row in rows if str(row.get("ticker", "")).upper() in wanted]
    return json.dumps(filtered)


def _deterministic_candidate_fallback(raw: str) -> CandidateSelectorOutput:
    rows = _load_json_list(raw)
    selected: list[str] = []
    for row in rows:
        try:
            ticker = str(row.get("ticker", "")).upper()
            fcf = float(row.get("fcf") or 0)
            dte = float(row.get("dte") or 999)
            mkt_cap = float(row.get("mkt_cap") or 0)
        except (TypeError, ValueError):
            continue
        if ticker and fcf > 0 and dte < 1.5 and mkt_cap > 50_000_000_000:
            selected.append(ticker)
        if len(selected) >= 5:
            break
    return CandidateSelectorOutput(
        selected_tickers=selected,
        reason="DETERMINISTIC_FALLBACK_STATIC_UNIVERSE",
    )


def candidate_selector_node(state: WheelState) -> WheelState:
    """Pick CASH-path candidates from local/news/fundamental universe input."""
    universe = (
        state.get("candidate_universe_input")
        or state.get("fundamentals_input")
        or "[]"
    )
    human = (
        f"Macro context: {state.get('macro_input', '')}\n"
        f"Candidate universe: {universe}"
    )
    raw, parsed = _invoke_structured(
        CandidateSelectorOutput,
        CANDIDATE_SELECTOR_PROMPT,
        human,
    )
    if parsed is None:
        parsed = _deterministic_candidate_fallback(universe)

    out: WheelState = {"candidate_selector_output": parsed.model_dump_json()}
    if raw is not None:
        out["messages"] = [raw]
    return out


def fundamental_screener_node(state: WheelState) -> WheelState:
    cro_feedback = (state.get("last_cro_reason") or "").strip()
    fundamentals = state.get("fundamentals_input", "") or "[]"

    try:
        selector = CandidateSelectorOutput.model_validate_json(
            state.get("candidate_selector_output", "") or "{}"
        )
        if selector.selected_tickers:
            fundamentals = _filter_json_rows_by_tickers(
                fundamentals, selector.selected_tickers
            )
    except Exception:
        pass

    human = f"Input: {fundamentals}"
    if cro_feedback:
        human += (
            f"\nPrevious CRO rejection (exclude problematic tickers): "
            f"{cro_feedback}"
        )

    raw, parsed = _invoke_structured(
        ScreenerOutput, FUNDAMENTAL_SCREENER_PROMPT, human
    )
    if parsed is None:
        parsed = ScreenerOutput(approved_tickers=[])

    out: WheelState = {"screener_output": parsed.model_dump_json()}
    if raw is not None:
        out["messages"] = [raw]
    return out


def cash_options_chain_node(state: WheelState) -> WheelState:
    """Fetch options chain after the screener chooses a CASH-path ticker."""
    raw_screener = (state.get("screener_output") or "").strip()
    try:
        screener = ScreenerOutput.model_validate_json(raw_screener)
    except Exception:
        return {}
    if not screener.approved_tickers:
        return {}

    ticker = screener.approved_tickers[0]
    try:
        from data_feeds import fetch_options_chain

        chain = fetch_options_chain(ticker, contract_type="put")
    except Exception as exc:
        logger.exception("Failed to fetch cash-path options chain for %s", ticker)
        chain = f"OPTIONS_CHAIN_ERROR: {exc}"
    return {"options_chain_input": chain}


def put_drafter_node(state: WheelState) -> WheelState:
    """Draft a cash-secured put ticket from the screener's approved list.

    This node fixes a structural defect in the previous design where the
    screener's ticker list was sent directly to the CRO, which expects a
    trade ticket with action/ticker/risk fields.
    """
    raw_screener = (state.get("screener_output") or "").strip()

    if not raw_screener:
        return {
            "draft_ticket": json.dumps(
                {"action": "NO_TRADE", "reason": "Screener produced no output"}
            ),
            "ticket_source": "PUT_DRAFTER",
        }

    try:
        screener = ScreenerOutput.model_validate_json(raw_screener)
    except Exception:
        return {
            "draft_ticket": json.dumps(
                {
                    "action": "NO_TRADE",
                    "reason": "Screener output failed Pydantic validation",
                }
            ),
            "ticket_source": "PUT_DRAFTER",
        }

    if not screener.approved_tickers:
        return {
            "draft_ticket": json.dumps(
                {
                    "action": "NO_TRADE",
                    "reason": "No tickers passed fundamental criteria",
                }
            ),
            "ticket_source": "PUT_DRAFTER",
        }

    pf = _parse_portfolio(state.get("portfolio_state", "") or "")
    if pf is None:
        return {
            "draft_ticket": json.dumps(
                {"action": "NO_TRADE", "reason": "Cannot parse portfolio for sizing"}
            ),
            "ticket_source": "PUT_DRAFTER",
        }

    max_deploy = pf["max_deploy"]
    if max_deploy <= 0:
        return {
            "draft_ticket": json.dumps(
                {"action": "NO_TRADE", "reason": "No deployable capital (NLV <= 0)"}
            ),
            "ticket_source": "PUT_DRAFTER",
        }

    ticker = screener.approved_tickers[0]
    selected_contract, selection_reason = _select_cash_secured_put_contract(
        state.get("options_chain_input", ""),
        max_deploy,
    )
    if selected_contract is None:
        return {
            "draft_ticket": json.dumps(
                {
                    "action": "NO_TRADE",
                    "reason": (
                        "No executable cash-secured put candidate: "
                        f"{selection_reason}"
                    ),
                    "ticker": ticker,
                }
            ),
            "ticket_source": "PUT_DRAFTER",
        }

    ticket = {
        "action": "SELL_CSP",
        "ticker": ticker,
        "symbol": selected_contract["symbol"],
        "contract_symbol": selected_contract["symbol"],
        "option_type": "put",
        "strike": selected_contract["strike"],
        "expiration": selected_contract.get("expiration"),
        "bid": selected_contract.get("bid"),
        "ask": selected_contract.get("ask"),
        "mid": selected_contract.get("mid"),
        "delta": selected_contract.get("delta"),
        "pop": selected_contract.get("pop"),
        "pop_source": "delta_proxy_from_alpaca_option_snapshot",
        "selection_reason": selection_reason,
        "max_risk": round(max_deploy, 2),
        "nlv": round(pf["nlv"], 2),
        "max_position_pct": MAX_POSITION_PCT * 100,
        "portfolio_state": state.get("portfolio_state", ""),
        "options_chain": state.get("options_chain_input", ""),
    }
    return {
        "draft_ticket": json.dumps(ticket),
        "ticket_source": "PUT_DRAFTER",
    }


def nominal_ticket_node(state: WheelState) -> WheelState:
    """Deterministic covered-call ticket draft (no LLM call).

    Covered calls are risk-reducing on an existing position.  We embed
    ``risk_reducing: true`` and ``position_pct`` so the CRO can skip the
    concentration check.
    """
    pf = _parse_portfolio(state.get("portfolio_state", "") or "")
    ticket: dict[str, Any] = {
        "action": "SELL_COVERED_CALL",
        "risk_reducing": True,
        "portfolio_state": state.get("portfolio_state", ""),
        "options_chain": state.get("options_chain_input", ""),
    }
    if pf is not None:
        ticket["position_pct"] = round(pf["position_pct"], 1)
        ticket["nlv"] = round(pf["nlv"], 2)
    return {
        "draft_ticket": json.dumps(ticket),
        "ticket_source": "ASSET_NOMINAL_DRAFTER",
    }


def distressed_fork_node(_: WheelState) -> WheelState:
    """Stable entry-point for the distressed pipeline (retry target)."""
    return {}


def options_quant_node(state: WheelState) -> WheelState:
    cro_feedback = (state.get("last_cro_reason") or "").strip()
    human = (
        f"Input State: {state.get('portfolio_state', '')}\n"
        f"Chain Data: {state.get('options_chain_input', '')}"
    )
    if cro_feedback:
        human += f"\nCRO feedback: {cro_feedback}"

    raw, parsed = _invoke_structured(QuantOutput, OPTIONS_QUANT_PROMPT, human)
    if parsed is None:
        parsed = QuantOutput(action="NO_TRADE", reason="LLM_FAILURE_FAIL_CLOSED")

    out: WheelState = {"quant_output": parsed.model_dump_json()}
    if raw is not None:
        out["messages"] = [raw]
    return out


def opportunity_cost_node(state: WheelState) -> WheelState:
    human = (
        f"Quant proposed: {state.get('quant_output', '')}\n"
        f"Liquidation snapshot: {state.get('liquidation_input', '')}"
    )
    raw, parsed = _invoke_structured(
        AssessorOutput, OPPORTUNITY_COST_ASSESSOR_PROMPT, human
    )
    if parsed is None:
        parsed = AssessorOutput(
            decision="LIQUIDATE", reason="LLM_FAILURE_FAIL_CLOSED"
        )

    out: WheelState = {"opportunity_output": parsed.model_dump_json()}
    if raw is not None:
        out["messages"] = [raw]
    return out


def distressed_decider_node(state: WheelState) -> WheelState:
    """Merge quant + assessor outputs into a single ``draft_ticket``."""
    raw_assessor = (state.get("opportunity_output") or "").strip()
    raw_quant = (state.get("quant_output") or "").strip()

    try:
        assessor = AssessorOutput.model_validate_json(raw_assessor)
    except Exception:
        assessor = None

    # LIQUIDATE (or unparseable → fail-closed to liquidation)
    if assessor is None or assessor.decision == "LIQUIDATE":
        reason = (
            assessor.reason
            if assessor
            else "Assessor output not parseable; fail-closed"
        )
        ticket = {"action": "LIQUIDATE", "reason": reason}
        return {
            "draft_ticket": json.dumps(ticket),
            "ticket_source": "OPPORTUNITY_COST_ASSESSOR",
        }

    # APPROVE_ROLL → forward the quant ticket
    if raw_quant:
        try:
            quant = QuantOutput.model_validate_json(raw_quant)
            if quant.action == "NO_TRADE":
                ticket = {
                    "action": "LIQUIDATE",
                    "reason": "Quant produced NO_TRADE; cannot roll",
                }
                return {
                    "draft_ticket": json.dumps(ticket),
                    "ticket_source": "DISTRESSED_FAILCLOSED",
                }
            return {
                "draft_ticket": raw_quant,
                "ticket_source": "OPTIONS_QUANT",
            }
        except Exception:
            pass

    ticket = {
        "action": "LIQUIDATE",
        "reason": "Quant output not parseable; fail-closed",
    }
    return {
        "draft_ticket": json.dumps(ticket),
        "ticket_source": "DISTRESSED_FAILCLOSED",
    }


def ticket_validator_node(state: WheelState) -> WheelState:
    """Structural validation of ``draft_ticket``.  Does not count as CRO retry."""
    raw = (state.get("draft_ticket") or "").strip()
    source = (state.get("ticket_source") or "").upper()
    vr = int(state.get("validation_retries", 0) or 0)

    def _invalid(reason: str) -> WheelState:
        return {
            "ticket_validation_status": "invalid",
            "ticket_validation_reason": reason,
            "validation_retries": vr + 1,
        }

    def _valid() -> WheelState:
        return {
            "ticket_validation_status": "valid",
            "ticket_validation_reason": "",
            "validation_retries": 0,
        }

    if not raw:
        return _invalid("draft_ticket is empty")

    try:
        ticket = json.loads(raw)
    except json.JSONDecodeError:
        return _invalid("draft_ticket is not valid JSON")

    if not isinstance(ticket, dict):
        return _invalid("draft_ticket must be a JSON object")

    action = str(ticket.get("action", "")).upper()

    if action == "LIQUIDATE":
        return _valid()

    if source == "PUT_DRAFTER":
        if action != "SELL_CSP":
            return _invalid(
                f"PUT_DRAFTER ticket has unexpected action: {action}"
            )
        max_risk = ticket.get("max_risk")
        if max_risk is None or max_risk <= 0:
            return _invalid(
                "SELL_CSP ticket missing valid max_risk (concentration cap)"
            )
        return _valid()

    if source == "ASSET_NOMINAL_DRAFTER":
        if action == "SELL_COVERED_CALL":
            return _valid()
        return _invalid(
            f"nominal ticket must have action SELL_COVERED_CALL, got: {action}"
        )

    if source in {
        "OPTIONS_QUANT",
        "OPPORTUNITY_COST_ASSESSOR",
        "DISTRESSED_FAILCLOSED",
        "FAILSAFE_LIQUIDATION",
    }:
        if not action:
            return _invalid("trade ticket missing action field")
        return _valid()

    if not action:
        return _invalid(f"unknown source '{source}' and no action field")
    return _valid()


def validation_abort_node(state: WheelState) -> WheelState:
    return {
        "abort_reason": (
            f"TICKET_VALIDATION: exceeded {TICKET_VALIDATION_MAX} retries.  "
            f"Last: {state.get('ticket_validation_reason', '')}"
        ),
    }


def chief_risk_officer_node(state: WheelState) -> WheelState:
    raw, parsed = _invoke_structured(
        CROOutput,
        CHIEF_RISK_OFFICER_PROMPT,
        f"Input: {state.get('draft_ticket', '')}",
    )
    if parsed is None:
        parsed = CROOutput(
            status="REJECTED", reason="CRO_LLM_FAILURE_FAIL_CLOSED"
        )

    retries = int(state.get("cro_retries", 0) or 0)
    if parsed.status == "REJECTED":
        retries += 1

    out: WheelState = {
        "cro_output": parsed.model_dump_json(),
        "cro_retries": retries,
        "last_cro_reason": parsed.reason,
    }
    if raw is not None:
        out["messages"] = [raw]
    return out


def force_liquidation_node(state: WheelState) -> WheelState:
    fl = int(state.get("forced_liq_attempts", 0) or 0) + 1
    ticket = {
        "action": "LIQUIDATE",
        "reason": (
            f"Forced after {CRO_REJECT_MAX} CRO rejections.  "
            f"Last: {state.get('last_cro_reason', '')}"
        ),
    }
    return {
        "draft_ticket": json.dumps(ticket),
        "ticket_source": "FAILSAFE_LIQUIDATION",
        "forced_liq_attempts": fl,
    }


def cro_rejected_abort_node(state: WheelState) -> WheelState:
    return {
        "abort_reason": (
            f"CRO_REJECTED: exceeded {CRO_REJECT_MAX} CASH-path retries.  "
            "No position exists to liquidate; no transaction will be made.  "
            f"Last: {state.get('last_cro_reason', '')}"
        ),
    }


def forced_abort_node(state: WheelState) -> WheelState:
    return {
        "abort_reason": (
            "FORCED_LIQUIDATION_ABORT: CRO rejected the failsafe liquidation "
            f"ticket.  Manual review required.  {state.get('last_cro_reason', '')}"
        ),
    }


def retry_router_node(_: WheelState) -> WheelState:
    """Pass-through; routing is handled by the conditional edge."""
    return {}


def execution_broker_node(state: WheelState) -> WheelState:
    chain = (state.get("options_chain_input") or "").strip()
    human = (
        f"Approved ticket: {state.get('draft_ticket', '')}\n"
        f"Options chain context: {chain}"
    )
    raw, parsed = _invoke_structured(
        BrokerOutput, EXECUTION_BROKER_PROMPT, human
    )
    if parsed is None:
        parsed = BrokerOutput(
            error="LLM_FAILURE",
            note="Broker call failed; manual execution required",
        )

    out: WheelState = {"execution_output": parsed.model_dump_json()}
    if raw is not None:
        out["messages"] = [raw]
    return out


# ── Routing functions ──────────────────────────────────────────────────────

def _route_after_preflight(state: WheelState) -> str:
    return "abort" if (state.get("abort_reason") or "").strip() else "continue"


def _route_after_macro(state: WheelState) -> str:
    """Fail-closed: only proceed on explicit CLEAR; anything else → HALT."""
    raw = state.get("macro_output") or ""
    try:
        parsed = MacroSentinelOutput.model_validate_json(raw)
        if parsed.status == "CLEAR":
            return "clear"
    except Exception:
        pass
    return "halt"


def _route_after_data_gate(state: WheelState) -> str:
    if state.get("data_gate_status") == "blocked":
        return "blocked"
    route = (state.get("route_to") or "").upper()
    if route == "ASSET_DISTRESSED":
        return "distressed"
    if route == "ASSET_NOMINAL":
        return "nominal"
    return "cash"


def _route_after_put_drafter(state: WheelState) -> str:
    """Short-circuit to END when no viable CSP ticket was drafted."""
    raw = (state.get("draft_ticket") or "").strip()
    try:
        t = json.loads(raw)
        if isinstance(t, dict) and t.get("action") == "NO_TRADE":
            return "no_trade"
    except Exception:
        return "no_trade"
    return "continue"


def _route_after_ticket_validation(state: WheelState) -> str:
    if state.get("ticket_validation_status") == "valid":
        return "valid"
    if int(state.get("validation_retries", 0) or 0) >= TICKET_VALIDATION_MAX:
        return "give_up"
    path = (state.get("active_path") or "cash").lower()
    if path == "distressed":
        return "redraft_distressed"
    if path == "nominal":
        return "redraft_nominal"
    return "redraft_cash"


def _route_after_cro(state: WheelState) -> str:
    raw = state.get("cro_output") or ""
    try:
        parsed = CROOutput.model_validate_json(raw)
        if parsed.status == "APPROVED":
            return "approved"
    except Exception:
        pass  # unparseable = REJECTED path

    if (state.get("ticket_source") or "").upper() == "FAILSAFE_LIQUIDATION":
        return "abort_forced"

    if int(state.get("cro_retries", 0) or 0) >= CRO_REJECT_MAX:
        if (state.get("active_path") or "").lower() == "cash":
            return "abort_rejected"
        return "force_liquidation"
    return "rejected"


def _route_retry_target(state: WheelState) -> str:
    src = (state.get("ticket_source") or "").upper()
    if src in {"PUT_DRAFTER", "FUNDAMENTAL_SCREENER"}:
        return "fundamental_screener"
    if src == "ASSET_NOMINAL_DRAFTER":
        return "nominal_ticket"
    if src in {
        "OPTIONS_QUANT",
        "OPPORTUNITY_COST_ASSESSOR",
        "DISTRESSED_FAILCLOSED",
    }:
        return "distressed_fork"
    return "__end__"


# ── Graph construction ─────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    g = StateGraph(WheelState)

    # ---- Nodes ----
    g.add_node("preflight", preflight_node)
    g.add_node("macro_sentinel", macro_sentinel_node)
    g.add_node("orchestrator", orchestrator_node)
    g.add_node("data_gate", data_gate_node)
    g.add_node("data_blocked", data_blocked_node)

    g.add_node("candidate_selector", candidate_selector_node)
    g.add_node("fundamental_screener", fundamental_screener_node)
    g.add_node("cash_options_chain", cash_options_chain_node)
    g.add_node("put_drafter", put_drafter_node)
    g.add_node("nominal_ticket", nominal_ticket_node)

    g.add_node("distressed_fork", distressed_fork_node)
    g.add_node("options_quant", options_quant_node)
    g.add_node("opportunity_cost", opportunity_cost_node)
    g.add_node("distressed_decider", distressed_decider_node)

    g.add_node("ticket_validator", ticket_validator_node)
    g.add_node("validation_abort", validation_abort_node)

    g.add_node("chief_risk_officer", chief_risk_officer_node)
    g.add_node("retry_router", retry_router_node)
    g.add_node("force_liquidation", force_liquidation_node)
    g.add_node("cro_rejected_abort", cro_rejected_abort_node)
    g.add_node("forced_abort", forced_abort_node)
    g.add_node("execution_broker", execution_broker_node)

    # ---- Edges ----
    g.add_edge(START, "preflight")

    g.add_conditional_edges(
        "preflight",
        _route_after_preflight,
        {"abort": END, "continue": "macro_sentinel"},
    )
    g.add_conditional_edges(
        "macro_sentinel",
        _route_after_macro,
        {"halt": END, "clear": "orchestrator"},
    )

    g.add_edge("orchestrator", "data_gate")

    g.add_conditional_edges(
        "data_gate",
        _route_after_data_gate,
        {
            "blocked": "data_blocked",
            "cash": "candidate_selector",
            "nominal": "nominal_ticket",
            "distressed": "distressed_fork",
        },
    )
    g.add_edge("data_blocked", END)

    # Cash path: candidate_selector → screener → chain → put_drafter
    g.add_edge("candidate_selector", "fundamental_screener")
    g.add_edge("fundamental_screener", "cash_options_chain")
    g.add_edge("cash_options_chain", "put_drafter")
    g.add_conditional_edges(
        "put_drafter",
        _route_after_put_drafter,
        {"no_trade": END, "continue": "ticket_validator"},
    )

    # Nominal path
    g.add_edge("nominal_ticket", "ticket_validator")

    # Distressed path (sequential to prevent concurrent state writes)
    g.add_edge("distressed_fork", "options_quant")
    g.add_edge("options_quant", "opportunity_cost")
    g.add_edge("opportunity_cost", "distressed_decider")
    g.add_edge("distressed_decider", "ticket_validator")

    # Ticket validation
    g.add_conditional_edges(
        "ticket_validator",
        _route_after_ticket_validation,
        {
            "valid": "chief_risk_officer",
            "give_up": "validation_abort",
            "redraft_cash": "fundamental_screener",
            "redraft_nominal": "nominal_ticket",
            "redraft_distressed": "distressed_fork",
        },
    )
    g.add_edge("validation_abort", END)

    # CRO
    g.add_conditional_edges(
        "chief_risk_officer",
        _route_after_cro,
        {
            "approved": "execution_broker",
            "rejected": "retry_router",
            "force_liquidation": "force_liquidation",
            "abort_rejected": "cro_rejected_abort",
            "abort_forced": "forced_abort",
        },
    )
    g.add_edge("force_liquidation", "chief_risk_officer")
    g.add_edge("cro_rejected_abort", END)
    g.add_edge("forced_abort", END)

    g.add_conditional_edges(
        "retry_router",
        _route_retry_target,
        {
            "fundamental_screener": "fundamental_screener",
            "nominal_ticket": "nominal_ticket",
            "distressed_fork": "distressed_fork",
            "__end__": END,
        },
    )

    # Terminal
    g.add_edge("execution_broker", END)

    return g


# ── Run entry-point ────────────────────────────────────────────────────────

def run_trading_flow(
    portfolio_state: str,
    *,
    macro_input: str = "",
    candidate_universe_input: str = "",
    fundamentals_input: str = "",
    options_chain_input: str = "",
    liquidation_input: str = "",
    thread_id: str | None = None,
    checkpoint_db: str | None = None,
) -> str:
    """Execute the full Wheel strategy graph.

    Parameters
    ----------
    thread_id : str, optional
        Enables persistence.  Each unique ``thread_id`` is a separate run.
        For a daily 9:15 AM scheduler use ``f"wheel-{date.today()}"``.
    checkpoint_db : str, optional
        Path to a SQLite database for durable checkpointing.  Falls back to
        in-memory ``MemorySaver`` if ``langgraph-checkpoint-sqlite`` is not
        installed.
    """
    return _format_result(
        run_trading_flow_state(
            portfolio_state,
            macro_input=macro_input,
            candidate_universe_input=candidate_universe_input,
            fundamentals_input=fundamentals_input,
            options_chain_input=options_chain_input,
            liquidation_input=liquidation_input,
            thread_id=thread_id,
            checkpoint_db=checkpoint_db,
        )
    )


def run_trading_flow_state(
    portfolio_state: str,
    *,
    macro_input: str = "",
    candidate_universe_input: str = "",
    fundamentals_input: str = "",
    options_chain_input: str = "",
    liquidation_input: str = "",
    thread_id: str | None = None,
    checkpoint_db: str | None = None,
) -> dict[str, Any]:
    """Execute the graph and return the raw terminal state.

    The scheduler uses this so it can pass ``draft_ticket`` and
    ``execution_output`` to the broker without scraping the formatted report.
    """
    checkpointer = None
    config: dict[str, Any] | None = None

    if thread_id:
        if checkpoint_db and _SqliteSaver is not None:
            import sqlite3

            conn = sqlite3.connect(checkpoint_db, check_same_thread=False)
            checkpointer = _SqliteSaver(conn)
        else:
            if checkpoint_db and _SqliteSaver is None:
                logger.warning(
                    "langgraph-checkpoint-sqlite not installed; "
                    "falling back to in-memory checkpointer"
                )
            from langgraph.checkpoint.memory import MemorySaver

            checkpointer = MemorySaver()
        config = {"configurable": {"thread_id": thread_id}}

    app = build_graph().compile(checkpointer=checkpointer)

    initial: WheelState = {
        "messages": [],
        "portfolio_state": portfolio_state,
        "macro_input": macro_input,
        "candidate_universe_input": candidate_universe_input,
        "fundamentals_input": fundamentals_input,
        "options_chain_input": options_chain_input,
        "liquidation_input": liquidation_input,
        "cro_retries": 0,
        "validation_retries": 0,
        "forced_liq_attempts": 0,
    }

    result = app.invoke(initial, config=config)
    return dict(result)


def _format_result(result: dict[str, Any]) -> str:
    """Human-readable summary of the graph's terminal state."""
    sections: list[str] = []

    if result.get("abort_reason"):
        sections.append("ABORT:\n" + result["abort_reason"])
    if result.get("data_gate_status") == "blocked":
        sections.append("DATA_GATE:\n" + (result.get("data_gate_reason") or ""))

    for key, label in [
        ("macro_output", "MACRO_SENTINEL"),
        ("orchestrator_output", "ORCHESTRATOR"),
        ("candidate_selector_output", "CANDIDATE_SELECTOR"),
        ("screener_output", "FUNDAMENTAL_SCREENER"),
        ("quant_output", "OPTIONS_QUANT"),
        ("opportunity_output", "OPPORTUNITY_COST_ASSESSOR"),
        ("draft_ticket", "DRAFT_TICKET"),
        ("cro_output", "CHIEF_RISK_OFFICER"),
        ("execution_output", "EXECUTION_BROKER"),
    ]:
        val = result.get(key)
        if val:
            sections.append(f"{label}:\n{val}")

    return (
        "\n\n".join(sections)
        if sections
        else "Graph completed with no captured outputs."
    )


def format_trading_flow_state(result: dict[str, Any]) -> str:
    """Public formatter for callers that need both state and report text."""
    return _format_result(result)


# ── CLI demo ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    portfolio = (
        sys.argv[1]
        if len(sys.argv) > 1
        else '{"ticker": "AAPL", "spot": 175, "cost_basis": 170, '
        '"cash": 5000, "shares": 100}'
    )
    print(
        run_trading_flow(
            portfolio,
            macro_input="VIX: 18.2. Next FOMC: 14 days. News: Tech rallies.",
            fundamentals_input=(
                '[{"ticker": "AAPL", "fcf": 25000000, '
                '"dte": 1.1, "mkt_cap": 2800000000000}]'
            ),
            options_chain_input=(
                "[Call 170 Exp 04/15 Bid: 0.10], "
                "[Call 165 Exp 05/15 Bid: 2.50]"
            ),
            liquidation_input=(
                "Liquidating today realizes a $600 loss, "
                "leaving $14,000 cash."
            ),
            thread_id=(
                f"wheel-demo-"
                f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
            ),
        )
    )
