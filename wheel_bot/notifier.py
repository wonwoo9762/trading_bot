"""Email notifications for the Wheel trading bot.

Uses Gmail SMTP with an App Password.  To set up:
  1. Enable 2-Step Verification on your Google account
  2. Go to https://myaccount.google.com/apppasswords
  3. Create an App Password for "Mail"
  4. Put the 16-char password in .env as SMTP_PASSWORD
"""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from config import (
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_SENDER,
)

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# ── Recipient list ─────────────────────────────────────────────────────────
# Add new emails here.  That's it.
RECIPIENTS: list[str] = [
    "wonwoo9762@gmail.com",
]


def send_run_report(
    result: str,
    *,
    run_label: str = "scheduled",
    ticker: str = "N/A",
    account_snapshot: dict | None = None,
    portfolio_json: str | None = None,
    transaction_summary: dict | None = None,
    extra_recipients: list[str] | None = None,
) -> bool:
    """Send the Wheel pipeline output as a formatted email.

    Parameters
    ----------
    account_snapshot : dict, optional
        Output of ``data_feeds.fetch_account_summary()`` — **real** balances
        and positions from Alpaca at send time.
    portfolio_json : str, optional
        The ``portfolio_state`` JSON passed into ``run_trading_flow`` for this
        run.  Shown as "Strategy input" so the pipeline math (e.g. NLV in
        DRAFT_TICKET) matches what the graph used.
    transaction_summary : dict, optional
        Scheduler-built explanation of whether an order was submitted and why.

    Returns True if the email was sent, False on any failure (logged, never
    raises — notifications must not crash the scheduler).
    """
    if not SMTP_SENDER or not SMTP_PASSWORD:
        logger.warning(
            "Email not configured (SMTP_SENDER / SMTP_PASSWORD missing in .env). "
            "Skipping notification."
        )
        return False

    to_list = list(RECIPIENTS)
    if extra_recipients:
        to_list.extend(extra_recipients)

    if not to_list:
        logger.warning("No recipients configured — skipping email")
        return False

    now = datetime.now(ET)
    subject = (
        f"Wheel Bot [{run_label.upper()}] — "
        f"{ticker} — {now.strftime('%b %d %Y %I:%M %p')} ET"
    )

    html_body = _build_html(
        result,
        run_label=run_label,
        ticker=ticker,
        ts=now,
        account=account_snapshot,
        portfolio_json=portfolio_json,
        transaction_summary=transaction_summary,
    )

    plain_tx = ""
    if transaction_summary:
        outcome = (
            "Transaction made"
            if transaction_summary.get("transaction_made")
            else "No transaction made"
        )
        plain_tx = (
            "--- Transaction summary ---\n"
            f"Outcome: {outcome}\n"
            f"Status: {transaction_summary.get('status', 'N/A')}\n"
            f"Action: {transaction_summary.get('action', 'N/A')}\n"
            f"Symbol: {transaction_summary.get('symbol', 'N/A')}\n"
        )
        if transaction_summary.get("order_id"):
            plain_tx += f"Order ID: {transaction_summary['order_id']}\n"
        plain_tx += f"Why: {transaction_summary.get('why', '')}\n\n"

    plain_acct = ""
    if account_snapshot and "error" not in account_snapshot:
        plain_acct = (
            f"Cash: ${account_snapshot.get('cash', 0):,.2f}  |  "
            f"Equity: ${account_snapshot.get('equity', 0):,.2f}  |  "
            f"Buying Power: ${account_snapshot.get('buying_power', 0):,.2f}\n"
        )
        for pos in account_snapshot.get("positions", []):
            pnl_sign = "+" if pos["unrealized_pl"] >= 0 else ""
            plain_acct += (
                f"  {pos['symbol']}: {pos['qty']} shares @ ${pos['avg_entry']:.2f} → "
                f"${pos['current_price']:.2f}  "
                f"({pnl_sign}${pos['unrealized_pl']:,.2f} / {pnl_sign}{pos['unrealized_pct']:.1f}%)\n"
            )
        plain_acct += "\n"
    elif account_snapshot and account_snapshot.get("error"):
        plain_acct = (
            f"[Account overview unavailable: {account_snapshot.get('error')}\n"
            f"{account_snapshot.get('hint', '')}]\n\n"
        )

    plain_strategy = ""
    if portfolio_json:
        from data_feeds import summarize_portfolio_json_for_email

        plain_strategy = (
            "\n--- Strategy input (this run) ---\n"
            f"{summarize_portfolio_json_for_email(portfolio_json)}\n\n"
        )

    plain_body = (
        f"Wheel Bot {run_label} run — {ticker}\n"
        f"{now.strftime('%Y-%m-%d %H:%M:%S')} ET\n\n"
        f"{plain_tx}"
        f"{plain_acct}"
        f"{plain_strategy}"
        f"{result}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_SENDER
    msg["To"] = ", ".join(to_list)
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_SENDER, SMTP_PASSWORD)
            server.sendmail(SMTP_SENDER, to_list, msg.as_string())
        logger.info("Email sent to %s", to_list)
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to_list)
        return False


