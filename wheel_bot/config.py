"""Load API keys and runtime toggles from environment or .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (parent of wheel_bot)
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)


def _strip_env(key: str) -> str | None:
    v = os.environ.get(key)
    return v.strip() if v else None


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("true", "1", "yes", "on")


OPENAI_API_KEY = _strip_env("OPENAI_API_KEY")
ALPACA_API_KEY = _strip_env("ALPACA_API_KEY")
ALPACA_SECRET_KEY = _strip_env("ALPACA_SECRET_KEY")
ALPACA_PAPER_TRADE = _env_bool("ALPACA_PAPER_TRADE", True)
WHEEL_BOT_RUN_ON_START = _env_bool("WHEEL_BOT_RUN_ON_START", False)
WHEEL_BOT_AUTO_EXECUTE = _env_bool("WHEEL_BOT_AUTO_EXECUTE", False)
WHEEL_BOT_ALLOW_LIVE_TRADING = _env_bool("WHEEL_BOT_ALLOW_LIVE_TRADING", False)

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_SENDER = (os.environ.get("SMTP_SENDER") or "").strip()


def _smtp_password() -> str:
    """Strip whitespace and optional surrounding quotes from .env values."""
    v = os.environ.get("SMTP_PASSWORD") or ""
    return v.strip().strip('"').strip("'")


SMTP_PASSWORD = _smtp_password()


def require_openai_key() -> str:
    """Return OpenAI API key or raise if missing."""
    if not OPENAI_API_KEY:
        raise ValueError(
            "OPENAI_API_KEY not set. Add it to .env or export OPENAI_API_KEY."
        )
    return OPENAI_API_KEY


def get_alpaca_credentials() -> tuple[str | None, str | None]:
    """Return (ALPACA_API_KEY, ALPACA_SECRET_KEY) from .env, or (None, None)."""
    return ALPACA_API_KEY, ALPACA_SECRET_KEY


def alpaca_trading_base_url() -> str:
    """REST base URL used by ``alpaca-py`` for the current paper/live setting."""
    return (
        "https://paper-api.alpaca.markets"
        if ALPACA_PAPER_TRADE
        else "https://api.alpaca.markets"
    )


def get_alpaca_trading_client(*, paper: bool | None = None):
    """Single factory for ``TradingClient`` — keeps paper vs live consistent.

    Paper account keys **must** use ``paper=True`` (``paper-api.alpaca.markets``).
    Live keys **must** use ``paper=False`` (``api.alpaca.markets``).  A mismatch
    yields HTTP 401.
    """
    from alpaca.trading.client import TradingClient

    key, secret = get_alpaca_credentials()
    if not key or not secret:
        raise ValueError(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env"
        )
    use_paper = ALPACA_PAPER_TRADE if paper is None else paper
    return TradingClient(key, secret, paper=use_paper)
