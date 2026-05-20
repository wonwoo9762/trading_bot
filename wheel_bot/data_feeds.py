"""Fetch live portfolio, macro, and options data from Alpaca.

This module bridges the gap between the Alpaca API and the JSON inputs
that ``run_trading_flow()`` expects.  Each function returns a plain string
ready to be passed as a graph input.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _get_trading_client():
    from config import get_alpaca_trading_client

    return get_alpaca_trading_client()


# ── Portfolio state ────────────────────────────────────────────────────────

def fetch_portfolio(ticker: str | None = None) -> str:
    """Build the portfolio JSON the graph expects.

    If *ticker* is provided, returns data for that specific position.
    Otherwise picks the largest equity position (by market value).

    Returns JSON like::

        {"ticker": "AAPL", "spot": 175.0, "cost_basis": 170.0,
         "cash": 5000.0, "shares": 100}
    """
    client = _get_trading_client()
    account = client.get_account()
    cash = float(account.cash)

    positions = client.get_all_positions()

    target = None
    if ticker:
        for p in positions:
            if p.symbol.upper() == ticker.upper():
                target = p
                break
    elif positions:
        target = max(positions, key=lambda p: abs(float(p.market_value)))

    if target is None:
        return json.dumps({
            "ticker": ticker or "NONE",
            "spot": 0,
            "cost_basis": 0,
            "cash": cash,
            "shares": 0,
        })

    return json.dumps({
        "ticker": target.symbol,
        "spot": float(target.current_price),
        "cost_basis": float(target.avg_entry_price),
        "cash": cash,
        "shares": int(float(target.qty)),
    })


def summarize_portfolio_json_for_email(portfolio_json: str) -> str:
    """Human-readable summary of the graph ``portfolio_state`` JSON for emails."""
    try:
        d = json.loads(portfolio_json)
    except Exception:
        return (portfolio_json or "")[:1200]
    if not isinstance(d, dict):
        return str(d)
    cash = float(d.get("cash") or 0)
    shares = float(d.get("shares") or 0)
    spot = float(d.get("spot") or 0)
    cost = float(d.get("cost_basis") or 0)
    nlv = cash + shares * spot
    ticker = d.get("ticker") or "—"
    return (
        f"Ticker: {ticker}\n"
        f"Cash: ${cash:,.2f}\n"
        f"Shares: {int(shares)} @ spot ${spot:.2f} (cost basis ${cost:.2f})\n"
        f"Implied NLV (cash + position): ${nlv:,.2f}"
    )


# ── Account summary (for email reports) ────────────────────────────────────

def fetch_account_summary() -> dict:
    """Return account-level balances and all open positions.

    Used by the notifier to give the recipient a full picture alongside
    the pipeline result.  Never raises — returns a safe fallback dict.
    """
    try:
        client = _get_trading_client()
        account = client.get_account()
        positions = client.get_all_positions()
    except Exception:
        logger.exception("Failed to fetch account summary")
        return {
            "error": "Could not fetch account data from Alpaca",
            "hint": (
                "Use paper API keys from your Paper account in .env, "
                "ALPACA_PAPER_TRADE=True, and no placeholders (REPLACE_*)."
            ),
        }

    pos_list = []
    for p in positions:
        market_val = float(p.market_value)
        cost = float(p.avg_entry_price) * float(p.qty)
        unrealized = float(p.unrealized_pl)
        pct = (unrealized / cost * 100) if cost else 0.0
        pos_list.append({
            "symbol": p.symbol,
            "qty": int(float(p.qty)),
            "avg_entry": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "market_value": market_val,
            "unrealized_pl": unrealized,
            "unrealized_pct": round(pct, 2),
        })

    return {
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
        "portfolio_value": float(account.portfolio_value),
        "equity": float(account.equity),
        "positions": pos_list,
    }


# ── Macro data ─────────────────────────────────────────────────────────────

# NYSE holidays for the current + next year.  Extend annually.
_NYSE_HOLIDAYS: set[date] = set()


def _load_nyse_holidays() -> set[date]:
    """Lazily compute NYSE holidays for the surrounding years."""
    if _NYSE_HOLIDAYS:
        return _NYSE_HOLIDAYS

    today = date.today()
    for year in (today.year, today.year + 1):
        # Fixed-ish holidays (simplified; real schedule has observation rules)
        _NYSE_HOLIDAYS.update([
            date(year, 1, 1),    # New Year's Day
            date(year, 1, 20),   # MLK Day (approx 3rd Mon)
            date(year, 2, 17),   # Presidents' Day (approx 3rd Mon)
            date(year, 7, 4),    # Independence Day
            date(year, 9, 1),    # Labor Day (approx 1st Mon)
            date(year, 11, 27),  # Thanksgiving (approx 4th Thu)
            date(year, 12, 25),  # Christmas
        ])
    return _NYSE_HOLIDAYS


def is_market_day(d: date | None = None) -> bool:
    """True if *d* is a weekday that isn't an NYSE holiday."""
    d = d or date.today()
    if d.weekday() >= 5:
        return False
    return d not in _load_nyse_holidays()


