"""Copy Alpaca keys from .env into .cursor/mcp.json so Cursor MCP has access. Run from wheel_bot: uv run python scripts/sync_mcp_env.py"""
import json
from pathlib import Path

from dotenv import load_dotenv
import os

# wheel_bot/scripts/sync_mcp_env.py -> wheel_bot/, project root = trading_bot/
_wheel_bot = Path(__file__).resolve().parent.parent
_project_root = _wheel_bot.parent
_env_path = _wheel_bot / ".env"
_mcp_path = _project_root / ".cursor" / "mcp.json"

load_dotenv(_env_path)

def main():
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    paper = os.environ.get("ALPACA_PAPER_TRADE", "True")
    if not key or not secret:
        print("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env")
        return
    if not _mcp_path.exists():
        print(f".cursor/mcp.json not found at {_mcp_path}")
        return
    with open(_mcp_path) as f:
        data = json.load(f)
    server = data.get("mcpServers", {}).get("alpaca-mcp-server", {})
    server["env"] = {
        "ALPACA_API_KEY": key,
        "ALPACA_SECRET_KEY": secret,
        "ALPACA_PAPER_TRADE": paper,
    }
    data["mcpServers"]["alpaca-mcp-server"] = server
    with open(_mcp_path, "w") as f:
        json.dump(data, f, indent=2)
    print("Updated .cursor/mcp.json with Alpaca keys from .env")

if __name__ == "__main__":
    main()
