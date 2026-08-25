"""Shared guardrails and output normalization for the wheel LangGraph agents."""

from __future__ import annotations

import re

GLOBAL_AGENT_POLICY = """
## Global policy (applies to every wheel agent)
- You are part of a **research / simulation** workflow.  Your output is a **draft artifact** for review or downstream automation—not a live trade.
- **Do not** claim you executed trades, moved money, or guaranteed profits.  **Do not** give personalized investment, tax, or legal advice.
- **Do not** ask for or assume access to secrets (API keys, passwords, SSNs).  Use only symbols and aggregates present in the input.
- **Do not** invent prices, VIX values, news, or fundamentals that are not in the provided inputs.  If critical numbers are missing, say so via the appropriate schema field (e.g. reason codes) instead of guessing.
- Prefer **conservative** outcomes when uncertain: HALT, REJECTED, empty screener
  list, NO_TRADE, MANUAL_REVIEW, or explicit missing-data reasons. Never infer
  that uncertainty is permission to liquidate an existing holding.
""".strip()

JSON_OUTPUT_CONTRACT = """
## Output contract
- Respond with **only** valid JSON as specified for your role.
- **No** markdown code fences, **no** preamble or postscript.
- Use double-quoted keys and strings.  No trailing commas.
""".strip()


def build_agent_system(role_prompt: str, *, structured: bool = False) -> str:
    """Compose the full system prompt sent to the model.

    When *structured* is True the JSON output contract is omitted because
    ``with_structured_output()`` already enforces the schema via function-calling.
    """
    parts = [GLOBAL_AGENT_POLICY, role_prompt.strip()]
    if not structured:
        parts.append(JSON_OUTPUT_CONTRACT)
    return "\n\n".join(p for p in parts if p)


_FENCE_OPEN = re.compile(r"^```(?:json)?\s*\n?", re.IGNORECASE)
_FENCE_CLOSE = re.compile(r"\n?```\s*$")


def normalize_llm_text(text: str | None) -> str:
    """Strip markdown fences and whitespace so downstream JSON parsing works."""
    if text is None:
        return ""
    t = str(text).strip()
    t = _FENCE_OPEN.sub("", t)
    t = _FENCE_CLOSE.sub("", t)
    return t.strip()


_INJECTION_PATTERNS = (
    r"ignore (all )?(previous|prior) instructions",
    r"disregard (the )?system prompt",
    r"you are now (a|an) ",
    r"new instructions:",
    r"<\|system\|>",
    r"<\|im_start\|>",
)

_INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]


def human_payload_suspicious(user_text: str) -> bool:
    """Return True if the human message looks like a prompt-injection attempt."""
    if not user_text:
        return False
    return any(rx.search(user_text) for rx in _INJECTION_RE)
