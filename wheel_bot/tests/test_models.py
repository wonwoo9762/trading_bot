from __future__ import annotations

import unittest

from pydantic import ValidationError

from support import WHEEL_BOT_DIR

from models import (
    BrokerOutput,
    CROOutput,
    CandidateSelectorOutput,
    MacroSentinelOutput,
    QuantOutput,
)


class ModelTests(unittest.TestCase):
    def test_core_schema_accepts_valid_outputs(self):
        macro = MacroSentinelOutput(status="CLEAR", reason="Nominal")
        quant = QuantOutput(
            action="ROLL",
            buy_to_close={"strike": 170, "exp": "2026-01-16"},
            sell_to_open={"strike": 175, "exp": "2026-02-20"},
            est_credit=1.25,
        )
        broker = BrokerOutput(
            symbol="AAPL260116C00170000",
            side="sell",
            qty=1,
            limit_price=1.2,
        )
        candidates = CandidateSelectorOutput(
            selected_tickers=["AAPL", "MSFT"],
            reason="Quality megacaps",
        )

        self.assertEqual(macro.status, "CLEAR")
        self.assertEqual(quant.buy_to_close.strike, 170)
        self.assertEqual(broker.side, "sell")
        self.assertEqual(candidates.selected_tickers, ["AAPL", "MSFT"])

    def test_schema_rejects_invalid_enums(self):
        with self.assertRaises(ValidationError):
            CROOutput(status="MAYBE", reason="invalid")

        with self.assertRaises(ValidationError):
            BrokerOutput(symbol="AAPL260116C00170000", side="hold")


if __name__ == "__main__":
    unittest.main()
