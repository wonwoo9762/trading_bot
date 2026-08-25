from __future__ import annotations

import json
import unittest
from email import message_from_string
from unittest import mock

from support import WHEEL_BOT_DIR

import notifier


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, sender, password):
        self.sender = sender
        self.password = password

    def sendmail(self, sender, recipients, message):
        self.sent.append((sender, recipients, message))


class NotifierTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.sent = []

    def test_build_html_prominently_explains_no_transaction(self):
        html = notifier._build_html(
            "MACRO_SENTINEL:\n{}",
            run_label="manual",
            ticker="AAPL",
            ts=notifier.datetime(2026, 1, 1, tzinfo=notifier.ET),
            transaction_summary={
                "transaction_made": False,
                "status": "BLOCKED",
                "action": "SELL_CSP",
                "symbol": "AAPL",
                "why": "CRO did not approve.",
            },
        )

        self.assertIn("No transaction made", html)
        self.assertIn("CRO did not approve.", html)

    def test_send_run_report_includes_transaction_summary_in_plain_email(self):
        with mock.patch.object(notifier, "SMTP_SENDER", "bot@example.com"):
            with mock.patch.object(notifier, "SMTP_PASSWORD", "secret"):
                with mock.patch.object(notifier, "RECIPIENTS", ["you@example.com"]):
                    with mock.patch.object(notifier.smtplib, "SMTP", FakeSMTP):
                        ok = notifier.send_run_report(
                            "ORDER_EXECUTION:\n{}",
                            run_label="manual",
                            ticker="AAPL",
                            account_snapshot={"cash": 1, "equity": 2, "buying_power": 3, "portfolio_value": 4, "positions": []},
                            portfolio_json=json.dumps({"ticker": "AAPL", "cash": 1}),
                            transaction_summary={
                                "transaction_made": True,
                                "status": "SUBMITTED",
                                "action": "SELL_COVERED_CALL",
                                "symbol": "AAPL260116C00170000",
                                "order_id": "order-123",
                                "why": "CRO approved and Alpaca accepted the order.",
                            },
                        )

        self.assertTrue(ok)
        self.assertEqual(len(FakeSMTP.sent), 1)
        message = message_from_string(FakeSMTP.sent[0][2])
        plain = next(
            part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8")
            for part in message.walk()
            if part.get_content_type() == "text/plain"
        )
        self.assertIn("Transaction made", plain)
        self.assertIn("order-123", plain)


if __name__ == "__main__":
    unittest.main()
