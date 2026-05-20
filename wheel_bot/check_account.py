"""Fetch Alpaca account balance and buying power.

Reads from ``.env`` (via ``config``) or falls back to ``.cursor/mcp.json``.
Uses ``ALPACA_PAPER_TRADE`` from ``.env`` so paper keys hit ``paper-api.alpaca.markets``.
"""

import json
from pathlib import Path

from alpaca.trading.client import TradingClient

from config import ALPACA_PAPER_TRADE, alpaca_trading_base_url, get_alpaca_credentials


def _get_credentials() -> tuple[str | None, str | None]:
    key, secret = get_alpaca_credentials()
    if key and secret:
        return key, secret
    mcp_path = Path(__file__).resolve().parent.parent / ".cursor" / "mcp.json"
    if mcp_path.exists():
        with open(mcp_path) as f:
            data = json.load(f)
        env = data.get("mcpServers", {}).get("alpaca-mcp-server", {}).get("env", {})
        k = env.get("ALPACA_API_KEY")
        s = env.get("ALPACA_SECRET_KEY")
        if k and s:
            return str(k).strip(), str(s).strip()
    return None, None


def main() -> None:
    key, secret = _get_credentials()
    if not key or not secret:
        print(
            "Add ALPACA_API_KEY and ALPACA_SECRET_KEY to .env "
            "(see .env.example) or .cursor/mcp.json."
        )
        return

    print(f"REST base: {alpaca_trading_base_url()}")
    print(f"Paper mode: {ALPACA_PAPER_TRADE}  (must match key type: paper vs live)")
    print(f"Key ID:    {key[:8]}…{key[-4:]}" if len(key) > 12 else f"Key ID: {key}")

    client = TradingClient(key, secret, paper=ALPACA_PAPER_TRADE)
    try:
        account = client.get_account()
    except Exception as exc:
        print(f"\nRequest failed: {exc}")
        print(
            "\nIf you see 401 Unauthorized:\n"
            "  • Paper account: set ALPACA_PAPER_TRADE=True in .env\n"
            "  • Live account:  set ALPACA_PAPER_TRADE=False\n"
            "  • Regenerated secret? Paste the new secret (no spaces).\n"
            "  • Keys must be from the same environment (paper vs live) as above."
        )
        return

    print("\nAccount balance:", account.cash)
    print("Buying power:   ", account.buying_power)
    print("Portfolio value:", account.portfolio_value)
    print("Equity:         ", account.equity)


if __name__ == "__main__":
    main()