def _build_html(
    result: str,
    *,
    run_label: str,
    ticker: str,
    ts: datetime,
    account: dict | None = None,
    portfolio_json: str | None = None,
    transaction_summary: dict | None = None,
) -> str:
    """Convert the pipeline result text into a clean HTML email."""
    from data_feeds import summarize_portfolio_json_for_email

    def esc(value: object) -> str:
        return (
            str(value or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    transaction_html = ""
    if transaction_summary:
        made = bool(transaction_summary.get("transaction_made"))
        accent = "#27ae60" if made else "#c0392b"
        bg = "#edf8f0" if made else "#fff4f2"
        outcome = "Transaction made" if made else "No transaction made"
        order_id = transaction_summary.get("order_id")
        order_line = (
            f'<p style="margin:6px 0 0;font-size:13px;color:#555;">'
            f'<strong>Order ID:</strong> {esc(order_id)}</p>'
            if order_id
            else ""
        )
        transaction_html = (
            f'<div style="background:{bg};border:1px solid {accent};border-radius:8px;'
            'padding:14px;margin-bottom:16px;">'
            f'<strong style="color:{accent};font-size:15px;">{outcome}</strong>'
            f'<p style="margin:8px 0 0;font-size:13px;color:#555;">'
            f'<strong>Status:</strong> {esc(transaction_summary.get("status", "N/A"))}'
            f' &nbsp; <strong>Action:</strong> {esc(transaction_summary.get("action", "N/A"))}'
            f' &nbsp; <strong>Symbol:</strong> {esc(transaction_summary.get("symbol", "N/A"))}</p>'
            f'{order_line}'
            f'<p style="margin:8px 0 0;font-size:14px;color:#333;">'
            f'{esc(transaction_summary.get("why", ""))}</p>'
            '</div>'
        )

    # ── Account snapshot section ──────────────────────────────────────
    account_html = ""
    if account and "error" in account:
        hint = account.get("hint", "")
        esc_hint = (
            hint.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        account_html += (
            '<div style="background:#fff8e6;border:1px solid #f0c040;border-radius:8px;'
            'padding:14px;margin-bottom:16px;">'
            '<strong style="color:#a06000;">Account overview unavailable</strong>'
            f'<p style="margin:8px 0 0;font-size:14px;color:#555;">'
            f'{esc_hint}</p>'
            f'<p style="margin:8px 0 0;font-size:12px;color:#888;">'
            f'Alpaca error: {account.get("error", "")}</p></div>'
        )

    if account and "error" not in account:
        cash = account.get("cash", 0)
        equity = account.get("equity", 0)
        buying_power = account.get("buying_power", 0)
        portfolio_value = account.get("portfolio_value", 0)

        account_html += (
            '<div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:8px;'
            'padding:16px;margin-bottom:20px;">'
            '<h3 style="margin:0 0 12px;font-size:15px;color:#555;">Account Overview</h3>'
            '<table style="width:100%;border-collapse:collapse;">'
            '<tr>'
        )

        for label, value in [
            ("Cash", cash),
            ("Equity", equity),
            ("Portfolio Value", portfolio_value),
            ("Buying Power", buying_power),
        ]:
            account_html += (
                f'<td style="text-align:center;padding:6px 8px;">'
                f'<div style="font-size:11px;color:#999;text-transform:uppercase;'
                f'letter-spacing:0.5px;">{label}</div>'
                f'<div style="font-size:18px;font-weight:700;color:#222;">'
                f'${value:,.2f}</div></td>'
            )

        account_html += '</tr></table>'

        positions = account.get("positions", [])
        if positions:
            account_html += (
                '<h3 style="margin:16px 0 8px;font-size:14px;color:#555;">'
                'Open Positions</h3>'
                '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
                '<tr style="border-bottom:2px solid #ddd;">'
                '<th style="text-align:left;padding:4px 8px;color:#888;">Symbol</th>'
                '<th style="text-align:right;padding:4px 8px;color:#888;">Qty</th>'
                '<th style="text-align:right;padding:4px 8px;color:#888;">Avg Entry</th>'
                '<th style="text-align:right;padding:4px 8px;color:#888;">Current</th>'
                '<th style="text-align:right;padding:4px 8px;color:#888;">Mkt Value</th>'
                '<th style="text-align:right;padding:4px 8px;color:#888;">P&amp;L</th>'
                '</tr>'
            )
            for pos in positions:
                pnl = pos["unrealized_pl"]
                pnl_pct = pos["unrealized_pct"]
                pnl_color = "#27ae60" if pnl >= 0 else "#c0392b"
                pnl_sign = "+" if pnl >= 0 else ""
                account_html += (
                    f'<tr style="border-bottom:1px solid #eee;">'
                    f'<td style="padding:6px 8px;font-weight:600;">{pos["symbol"]}</td>'
                    f'<td style="padding:6px 8px;text-align:right;">{pos["qty"]}</td>'
                    f'<td style="padding:6px 8px;text-align:right;">${pos["avg_entry"]:.2f}</td>'
                    f'<td style="padding:6px 8px;text-align:right;">${pos["current_price"]:.2f}</td>'
                    f'<td style="padding:6px 8px;text-align:right;">${pos["market_value"]:,.2f}</td>'
                    f'<td style="padding:6px 8px;text-align:right;color:{pnl_color};font-weight:600;">'
                    f'{pnl_sign}${pnl:,.2f}<br>'
                    f'<span style="font-size:11px;">{pnl_sign}{pnl_pct:.1f}%</span></td>'
                    f'</tr>'
                )
            account_html += '</table>'

        account_html += '</div>'

    # ── Strategy input (matches pipeline / DRAFT_TICKET math) ────────
    strategy_html = ""
    if portfolio_json and portfolio_json.strip():
        brief = summarize_portfolio_json_for_email(portfolio_json)
        esc_brief = (
            brief.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        strategy_html = (
            '<div style="background:#f0f7ff;border:1px solid #b8d4f0;border-radius:8px;'
            'padding:14px;margin-bottom:20px;">'
            '<h3 style="margin:0 0 8px;font-size:15px;color:#336;">'
            'Strategy input (this run)</h3>'
            '<p style="margin:0;font-size:13px;font-family:monospace;color:#333;line-height:1.6;">'
            f'{esc_brief}</p>'
            '<p style="margin:10px 0 0;font-size:11px;color:#668;">'
            'This is the portfolio JSON fed into the graph. '
            'Pipeline Result below should agree with these numbers.</p></div>'
        )

    # ── Pipeline result section ───────────────────────────────────────
    sections = result.split("\n\n")
    rows: list[str] = []

    for section in sections:
        if not section.strip():
            continue
        lines = section.strip().split("\n", 1)
        header = lines[0].rstrip(":")
        body = lines[1].strip() if len(lines) > 1 else ""

        color = "#2d7d46"
        if "ABORT" in header or "REJECTED" in header.upper():
            color = "#c0392b"
        elif "HALT" in body.upper():
            color = "#e67e22"
        elif "APPROVED" in body.upper():
            color = "#27ae60"

        escaped_body = (
            body.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

        rows.append(
            f'<tr>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;'
            f'font-weight:600;color:{color};vertical-align:top;white-space:nowrap;">'
            f'{header}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;'
            f'font-family:monospace;font-size:13px;word-break:break-all;">'
            f'{escaped_body}</td>'
            f'</tr>'
        )

    table_rows = "\n".join(rows)

    return f"""\
<html>
<body style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#333;margin:0;padding:20px;">
  <div style="max-width:700px;margin:0 auto;">
    <h2 style="margin:0 0 4px;">Wheel Bot — {run_label.upper()}</h2>
    <p style="margin:0 0 16px;color:#888;font-size:14px;">
      {ticker} &nbsp;|&nbsp; {ts.strftime('%b %d, %Y  %I:%M %p')} ET
    </p>
    {transaction_html}
    {account_html}
    {strategy_html}
    <h3 style="margin:0 0 8px;font-size:15px;color:#555;">Pipeline Result</h3>
    <table style="width:100%;border-collapse:collapse;border:1px solid #ddd;border-radius:6px;">
      {table_rows}
    </table>
    <p style="margin:16px 0 0;color:#aaa;font-size:12px;">
      <strong>Account Overview</strong> is from Alpaca at send time.
      <strong>Strategy input</strong> is what the graph used for this run (scheduler).
      <strong>Pipeline Result</strong> is LLM output and broker outcome details.
    </p>
  </div>
</body>
</html>"""
