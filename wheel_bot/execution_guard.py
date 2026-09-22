"""Persistent reservation and broker reconciliation before option submission.

Workers sharing this journal serialize reservations. An unresolved reservation
survives a crash and blocks further orders, including on subsequent days.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from order_policy import ApprovedOptionOrder


DEFAULT_JOURNAL_PATH = Path(__file__).resolve().parent / "data" / "order_journal.sqlite3"
TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced"}


class ExecutionBlocked(ValueError):
    pass


def _field(order, key):
    return order.get(key) if isinstance(order, dict) else getattr(order, key, None)


def _snapshot(order, client_id):
    if not order or str(_field(order, "client_order_id") or "") != client_id:
        raise ExecutionBlocked("ORDER_RECONCILIATION_FAILED: broker returned a mismatched order")
    order_id = str(_field(order, "id") or "")
    status = _field(order, "status")
    status = str(getattr(status, "value", status) or "")
    if not order_id or not status:
        raise ExecutionBlocked("ORDER_RECONCILIATION_FAILED: broker order has no ID or status")
    return order_id, status


def lookup_order(client, client_id):
    """Only a real HTTP 404 establishes that this client ID was not found."""
    from alpaca.common.exceptions import APIError

    try:
        order = client.get_order_by_client_id(client_id)
    except APIError as exc:
        if exc.status_code == 404:
            return None
        raise
    _snapshot(order, client_id)
    return order


class ExecutionGuard:
    def __init__(self, path=DEFAULT_JOURNAL_PATH):
        self.path = Path(path)

    @contextmanager
    def _transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS order_intents (
                account_key TEXT NOT NULL,
                client_order_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                order_id TEXT,
                broker_status TEXT
            )""")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except ExecutionBlocked:
            # Persist any confirmed status updates even when a later safety
            # check denies the new order. No reservation is inserted on denial.
            conn.commit()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def client_order_id(account_key, approved, now):
        # One attempt per account/action/contract/ET day. Quantity and price
        # changes cannot disguise a retry as a new order.
        day = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        identity = json.dumps([account_key, day, approved.action, approved.symbol])
        return "wheelbot-" + hashlib.sha256(identity.encode()).hexdigest()[:32]

    @staticmethod
    def _record(conn, client_id, order):
        order_id, status = _snapshot(order, client_id)
        conn.execute(
            "UPDATE order_intents SET resolved=?, order_id=?, broker_status=? WHERE client_order_id=?",
            (int(status in TERMINAL_STATUSES), order_id, status, client_id),
        )

    def reserve(self, client, *, paper, approved: ApprovedOptionOrder, limit_price):
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        account = client.get_account()
        account_id = str(_field(account, "id") or "").strip()
        if not account_id:
            raise ExecutionBlocked("ACCOUNT_ID_REQUIRED: cannot scope order deduplication")
        account_key = f"{'paper' if paper else 'live'}:{account_id}"
        now = datetime.now(timezone.utc)
        client_id = self.client_order_id(account_key, approved, now)
        with self._transaction() as conn:
            pending = conn.execute(
                "SELECT client_order_id FROM order_intents WHERE account_key=? AND resolved=0",
                (account_key,),
            ).fetchall()
            for row in pending:
                pending_id = row["client_order_id"]
                order = lookup_order(client, pending_id)
                if order is None:
                    raise ExecutionBlocked(
                        f"UNRESOLVED_SUBMISSION: {pending_id} is not yet found at Alpaca; "
                        "manual reconciliation is required before further submissions"
                    )
                _, status = _snapshot(order, pending_id)
                self._record(conn, pending_id, order)
                if status not in TERMINAL_STATUSES:
                    raise ExecutionBlocked(f"PENDING_ORDER: {pending_id} has status {status}")

            if conn.execute(
                "SELECT 1 FROM order_intents WHERE client_order_id=?", (client_id,)
            ).fetchone():
                raise ExecutionBlocked(f"DUPLICATE_INTENT: {client_id} was already attempted today")
            if lookup_order(client, client_id) is not None:
                raise ExecutionBlocked(f"DUPLICATE_INTENT: Alpaca already has {client_id}")

            # The strategy does not yet budget pending collateral/coverage.
            # Conservatively require the whole account's open-order list empty.
            orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=1))
            if not isinstance(orders, list):
                raise ExecutionBlocked("OPEN_ORDER_CHECK_FAILED: expected an order list")
            if orders:
                raise ExecutionBlocked("PENDING_ORDER: account has an open order; reconcile it before submitting")
            conn.execute(
                "INSERT INTO order_intents (account_key, client_order_id, created_at, payload) VALUES (?, ?, ?, ?)",
                (account_key, client_id, now.isoformat(), json.dumps({
                    "action": approved.action, "symbol": approved.symbol, "qty": approved.qty,
                    "side": approved.side, "position_intent": approved.position_intent,
                    "limit_price": str(limit_price),
                })),
            )
        # Reservation is durable BEFORE submit_order; never erase it on failure.
        return client_id

    def record(self, client_id, order):
        with self._transaction() as conn:
            self._record(conn, client_id, order)


def order_outcome_label(summary):
    """Describe submission evidence without claiming that an order filled."""
    status = str(summary.get("status") or "")
    if status == "UNKNOWN":
        return "Order status uncertain; reconciliation required"
    if status == "RECONCILED":
        return "Existing order found; no new submission"
    if status == "SUBMITTED":
        return "Order submitted; fill not confirmed"
    return "No new order submitted"
