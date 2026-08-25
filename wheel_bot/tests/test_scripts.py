from __future__ import annotations

import importlib
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from support import install_alpaca_stubs

install_alpaca_stubs()


class ScriptTests(unittest.TestCase):
    def test_manual_openai_smoke_helper_can_be_faked(self):
        import test_openai

        class ChatOpenAI:
            def __init__(self, model, temperature):
                self.model = model
                self.temperature = temperature

            def invoke(self, prompt):
                return types.SimpleNamespace(content="OpenAI connection OK.")

        fake_langchain_openai = types.ModuleType("langchain_openai")
        fake_langchain_openai.ChatOpenAI = ChatOpenAI

        with mock.patch.object(test_openai, "require_openai_key", return_value="sk-test"):
            with mock.patch.dict(sys.modules, {"langchain_openai": fake_langchain_openai}):
                reply = test_openai.test_openai_connection()

        self.assertEqual(reply, "OpenAI connection OK.")

    def test_send_test_email_uses_demo_portfolio_when_fetch_fails(self):
        send_test_email = importlib.import_module("scripts.send_test_email")
        sent = []

        with mock.patch.object(send_test_email, "fetch_portfolio", side_effect=RuntimeError("no account")):
            with mock.patch.object(send_test_email, "fetch_account_summary", return_value={"error": "none"}):
                with mock.patch.object(
                    send_test_email,
                    "send_run_report",
                    side_effect=lambda result, **kwargs: sent.append((result, kwargs)) or True,
                ):
                    with redirect_stdout(io.StringIO()):
                        send_test_email.main()

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1]["ticker"], "AAPL")
        self.assertIn("DRAFT_TICKET", sent[0][0])

    def test_sync_mcp_env_updates_cursor_config(self):
        sync_mcp_env = importlib.import_module("scripts.sync_mcp_env")

        with tempfile.TemporaryDirectory() as tmp:
            mcp_path = Path(tmp) / "mcp.json"
            mcp_path.write_text(
                json.dumps({"mcpServers": {"alpaca-mcp-server": {"env": {}}}}),
                encoding="utf-8",
            )

            with mock.patch.object(sync_mcp_env, "_mcp_path", mcp_path):
                with mock.patch.dict(
                    sync_mcp_env.os.environ,
                    {
                        "ALPACA_API_KEY": "key",
                        "ALPACA_SECRET_KEY": "secret",
                        "ALPACA_PAPER_TRADE": "True",
                    },
                    clear=False,
                ):
                    with redirect_stdout(io.StringIO()):
                        sync_mcp_env.main()

            data = json.loads(mcp_path.read_text(encoding="utf-8"))

        env = data["mcpServers"]["alpaca-mcp-server"]["env"]
        self.assertEqual(env["ALPACA_API_KEY"], "key")
        self.assertEqual(env["ALPACA_SECRET_KEY"], "secret")
        self.assertEqual(env["ALPACA_PAPER_TRADE"], "True")

    def test_check_account_credentials_use_config_first(self):
        check_account = importlib.import_module("check_account")

        with mock.patch.object(
            check_account, "get_alpaca_credentials", return_value=("key", "secret")
        ):
            self.assertEqual(check_account._get_credentials(), ("key", "secret"))


if __name__ == "__main__":
    unittest.main()