def fetch_macro() -> str:
    """Build a macro-data string for the Macro Sentinel.

    In production, wire this to a market data API (e.g. CBOE VIX feed,
    FRED for FOMC schedule).  For now, returns a safe placeholder that
    the Sentinel will classify as CLEAR.
    """
    # TODO: Integrate a real VIX / FOMC feed.
    #   - VIX: Yahoo Finance, CBOE, or Alpaca's market-data API
    #   - FOMC: FRED calendar API or a static schedule
    now_utc = datetime.now(timezone.utc).isoformat()
    return (
        f"Timestamp: {now_utc}. "
        "VIX: DATA_UNAVAILABLE. "
        "Next FOMC: DATA_UNAVAILABLE. "
        "News: No breaking macro events detected."
    )


# ── Fundamentals ───────────────────────────────────────────────────────────

def fetch_fundamentals(tickers: list[str] | None = None) -> str:
    """Return fundamental metrics for a set of tickers.

    In production, wire this to a fundamentals API (e.g. Financial
    Modeling Prep, Alpha Vantage, or Polygon.io).  For now, returns
    an empty list so the screener conservatively passes nothing.
    """
    # TODO: Integrate a real fundamentals provider.
    if not tickers:
        return "[]"
    logger.info("Fundamentals fetch requested for %s (stub)", tickers)
    return "[]"


# ── Options chain ──────────────────────────────────────────────────────────

def fetch_options_chain(ticker: str) -> str:
    """Fetch the options chain for *ticker* from Alpaca.

    Requires an Alpaca subscription that includes options data.
    Returns a human-readable string of strikes, expirations, and
    bid/ask spreads that the Quant and Broker nodes can parse.
    """
    try:
        from config import get_alpaca_credentials, get_alpaca_trading_client

        k, s = get_alpaca_credentials()
        if not k or not s:
            return f"OPTIONS_CHAIN_UNAVAILABLE: No Alpaca credentials for {ticker}"

        client = get_alpaca_trading_client()

        from alpaca.trading.requests import GetOptionContractsRequest

        today = date.today()
        req = GetOptionContractsRequest(
            underlying_symbols=[ticker.upper()],
            expiration_date_gte=today.isoformat(),
            expiration_date_lte=(today + timedelta(days=60)).isoformat(),
            status="active",
        )
        resp = client.get_option_contracts(req)
        contracts = resp.option_contracts if resp else []

        if not contracts:
            return f"OPTIONS_CHAIN_EMPTY: No active contracts for {ticker} within 60 days"

        lines: list[str] = []
        for c in contracts[:30]:
            lines.append(
                f"[{c.type.value.title()} {c.strike_price} "
                f"Exp {c.expiration_date} "
                f"Symbol: {c.symbol}]"
            )
        return "\n".join(lines)

    except ImportError:
        logger.warning("Alpaca options API not available in this alpaca-py version")
        return f"OPTIONS_CHAIN_UNAVAILABLE: alpaca-py options API not available for {ticker}"
    except Exception as exc:
        logger.warning("Options chain fetch failed for %s: %s", ticker, exc)
        return f"OPTIONS_CHAIN_ERROR: {exc}"


# ── Liquidation snapshot ──────────────────────────────────────────────────

def compute_liquidation_snapshot(portfolio_json: str) -> str:
    """Compute what happens if we liquidate the position today."""
    try:
        pf = json.loads(portfolio_json)
    except Exception:
        return "LIQUIDATION_DATA_UNAVAILABLE: Cannot parse portfolio"

    shares = float(pf.get("shares") or 0)
    spot = float(pf.get("spot") or 0)
    cost_basis = float(pf.get("cost_basis") or 0)
    cash = float(pf.get("cash") or 0)

    if shares <= 0:
        return "No position to liquidate."

    position_value = shares * spot
    total_cost = shares * cost_basis
    pnl = position_value - total_cost
    cash_after = cash + position_value

    return (
        f"Liquidating {int(shares)} shares of {pf.get('ticker', '?')} "
        f"at ${spot:.2f} realizes a "
        f"{'gain' if pnl >= 0 else 'loss'} of ${abs(pnl):,.2f}, "
        f"leaving ${cash_after:,.2f} cash."
    )
