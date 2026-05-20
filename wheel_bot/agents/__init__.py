"""LangGraph agents for trading. Use: from agents.graph import run_trading_flow"""


def run_trading_flow(portfolio_state: str, **kwargs: object) -> str:
    """Lazy import to avoid RuntimeWarning when running python -m agents.graph."""
    from .graph import run_trading_flow as _run
    return _run(portfolio_state, **kwargs)


__all__ = ["run_trading_flow"]
