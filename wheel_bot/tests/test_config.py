from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from unittest import mock

from support import WHEEL_BOT_DIR


class ConfigTests(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("config", None)

    def load_config(self, env: dict[str, str]):
        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda path: None
        with mock.patch.dict(sys.modules, {"dotenv": fake_dotenv}):
            with mock.patch.dict(os.environ, env, clear=True):
                sys.modules.pop("config", None)
                return importlib.import_module("config")

    def test_bool_toggles_and_secret_stripping(self):
        cfg = self.load_config(
            {
                "OPENAI_API_KEY": "  sk-test  ",
                "ALPACA_PAPER_TRADE": "False",
                "WHEEL_BOT_RUN_ON_START": "yes",
                "WHEEL_BOT_AUTO_EXECUTE": "1",
                "WHEEL_BOT_ALLOW_LIVE_TRADING": "on",
                "SMTP_PASSWORD": ' "abc123" ',
            }
        )

        self.assertEqual(cfg.OPENAI_API_KEY, "sk-test")
        self.assertFalse(cfg.ALPACA_PAPER_TRADE)
        self.assertTrue(cfg.WHEEL_BOT_RUN_ON_START)
        self.assertTrue(cfg.WHEEL_BOT_AUTO_EXECUTE)
        self.assertTrue(cfg.WHEEL_BOT_ALLOW_LIVE_TRADING)
        self.assertEqual(cfg.SMTP_PASSWORD, "abc123")
        self.assertEqual(cfg.alpaca_trading_base_url(), "https://api.alpaca.markets")

    def test_require_openai_key_fails_closed(self):
        cfg = self.load_config({})

        with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY not set"):
            cfg.require_openai_key()

    def test_get_alpaca_client_uses_requested_paper_flag(self):
        cfg = self.load_config(
            {
                "ALPACA_API_KEY": "key",
                "ALPACA_SECRET_KEY": "secret",
                "ALPACA_PAPER_TRADE": "True",
            }
        )

        class TradingClient:
            def __init__(self, key, secret, paper=True):
                self.key = key
                self.secret = secret
                self.paper = paper

        alpaca = types.ModuleType("alpaca")
        trading = types.ModuleType("alpaca.trading")
        client_mod = types.ModuleType("alpaca.trading.client")
        client_mod.TradingClient = TradingClient
        with mock.patch.dict(
            sys.modules,
            {
                "alpaca": alpaca,
                "alpaca.trading": trading,
                "alpaca.trading.client": client_mod,
            },
        ):
            client = cfg.get_alpaca_trading_client(paper=False)

        self.assertEqual(client.key, "key")
        self.assertEqual(client.secret, "secret")
        self.assertFalse(client.paper)


if __name__ == "__main__":
    unittest.main()
