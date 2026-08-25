"""LangGraph multi-agent flow for the Wheel options strategy.

Architecture
────────────
START → preflight → macro_sentinel → orchestrator → data_gate ─┐
  ┌────────────────────────────────────────────────────────────┘
  ├─ CASH       → candidate_selector → screener → chain → put_drafter ──┐
  ├─ SHORT PUT  → deterministic close/hold manager ─────────────────────┤
  ├─ NOMINAL    → nominal_ticket ──────────┤
  └─ DISTRESSED → quant → assessor → decider ──┤
                                                ↓
                              ticket_validator → CRO → broker → END
                                ↕ retry               ↕ retry / manual review

Every LLM call uses ``with_structured_output()`` (Pydantic schemas enforced
via function-calling).  No regex JSON extraction.  Failures are fail-closed.

Persistence is supported via an optional SQLite checkpointer (pass
``thread_id`` + ``checkpoint_db`` to ``run_trading_flow``).
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, timezone
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
MAX_POSITION_PCT = 0.15
MAX_TOTAL_CSP_PCT = 0.50
CSP_MIN_DTE = 7
CSP_MAX_DTE = 45
CSP_MIN_POP_PCT = 70.0
CSP_MAX_POP_PCT = 85.0
CSP_MIN_ANNUALIZED_YIELD_PCT = 20.0
CSP_MAX_ANNUALIZED_YIELD_PCT = 35.0
CSP_MIN_OPEN_INTEREST = 100
CSP_MAX_SPREAD_PCT = 20.0
SHORT_PUT_PROFIT_TAKE_PCT = 50.0
SHORT_PUT_EXPIRY_DTE = 3
SHORT_PUT_EXPIRY_PROFIT_TAKE_PCT = 20.0
CC_MIN_DTE = 7
CC_MAX_DTE = 45
CC_MIN_ABS_DELTA = 0.10
CC_MAX_ABS_DELTA = 0.35
CC_MIN_OPEN_INTEREST = 100
CC_MAX_SPREAD_PCT = 20.0

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
    active_path: str           # "cash" | "short_put" | "nominal" | "distressed"
    route_to: str              # deterministic strategy state
    cro_retries: int
    validation_retries: int
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
    short_puts = data.get("short_puts") or []
    if isinstance(short_puts, list):
        for item in short_puts:
            if not isinstance(item, dict):
                continue
            try:
                if float(item.get("qty") or 0) > 0:
                    return "SHORT_PUT_OPEN"
            except (TypeError, ValueError):
                continue
    if shares <= 0 and cash > 0:
        return "CASH"
    if shares > 0 and cost > 0 and spot < cost * 0.95:
        return "ASSET_DISTRESSED"
    if shares > 0:
        return "ASSET_NOMINAL"
    return "CASH"


_PATH_MAP = {
    "CASH": "cash",
    "SHORT_PUT_OPEN": "short_put",
    "ASSET_NOMINAL": "nominal",
    "ASSET_DISTRESSED": "distressed",
}


def _parse_portfolio(raw: str) -> dict[str, Any] | None:
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
    try:
        explicit_nlv = float(data.get("nlv") or 0)
        short_put_collateral = float(data.get("short_put_collateral") or 0)
    except (TypeError, ValueError):
        return None
    nlv = explicit_nlv if explicit_nlv > 0 else cash + position_value
    max_deploy = MAX_POSITION_PCT * nlv if nlv > 0 else 0
    max_total_deploy = MAX_TOTAL_CSP_PCT * nlv if nlv > 0 else 0

    collateral_by_underlying: dict[str, float] = {}
    normalized_short_puts: list[dict[str, Any]] = []
    short_puts = data.get("short_puts") or []
    if isinstance(short_puts, list):
        for item in short_puts:
            if not isinstance(item, dict):
                continue
            underlying = str(item.get("underlying") or "").upper()
            try:
                collateral = float(item.get("collateral") or 0)
            except (TypeError, ValueError):
                continue
            if underlying and collateral > 0:
                collateral_by_underlying[underlying] = (
                    collateral_by_underlying.get(underlying, 0) + collateral
                )
                normalized_short_puts.append(dict(item))
    return {
        "ticker": str(data.get("ticker") or "").upper(),
        "cash": cash,
        "shares": shares,
        "spot": spot,
        "cost_basis": cost_basis,
        "position_value": position_value,
        "nlv": nlv,
        "max_deploy": max_deploy,
        "max_total_deploy": max_total_deploy,
        "short_put_collateral": short_put_collateral,
        "short_put_collateral_by_underlying": collateral_by_underlying,
        "short_puts": normalized_short_puts,
        "short_calls": data.get("short_calls") or [],
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
        underlying = re.search(r"Underlying:\s+([A-Z0-9.\-]+)", line)
        try:
            strike = float(kind.group(2))
        except ValueError:
            continue

        expiration = exp.group(1) if exp else None
        dte = None
        if expiration:
            try:
                dte = (date.fromisoformat(expiration) - date.today()).days
            except ValueError:
                pass

        contracts.append(
            {
                "type": kind.group(1).lower(),
                "strike": strike,
                "expiration": expiration,
                "dte": dte,
                "underlying": underlying.group(1) if underlying else None,
                "symbol": symbol.group(1),
                "bid": _num("Bid"),
                "ask": _num("Ask"),
                "delta": _num("Delta"),
                "pop": _num("POP"),
                "iv": _num("IV"),
                "open_interest": _num("OI"),
            }
        )
    return contracts


def _selected_contract_context(options_chain: str, symbol: str) -> str:
    for line in (options_chain or "").splitlines():
        if f"Symbol: {symbol}" in line:
            return line
    return ""


def _annualized_spread_cost_pct(
    *, bid: float, ask: float, strike: float, dte: int
) -> float:
    """Annualize half the quoted spread against net cash collateral."""
    net_collateral = strike - bid
    if net_collateral <= 0 or dte <= 0:
        return 100.0
    return ((ask - bid) / 2) / net_collateral * (365 / dte) * 100


def _risk_adjusted_option_score(
    *,
    annualized_yield_pct: float,
    annualized_spread_cost_pct: float,
    abs_delta: float,
    dte: int,
    open_interest: int,
) -> tuple[float, float, float]:
    """Score premium after observable execution and near-expiry risk costs.

    The score deliberately has no preferred DTE. Shorter contracts must earn
    enough additional premium to offset their larger negative-gamma penalty.
    """
    gamma_penalty = abs_delta * 75.0 / math.sqrt(max(dte, 1))
    liquidity_bonus = min(2.0, math.log10(max(open_interest, 1)) * 0.5)
    score = (
        annualized_yield_pct
        - annualized_spread_cost_pct
        - gamma_penalty
        + liquidity_bonus
    )
    return round(score, 3), round(gamma_penalty, 3), round(liquidity_bonus, 3)


def _select_cash_secured_put_contract(
    options_chain: str, portfolio: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    contracts = _parse_option_chain_contracts(options_chain)
    if not contracts:
        return None, "options chain contained no parseable contracts"

    puts = [c for c in contracts if c.get("type") == "put"]
    if not puts:
        return None, "options chain contained no put contracts"

    nlv = float(portfolio.get("nlv") or 0)
    cash = float(portfolio.get("cash") or 0)
    existing_total = float(portfolio.get("short_put_collateral") or 0)
    existing_by_underlying = portfolio.get(
        "short_put_collateral_by_underlying", {}
    )
    if not isinstance(existing_by_underlying, dict):
        existing_by_underlying = {}
    total_remaining = min(
        max(0.0, MAX_TOTAL_CSP_PCT * nlv - existing_total),
        max(0.0, cash - existing_total),
    )

    eligible: list[dict[str, Any]] = []
    for c in puts:
        bid = c.get("bid")
        ask = c.get("ask")
        pop = c.get("pop")
        strike = c.get("strike")
        dte = c.get("dte")
        open_interest = c.get("open_interest")
        underlying = str(c.get("underlying") or "").upper()
        if (
            bid is None
            or ask is None
            or pop is None
            or strike is None
            or dte is None
            or open_interest is None
            or not underlying
        ):
            continue
        bid = float(bid)
        ask = float(ask)
        pop = float(pop)
        strike = float(strike)
        dte = int(dte)
        open_interest = int(open_interest)
        if bid <= 0 or ask < bid:
            continue
        if not CSP_MIN_DTE <= dte <= CSP_MAX_DTE:
            continue
        if not CSP_MIN_POP_PCT <= pop <= CSP_MAX_POP_PCT:
            continue
        if open_interest < CSP_MIN_OPEN_INTEREST:
            continue
        mid = (bid + ask) / 2
        spread_pct = ((ask - bid) / mid * 100) if mid > 0 else 100.0
        if spread_pct > CSP_MAX_SPREAD_PCT:
            continue
        annualized_yield = (
            bid / (strike - bid) * (365 / dte) * 100
            if strike > bid and dte > 0
            else 0
        )
        if not (
            CSP_MIN_ANNUALIZED_YIELD_PCT
            <= annualized_yield
            <= CSP_MAX_ANNUALIZED_YIELD_PCT
        ):
            continue
        abs_delta = abs(float(c.get("delta") or (1.0 - pop / 100.0)))
        annualized_spread_cost = _annualized_spread_cost_pct(
            bid=bid,
            ask=ask,
            strike=strike,
            dte=dte,
        )
        risk_adjusted_score, gamma_penalty, liquidity_bonus = (
            _risk_adjusted_option_score(
                annualized_yield_pct=annualized_yield,
                annualized_spread_cost_pct=annualized_spread_cost,
                abs_delta=abs_delta,
                dte=dte,
                open_interest=open_interest,
            )
        )
        # Do not overlap CSP cycles in one underlying. Diversify the next entry.
        if float(existing_by_underlying.get(underlying, 0) or 0) > 0:
            continue
        collateral_per_contract = strike * 100
        allowed_collateral = min(MAX_POSITION_PCT * nlv, total_remaining)
        qty = int(allowed_collateral // collateral_per_contract)
        if qty < 1:
            continue
        candidate = dict(c)
        candidate["mid"] = round(mid, 2)
        candidate["spread_pct"] = round(spread_pct, 2)
        candidate["annualized_yield_pct"] = round(annualized_yield, 2)
        candidate["annualized_spread_cost_pct"] = round(
            annualized_spread_cost, 2
        )
        candidate["gamma_risk_penalty"] = gamma_penalty
        candidate["liquidity_bonus"] = liquidity_bonus
        candidate["risk_adjusted_score"] = risk_adjusted_score
        candidate["qty"] = qty
        candidate["collateral_per_contract"] = round(
            collateral_per_contract, 2
        )
        candidate["total_collateral"] = round(
            collateral_per_contract * qty, 2
        )
        candidate["gross_premium"] = round(bid * 100 * qty, 2)
        eligible.append(candidate)

    if not eligible:
        return (
            None,
            (
                "no put contract passed 7-45 DTE, 70-85% delta-proxy POP, "
                "20-35% "
                "annualized collateral yield, OI >= 100, spread <= 20%, "
                "cash/concentration limits, and no-overlap rules"
            ),
        )

    selected = max(
        eligible,
        key=lambda c: (
            float(c["risk_adjusted_score"]),
            -float(c.get("spread_pct") or 100),
            float(c.get("open_interest") or 0),
        ),
    )
    return (
        selected,
        (
            "selected the highest risk-adjusted premium score across 7-45 DTE; "
            "the score subtracts annualized spread cost and near-expiry gamma "
            "risk, with no target expiration"
        ),
    )


def _select_covered_call_contract(
    options_chain: str, portfolio: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    """Select an executable call without selling shares below cost basis."""
    ticker = str(portfolio.get("ticker") or "").upper()
    shares = int(float(portfolio.get("shares") or 0))
    spot = float(portfolio.get("spot") or 0)
    cost_basis = float(portfolio.get("cost_basis") or 0)
    existing_calls = [
        item
        for item in (portfolio.get("short_calls") or [])
        if isinstance(item, dict)
        and str(item.get("underlying") or "").upper() == ticker
    ]
    if existing_calls:
        return None, "an open short call already exists; duplicate coverage is blocked"
    if not ticker or shares < 100 or spot <= 0:
        return None, "at least 100 shares and a valid spot price are required"

    minimum_strike = max(cost_basis, spot * 1.02)
    eligible: list[dict[str, Any]] = []
    for contract in _parse_option_chain_contracts(options_chain):
        if contract.get("type") != "call":
            continue
        if str(contract.get("underlying") or "").upper() != ticker:
            continue
        try:
            bid = float(contract["bid"])
            ask = float(contract["ask"])
            strike = float(contract["strike"])
            dte = int(contract["dte"])
            delta = abs(float(contract["delta"]))
            open_interest = int(contract["open_interest"])
        except (KeyError, TypeError, ValueError):
            continue
        if bid <= 0 or ask < bid or strike < minimum_strike:
            continue
        if not CC_MIN_DTE <= dte <= CC_MAX_DTE:
            continue
        if not CC_MIN_ABS_DELTA <= delta <= CC_MAX_ABS_DELTA:
            continue
        if open_interest < CC_MIN_OPEN_INTEREST:
            continue
        mid = (bid + ask) / 2
        spread_pct = ((ask - bid) / mid * 100) if mid > 0 else 100.0
        if spread_pct > CC_MAX_SPREAD_PCT:
            continue
        annualized_yield = bid / spot * (365 / dte) * 100
        spread_cost = ((ask - bid) / 2) / spot * (365 / dte) * 100
        score, gamma_penalty, liquidity_bonus = _risk_adjusted_option_score(
            annualized_yield_pct=annualized_yield,
            annualized_spread_cost_pct=spread_cost,
            abs_delta=delta,
            dte=dte,
            open_interest=open_interest,
        )
        candidate = dict(contract)
        candidate.update(
            {
                "mid": round(mid, 2),
                "spread_pct": round(spread_pct, 2),
                "annualized_yield_pct": round(annualized_yield, 2),
                "annualized_spread_cost_pct": round(spread_cost, 2),
                "gamma_risk_penalty": gamma_penalty,
                "liquidity_bonus": liquidity_bonus,
                "risk_adjusted_score": score,
                "qty": shares // 100,
            }
        )
        eligible.append(candidate)

    if not eligible:
        return (
            None,
            "no covered call passed cost-basis, 7-45 DTE, 0.10-0.35 delta, "
            "open-interest, and spread safeguards",
        )
    selected = max(
        eligible,
        key=lambda c: (
            float(c["risk_adjusted_score"]),
            -float(c.get("spread_pct") or 100),
            float(c.get("open_interest") or 0),
        ),
    )
    return selected, (
        "selected the highest risk-adjusted covered-call premium without "
        "targeting an expiration or selling below cost basis"
    )


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
    route = _derive_route_from_portfolio(
        state.get("portfolio_state", "") or ""
    )
    if parsed is None or route is not None:
        route = route or "CASH"
        parsed = OrchestratorOutput(
            route_to=route,  # type: ignore[arg-type]
            action=(
                "Deterministic portfolio-state routing; the LLM cannot override "
                "positions or cash state."
            ),
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
    if (
        route != "SHORT_PUT_OPEN"
        and not (state.get("macro_input") or "").strip()
    ):
        reasons.append("macro_input is empty")

    if route == "CASH":
        if not (state.get("candidate_universe_input") or state.get("fundamentals_input") or "").strip():
            reasons.append("candidate_universe_input required for CASH path")
        if not (state.get("fundamentals_input") or "").strip():
            reasons.append("fundamentals_input required for CASH path")
    elif route == "SHORT_PUT_OPEN":
        if not (state.get("options_chain_input") or "").strip():
            reasons.append("options_chain_input required to manage open short put")
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


def _candidate_data_source(raw: str, ticker: str) -> str:
    for row in _load_json_list(raw):
        if str(row.get("ticker") or "").upper() == ticker.upper():
            return str(row.get("source") or "UNKNOWN")
    return "UNKNOWN"


def _deterministic_candidate_fallback(raw: str) -> CandidateSelectorOutput:
    rows = _load_json_list(raw)
    selected: list[str] = []
    for row in rows:
        try:
            ticker = str(row.get("ticker", "")).upper()
            fcf = float(row.get("fcf") or 0)
            debt_to_equity = float(
                row.get("debt_to_equity")
                if row.get("debt_to_equity") is not None
                else row.get("dte") or 999
            )
            mkt_cap = float(row.get("mkt_cap") or 0)
        except (TypeError, ValueError):
            continue
        if (
            ticker
            and fcf > 0
            and debt_to_equity < 1.5
            and mkt_cap > 50_000_000_000
        ):
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
    """Fetch broad-DTE put chains for every approved CASH-path ticker."""
    raw_screener = (state.get("screener_output") or "").strip()
    try:
        screener = ScreenerOutput.model_validate_json(raw_screener)
    except Exception:
        return {}
    if not screener.approved_tickers:
        return {}

    from data_feeds import fetch_options_chain

    chains: list[str] = []
    for ticker in screener.approved_tickers[:5]:
        try:
            chains.append(
                fetch_options_chain(
                    ticker,
                    contract_type="put",
                    min_dte=CSP_MIN_DTE,
                    max_dte=CSP_MAX_DTE,
                )
            )
        except Exception as exc:
            logger.exception(
                "Failed to fetch cash-path options chain for %s", ticker
            )
            chains.append(f"OPTIONS_CHAIN_ERROR {ticker}: {exc}")
    return {"options_chain_input": "\n".join(chains)}


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

    selected_contract, selection_reason = _select_cash_secured_put_contract(
        state.get("options_chain_input", ""),
        pf,
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
                    "ticker": ",".join(screener.approved_tickers),
                }
            ),
            "ticket_source": "PUT_DRAFTER",
        }

    ticker = str(selected_contract["underlying"])
    candidate_data_source = _candidate_data_source(
        state.get("fundamentals_input", ""), ticker
    )
    post_trade_total_collateral = (
        pf["short_put_collateral"] + selected_contract["total_collateral"]
    )
    selected_contract_context = _selected_contract_context(
        state.get("options_chain_input", ""), selected_contract["symbol"]
    )
    ticket = {
        "action": "SELL_CSP",
        "ticker": ticker,
        "candidate_data_source": candidate_data_source,
        "candidate_data_live": candidate_data_source.startswith("LIVE_"),
        "symbol": selected_contract["symbol"],
        "contract_symbol": selected_contract["symbol"],
        "option_type": "put",
        "strike": selected_contract["strike"],
        "expiration": selected_contract.get("expiration"),
        "dte": selected_contract.get("dte"),
        "bid": selected_contract.get("bid"),
        "ask": selected_contract.get("ask"),
        "mid": selected_contract.get("mid"),
        "delta": selected_contract.get("delta"),
        "pop": selected_contract.get("pop"),
        "pop_source": "delta_proxy_from_alpaca_option_snapshot",
        "iv": selected_contract.get("iv"),
        "open_interest": selected_contract.get("open_interest"),
        "spread_pct": selected_contract.get("spread_pct"),
        "annualized_yield_pct": selected_contract.get(
            "annualized_yield_pct"
        ),
        "annualized_spread_cost_pct": selected_contract.get(
            "annualized_spread_cost_pct"
        ),
        "gamma_risk_penalty": selected_contract.get("gamma_risk_penalty"),
        "liquidity_bonus": selected_contract.get("liquidity_bonus"),
        "risk_adjusted_score": selected_contract.get("risk_adjusted_score"),
        "annualized_yield_basis": (
            "gross bid premium / net cash collateral, annualized; "
            "not a forecast of realized portfolio return"
        ),
        "qty": selected_contract.get("qty"),
        "collateral_per_contract": selected_contract.get(
            "collateral_per_contract"
        ),
        "total_collateral": selected_contract.get("total_collateral"),
        "gross_premium_at_bid": selected_contract.get("gross_premium"),
        "selection_reason": selection_reason,
        "max_risk": round(max_deploy, 2),
        "max_total_csp_risk": round(pf["max_total_deploy"], 2),
        "existing_short_put_collateral": round(
            pf["short_put_collateral"], 2
        ),
        "post_trade_total_collateral": round(
            post_trade_total_collateral, 2
        ),
        "nlv": round(pf["nlv"], 2),
        "max_position_pct": MAX_POSITION_PCT * 100,
        "portfolio_state": state.get("portfolio_state", ""),
        "options_chain": selected_contract_context,
    }
    return {
        "draft_ticket": json.dumps(ticket),
        "ticket_source": "PUT_DRAFTER",
    }


def nominal_ticket_node(state: WheelState) -> WheelState:
    """Draft a deterministic, cost-basis-aware covered-call ticket."""
    pf = _parse_portfolio(state.get("portfolio_state", "") or "")
    if pf is None:
        ticket: dict[str, Any] = {
            "action": "NO_TRADE",
            "reason": "Cannot parse portfolio for covered-call selection",
        }
    else:
        selected, reason = _select_covered_call_contract(
            state.get("options_chain_input", ""), pf
        )
        if selected is None:
            ticket = {"action": "NO_TRADE", "reason": reason}
        else:
            context = _selected_contract_context(
                state.get("options_chain_input", ""), selected["symbol"]
            )
            ticket = {
                "action": "SELL_COVERED_CALL",
                "risk_reducing": True,
                "ticker": pf["ticker"],
                "symbol": selected["symbol"],
                "contract_symbol": selected["symbol"],
                "option_type": "call",
                "strike": selected["strike"],
                "expiration": selected.get("expiration"),
                "dte": selected.get("dte"),
                "bid": selected.get("bid"),
                "ask": selected.get("ask"),
                "mid": selected.get("mid"),
                "delta": selected.get("delta"),
                "open_interest": selected.get("open_interest"),
                "spread_pct": selected.get("spread_pct"),
                "annualized_yield_pct": selected.get(
                    "annualized_yield_pct"
                ),
                "annualized_spread_cost_pct": selected.get(
                    "annualized_spread_cost_pct"
                ),
                "gamma_risk_penalty": selected.get("gamma_risk_penalty"),
                "liquidity_bonus": selected.get("liquidity_bonus"),
                "risk_adjusted_score": selected.get("risk_adjusted_score"),
                "selection_reason": reason,
                "qty": selected["qty"],
                "shares_covered": selected["qty"] * 100,
                "cost_basis": pf["cost_basis"],
                "spot": pf["spot"],
                "position_pct": round(pf["position_pct"], 1),
                "nlv": round(pf["nlv"], 2),
                "portfolio_state": state.get("portfolio_state", ""),
                "options_chain": context,
            }
    return {
        "draft_ticket": json.dumps(ticket),
        "ticket_source": "ASSET_NOMINAL_DRAFTER",
    }


def short_put_manager_node(state: WheelState) -> WheelState:
    """Close profitable short puts conservatively; otherwise hold for review."""
    pf = _parse_portfolio(state.get("portfolio_state", "") or "")
    positions = pf.get("short_puts", []) if pf else []
    contracts = {
        str(contract.get("symbol")): contract
        for contract in _parse_option_chain_contracts(
            state.get("options_chain_input", "")
        )
    }
    close_candidates: list[dict[str, Any]] = []
    observations: list[str] = []
    for position in positions:
        symbol = str(position.get("symbol") or "")
        quote = contracts.get(symbol)
        try:
            entry_credit = float(position.get("entry_credit") or 0)
            qty = int(position.get("qty") or 0)
            ask = float(quote.get("ask")) if quote else 0.0
            bid = float(quote.get("bid")) if quote else 0.0
            dte = int(
                quote.get("dte")
                if quote and quote.get("dte") is not None
                else position.get("dte")
            )
        except (TypeError, ValueError):
            observations.append(f"{symbol}: missing entry, quantity, quote, or DTE")
            continue
        if entry_credit <= 0 or qty < 1 or ask <= 0 or ask < bid:
            observations.append(f"{symbol}: quote or entry credit is not executable")
            continue
        profit_capture_pct = (entry_credit - ask) / entry_credit * 100
        regular_close = profit_capture_pct >= SHORT_PUT_PROFIT_TAKE_PCT
        expiry_close = (
            dte <= SHORT_PUT_EXPIRY_DTE
            and profit_capture_pct >= SHORT_PUT_EXPIRY_PROFIT_TAKE_PCT
        )
        observations.append(
            f"{symbol}: {profit_capture_pct:.1f}% captured with {dte} DTE"
        )
        if regular_close or expiry_close:
            close_candidates.append(
                {
                    "position": position,
                    "quote": quote,
                    "entry_credit": entry_credit,
                    "qty": qty,
                    "bid": bid,
                    "ask": ask,
                    "dte": dte,
                    "profit_capture_pct": profit_capture_pct,
                    "reason": (
                        f"captured at least {SHORT_PUT_PROFIT_TAKE_PCT:.0f}% "
                        "of maximum premium"
                        if regular_close
                        else "reduced near-expiration gamma risk after a profit"
                    ),
                }
            )

    if not close_candidates:
        return {
            "draft_ticket": json.dumps(
                {
                    "action": "NO_TRADE",
                    "reason": (
                        "Open short put remains below deterministic close thresholds; "
                        "hold or manually review threatened/losing positions. "
                        + "; ".join(observations)
                    ),
                }
            ),
            "ticket_source": "SHORT_PUT_MANAGER",
        }

    selected = max(
        close_candidates,
        key=lambda item: (item["profit_capture_pct"], -item["dte"]),
    )
    position = selected["position"]
    quote = selected["quote"]
    symbol = str(position["symbol"])
    ticket = {
        "action": "CLOSE_SHORT_PUT",
        "risk_reducing": True,
        "ticker": position.get("underlying"),
        "symbol": symbol,
        "contract_symbol": symbol,
        "side": "buy",
        "qty": selected["qty"],
        "bid": selected["bid"],
        "ask": selected["ask"],
        "mid": round((selected["bid"] + selected["ask"]) / 2, 2),
        "entry_credit": selected["entry_credit"],
        "profit_capture_pct_at_ask": round(selected["profit_capture_pct"], 2),
        "dte": selected["dte"],
        "management_reason": selected["reason"],
        "portfolio_state": state.get("portfolio_state", ""),
        "options_chain": _selected_contract_context(
            state.get("options_chain_input", ""), symbol
        ),
        "delta": quote.get("delta"),
    }
    return {
        "draft_ticket": json.dumps(ticket),
        "ticket_source": "SHORT_PUT_MANAGER",
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
            decision="MANUAL_REVIEW", reason="LLM_FAILURE_FAIL_CLOSED"
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

    # An LLM never receives authority to liquidate an existing holding.
    if assessor is None or assessor.decision == "MANUAL_REVIEW":
        reason = (
            assessor.reason
            if assessor
            else "Assessor output not parseable"
        )
        ticket = {
            "action": "NO_TRADE",
            "reason": f"Manual review required for distressed position: {reason}",
        }
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
                    "action": "NO_TRADE",
                    "reason": "Quant found no credit repair; manual review required",
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
        "action": "NO_TRADE",
        "reason": "Quant output not parseable; manual review required",
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
        required = {
            "symbol",
            "strike",
            "qty",
            "total_collateral",
            "max_risk",
            "max_total_csp_risk",
            "post_trade_total_collateral",
            "pop",
            "dte",
            "annualized_yield_pct",
            "open_interest",
            "spread_pct",
        }
        missing = sorted(key for key in required if ticket.get(key) is None)
        if missing:
            return _invalid(
                "SELL_CSP ticket missing required fields: " + ", ".join(missing)
            )
        try:
            strike = float(ticket["strike"])
            qty = int(ticket["qty"])
            total_collateral = float(ticket["total_collateral"])
            max_risk = float(ticket["max_risk"])
            max_total_risk = float(ticket["max_total_csp_risk"])
            post_trade_total = float(ticket["post_trade_total_collateral"])
            pop = float(ticket["pop"])
            dte = int(ticket["dte"])
            annualized_yield = float(ticket["annualized_yield_pct"])
            open_interest = int(ticket["open_interest"])
            spread_pct = float(ticket["spread_pct"])
        except (TypeError, ValueError):
            return _invalid("SELL_CSP ticket contains non-numeric risk fields")
        expected_collateral = strike * 100 * qty
        if qty < 1 or abs(expected_collateral - total_collateral) > 1:
            return _invalid("SELL_CSP quantity/collateral calculation is invalid")
        if total_collateral > max_risk or post_trade_total > max_total_risk:
            return _invalid("SELL_CSP exceeds concentration or total CSP cap")
        if not CSP_MIN_DTE <= dte <= CSP_MAX_DTE:
            return _invalid("SELL_CSP expiration is outside 7-45 DTE")
        if not CSP_MIN_POP_PCT <= pop <= CSP_MAX_POP_PCT:
            return _invalid("SELL_CSP POP is outside 70-85%")
        if not (
            CSP_MIN_ANNUALIZED_YIELD_PCT
            <= annualized_yield
            <= CSP_MAX_ANNUALIZED_YIELD_PCT
        ):
            return _invalid(
                "SELL_CSP annualized collateral yield is outside 20-35%"
            )
        if open_interest < CSP_MIN_OPEN_INTEREST:
            return _invalid("SELL_CSP open interest is below 100")
        if spread_pct > CSP_MAX_SPREAD_PCT:
            return _invalid("SELL_CSP bid/ask spread exceeds 20% of midpoint")
        return _valid()

    if source == "ASSET_NOMINAL_DRAFTER":
        if action != "SELL_COVERED_CALL":
            return _invalid(
                f"nominal ticket must have action SELL_COVERED_CALL, got: {action}"
            )
        required = {
            "symbol", "strike", "qty", "bid", "ask", "dte", "delta",
            "open_interest", "spread_pct", "cost_basis", "spot",
        }
        missing = sorted(key for key in required if ticket.get(key) is None)
        if missing:
            return _invalid(
                "SELL_COVERED_CALL ticket missing required fields: "
                + ", ".join(missing)
            )
        try:
            strike = float(ticket["strike"])
            qty = int(ticket["qty"])
            bid = float(ticket["bid"])
            ask = float(ticket["ask"])
            dte = int(ticket["dte"])
            delta = abs(float(ticket["delta"]))
            open_interest = int(ticket["open_interest"])
            spread_pct = float(ticket["spread_pct"])
            cost_basis = float(ticket["cost_basis"])
            spot = float(ticket["spot"])
        except (TypeError, ValueError):
            return _invalid("SELL_COVERED_CALL ticket has invalid numeric fields")
        if qty < 1 or bid <= 0 or ask < bid:
            return _invalid("SELL_COVERED_CALL ticket is not executable")
        if strike < max(cost_basis, spot * 1.02):
            return _invalid("SELL_COVERED_CALL strike violates cost-basis floor")
        if not CC_MIN_DTE <= dte <= CC_MAX_DTE:
            return _invalid("SELL_COVERED_CALL expiration is outside 7-45 DTE")
        if not CC_MIN_ABS_DELTA <= delta <= CC_MAX_ABS_DELTA:
            return _invalid("SELL_COVERED_CALL delta is outside 0.10-0.35")
        if open_interest < CC_MIN_OPEN_INTEREST:
            return _invalid("SELL_COVERED_CALL open interest is below 100")
        if spread_pct > CC_MAX_SPREAD_PCT:
            return _invalid("SELL_COVERED_CALL spread exceeds 20% of midpoint")
        return _valid()

    if source == "SHORT_PUT_MANAGER":
        if action != "CLOSE_SHORT_PUT":
            return _invalid(
                f"short-put manager ticket has unexpected action: {action}"
            )
        required = {"symbol", "qty", "bid", "ask", "entry_credit", "dte"}
        missing = sorted(key for key in required if ticket.get(key) is None)
        if missing:
            return _invalid(
                "CLOSE_SHORT_PUT ticket missing required fields: "
                + ", ".join(missing)
            )
        try:
            qty = int(ticket["qty"])
            bid = float(ticket["bid"])
            ask = float(ticket["ask"])
            entry_credit = float(ticket["entry_credit"])
        except (TypeError, ValueError):
            return _invalid("CLOSE_SHORT_PUT ticket has invalid numeric fields")
        if qty < 1 or bid < 0 or ask <= 0 or ask < bid or entry_credit <= 0:
            return _invalid("CLOSE_SHORT_PUT ticket is not executable")
        return _valid()

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


def cro_rejected_abort_node(state: WheelState) -> WheelState:
    return {
        "abort_reason": (
            f"CRO_REJECTED: exceeded {CRO_REJECT_MAX} retries.  "
            "No transaction will be made; existing positions require manual review.  "
            f"Last: {state.get('last_cro_reason', '')}"
        ),
    }


def retry_router_node(_: WheelState) -> WheelState:
    """Pass-through; routing is handled by the conditional edge."""
    return {}


def execution_broker_node(state: WheelState) -> WheelState:
    chain = (state.get("options_chain_input") or "").strip()
    try:
        ticket = json.loads(state.get("draft_ticket", "") or "{}")
    except json.JSONDecodeError:
        ticket = {}
    if str(ticket.get("action") or "").upper() in {
        "SELL_CSP",
        "SELL_COVERED_CALL",
        "CLOSE_SHORT_PUT",
    }:
        chain = str(ticket.get("options_chain") or "").strip()
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
    """Halt new risk, while still allowing risk-reducing short-put management."""
    portfolio_route = _derive_route_from_portfolio(
        state.get("portfolio_state", "") or ""
    )
    if portfolio_route == "SHORT_PUT_OPEN":
        return "clear"
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
    if route == "SHORT_PUT_OPEN":
        return "short_put"
    return "cash"


def _route_after_strategy_drafter(state: WheelState) -> str:
    """Short-circuit to END when deterministic strategy code says no trade."""
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
    if path == "short_put":
        return "redraft_short_put"
    return "redraft_cash"


def _route_after_cro(state: WheelState) -> str:
    raw = state.get("cro_output") or ""
    try:
        parsed = CROOutput.model_validate_json(raw)
        if parsed.status == "APPROVED":
            return "approved"
    except Exception:
        pass  # unparseable = REJECTED path

    if int(state.get("cro_retries", 0) or 0) >= CRO_REJECT_MAX:
        return "abort_rejected"
    return "rejected"


def _route_retry_target(state: WheelState) -> str:
    src = (state.get("ticket_source") or "").upper()
    if src in {"PUT_DRAFTER", "FUNDAMENTAL_SCREENER"}:
        return "fundamental_screener"
    if src == "ASSET_NOMINAL_DRAFTER":
        return "nominal_ticket"
    if src == "SHORT_PUT_MANAGER":
        return "short_put_manager"
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
    g.add_node("short_put_manager", short_put_manager_node)
    g.add_node("nominal_ticket", nominal_ticket_node)

    g.add_node("distressed_fork", distressed_fork_node)
    g.add_node("options_quant", options_quant_node)
    g.add_node("opportunity_cost", opportunity_cost_node)
    g.add_node("distressed_decider", distressed_decider_node)

    g.add_node("ticket_validator", ticket_validator_node)
    g.add_node("validation_abort", validation_abort_node)

    g.add_node("chief_risk_officer", chief_risk_officer_node)
    g.add_node("retry_router", retry_router_node)
    g.add_node("cro_rejected_abort", cro_rejected_abort_node)
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
            "short_put": "short_put_manager",
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
        _route_after_strategy_drafter,
        {"no_trade": END, "continue": "ticket_validator"},
    )

    # Existing-position paths
    g.add_conditional_edges(
        "short_put_manager",
        _route_after_strategy_drafter,
        {"no_trade": END, "continue": "ticket_validator"},
    )
    g.add_conditional_edges(
        "nominal_ticket",
        _route_after_strategy_drafter,
        {"no_trade": END, "continue": "ticket_validator"},
    )

    # Distressed path (sequential to prevent concurrent state writes)
    g.add_edge("distressed_fork", "options_quant")
    g.add_edge("options_quant", "opportunity_cost")
    g.add_edge("opportunity_cost", "distressed_decider")
    g.add_conditional_edges(
        "distressed_decider",
        _route_after_strategy_drafter,
        {"no_trade": END, "continue": "ticket_validator"},
    )

    # Ticket validation
    g.add_conditional_edges(
        "ticket_validator",
        _route_after_ticket_validation,
        {
            "valid": "chief_risk_officer",
            "give_up": "validation_abort",
            "redraft_cash": "fundamental_screener",
            "redraft_short_put": "short_put_manager",
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
            "abort_rejected": "cro_rejected_abort",
        },
    )
    g.add_edge("cro_rejected_abort", END)

    g.add_conditional_edges(
        "retry_router",
        _route_retry_target,
        {
            "fundamental_screener": "fundamental_screener",
            "short_put_manager": "short_put_manager",
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
                '"debt_to_equity": 1.1, "mkt_cap": 2800000000000}]'
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
