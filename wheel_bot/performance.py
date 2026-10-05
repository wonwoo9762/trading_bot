"""Read-only broker account P/L reporting against an annual objective."""
from datetime import datetime, timezone

from portfolio_policy import (
    TARGET_ANNUAL_RETURN_MIN_PCT, TARGET_ANNUAL_RETURN_MAX_PCT, finite_number,
)


def summarize_history(history: dict) -> dict:
    timestamps = history.get("timestamp", [])
    returns = history.get("profit_loss_pct", [])
    pnl = history.get("profit_loss", [])
    equity = history.get("equity", [])
    if not timestamps or len({len(timestamps), len(returns), len(pnl), len(equity)}) != 1:
        raise ValueError("Incomplete account history")
    times = [finite_number(value, "timestamp") for value in timestamps]
    if any(later <= earlier for earlier, later in zip(times, times[1:])):
        raise ValueError("History timestamps must be increasing")
    valid = [i for i, value in enumerate(equity) if finite_number(value, "equity") > 0]
    if len(valid) < 2:
        raise ValueError("At least two funded history observations are required")
    first, last = valid[0], len(timestamps) - 1
    start = datetime.fromtimestamp(finite_number(timestamps[first], "start"), timezone.utc)
    end = datetime.fromtimestamp(finite_number(timestamps[last], "end"), timezone.utc)
    days = (end - start).total_seconds() / 86400
    if days <= 0:
        raise ValueError("History interval must be positive")
    # These percentages come from the broker's P/L series, not equity growth
    # or option premium receipts (both can misrepresent investment profit).
    period_return = finite_number(returns[last], "broker return") * 100
    return {
        "status": "AVAILABLE",
        "source": "Alpaca account portfolio history, 1D resolution",
        "period_start": start.date().isoformat(), "period_end": end.date().isoformat(),
        "observed_days": round(days, 1),
        "broker_reported_period_return_pct": round(period_return, 3),
        "broker_reported_period_pnl": round(finite_number(pnl[last], "broker P/L"), 2),
        "annual_objective_pct": [TARGET_ANNUAL_RETURN_MIN_PCT, TARGET_ANNUAL_RETURN_MAX_PCT],
        "compounded_objective_for_observed_days_pct": [
            round(((1 + target / 100) ** (days / 365) - 1) * 100, 3)
            for target in (TARGET_ANNUAL_RETURN_MIN_PCT, TARGET_ANNUAL_RETURN_MAX_PCT)
        ],
        "basis": "Broker-reported account P/L before tax, including all account activity. Not bot-only attribution, an audited cash-flow-adjusted return, or an annualized forecast. Reconcile transfers and external costs before evaluating the objective.",
    }


def fetch_performance_summary() -> dict:
    try:
        from config import get_alpaca_trading_client
        from alpaca.trading.requests import GetPortfolioHistoryRequest

        history = get_alpaca_trading_client().get_portfolio_history(
            GetPortfolioHistoryRequest(period="1A", timeframe="1D", pnl_reset="no_reset", cashflow_types="ALL")
        )
        return summarize_history(history if isinstance(history, dict) else history.model_dump())
    except Exception as exc:
        return {"status": "UNAVAILABLE", "reason": str(exc), "annual_objective_pct": [TARGET_ANNUAL_RETURN_MIN_PCT, TARGET_ANNUAL_RETURN_MAX_PCT]}
