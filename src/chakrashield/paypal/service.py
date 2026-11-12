"""The money loop: turn a ChakraShield decision into a PayPal payment, and settle it when the parcel lands.

    decision STEP_UP_DEPOSIT  -> create a deposit order (the ₹49 token, charged in the configured currency, USD by default)
    decision FORCE_PREPAID    -> create an order for the full basket value
    buyer approves            -> capture            (return redirect, or the CHECKOUT.ORDER.APPROVED webhook)
    delivery outcome          -> deposit refunded if delivered, retained if the parcel RTO'd
    PayPal dispute raised     -> compile the CE3.0 packet and push it through provide-evidence

The risk request path never calls PayPal: ``/v1/risk/evaluate`` stays inside its 25 ms budget and
payment creation is a separate call the checkout makes after it has the decision.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from ..dispute.ce3 import TransactionLedger, compile_ce3
from ..dispute.render import render_packet_html
from .client import LIVE_BASE, SANDBOX_BASE, MockPayPal, PayPalClient, PayPalError, money

MAX_PAYMENTS = 50_000
DEPOSIT, PREPAID_KIND = "DEPOSIT", "PREPAID"
# payment states
CREATED, APPROVED, CAPTURED, REFUNDED, RETAINED, REVERSED = "CREATED", "APPROVED", "CAPTURED", "REFUNDED", "RETAINED", "REVERSED"


@dataclass
class Payment:
    order_id: str
    kind: str                       # DEPOSIT | PREPAID
    amount_inr: float
    amount: str                     # what PayPal charges, as a string in ``currency``
    currency: str
    paypal_order_id: str
    approve_url: str | None
    status: str = CREATED
    capture_id: str | None = None
    refund_id: str | None = None
    created_at: float = field(default_factory=time.time)
    history: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class PaymentError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def charge_amount(inr: float, currency: str, inr_per_usd: float) -> str:
    """INR is charged as is; USD is converted at ``inr_per_usd``."""
    if currency == "INR":
        return money(inr)
    return money((Decimal(str(inr)) / Decimal(str(inr_per_usd))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class PaymentService:
    def __init__(self, client, *, currency: str = "USD", inr_per_usd: float = 85.0, public_url: str = "", log_path: Path | None = None,
                 ledger: TransactionLedger | None = None):
        self.client, self.currency, self.inr_per_usd, self.public_url = client, currency.upper(), inr_per_usd, public_url.rstrip("/")
        self.ledger, self.log_path = ledger, log_path
        self._payments: "OrderedDict[str, Payment]" = OrderedDict()
        self._by_pp_order: dict[str, str] = {}
        self._seen_events: "OrderedDict[str, float]" = OrderedDict()
        self.disputes_seen: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_env(cls, ledger: TransactionLedger | None = None, log_path: Path | None = None) -> "PaymentService":
        public = os.environ.get("CHAKRA_PUBLIC_URL", "http://127.0.0.1:8080")
        rate = float(os.environ.get("PAYPAL_INR_PER_USD", 85.0))
        currency = os.environ.get("PAYPAL_CURRENCY", "USD").strip().upper() or "USD"
        cid, secret = os.environ.get("PAYPAL_CLIENT_ID"), os.environ.get("PAYPAL_CLIENT_SECRET")
        if cid and secret and os.environ.get("PAYPAL_MODE", "").lower() != "mock":
            base = LIVE_BASE if os.environ.get("PAYPAL_ENV", "sandbox").lower() == "live" else SANDBOX_BASE
            client = PayPalClient(cid, secret, base_url=base, webhook_id=os.environ.get("PAYPAL_WEBHOOK_ID"))
        else:
            client = MockPayPal(public_base=public)
        return cls(client, currency=currency, inr_per_usd=rate, public_url=public, log_path=log_path, ledger=ledger)

    @property
    def mode(self) -> str:
        return self.client.mode

    def status(self) -> dict:
        return {"mode": self.mode, "currency": self.currency, "inr_per_usd": self.inr_per_usd, "webhook_configured": bool(self.client.webhook_id),
                "payments": len(self._payments), "disputes_seen": len(self.disputes_seen), "public_url": self.public_url}

    # ------------------------------------------------------------------ bookkeeping
    def _audit(self, p: Payment, event: str, **extra) -> None:
        p.history.append({"ts": round(time.time(), 3), "event": event, "status": p.status, **extra})
        if self.log_path is not None:
            try:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"order_id": p.order_id, "event": event, "status": p.status, "kind": p.kind,
                                         "amount": p.amount, "currency": p.currency, "paypal_order_id": p.paypal_order_id, **extra}) + "\n")
            except OSError:
                pass

    def _store(self, p: Payment) -> None:
        with self._lock:
            self._payments[p.order_id] = p
            self._by_pp_order[p.paypal_order_id] = p.order_id
            self._payments.move_to_end(p.order_id)
            while len(self._payments) > MAX_PAYMENTS:
                _, old = self._payments.popitem(last=False)
                self._by_pp_order.pop(old.paypal_order_id, None)

    def get(self, order_id: str) -> Payment | None:
        return self._payments.get(order_id)

    # ------------------------------------------------------------------ the loop
    def start(self, order_id: str, action: str, gmv_inr: float, deposit_inr: float) -> Payment:
        """Create the PayPal order for a decision. Idempotent per order."""
        existing = self.get(order_id)
        if existing is not None:
            return existing
        if action == "STEP_UP_DEPOSIT":
            kind, inr = DEPOSIT, float(deposit_inr)
            desc = f"Refundable shipping deposit, order {order_id}"
        elif action == "FORCE_PREPAID":
            kind, inr = PREPAID_KIND, float(gmv_inr)
            desc = f"Prepaid order {order_id}"
        else:
            raise PaymentError(409, f"decision {action} needs no payment")
        amount = charge_amount(inr, self.currency, self.inr_per_usd)
        try:
            out = self.client.create_order(
                amount=amount, currency=self.currency, reference_id=order_id, custom_id=order_id, description=desc, request_id=f"chakra-{order_id}-create",
                return_url=f"{self.public_url}/v1/pay/{order_id}/return" if self.public_url else None,
                cancel_url=f"{self.public_url}/v1/pay/{order_id}/cancel" if self.public_url else None)
        except PayPalError as exc:
            raise PaymentError(502, f"PayPal create order failed: {exc}") from exc
        p = Payment(order_id=order_id, kind=kind, amount_inr=inr, amount=amount, currency=self.currency, paypal_order_id=out["id"], approve_url=out["approve_url"])
        self._audit(p, "order_created", amount_inr=inr)
        self._store(p)
        return p

    def capture(self, order_id: str) -> Payment:
        p = self.get(order_id)
        if p is None:
            raise PaymentError(404, "no payment for this order")
        if p.status in (CAPTURED, REFUNDED, RETAINED):
            return p
        try:
            out = self.client.capture_order(p.paypal_order_id, request_id=f"chakra-{order_id}-capture")
        except PayPalError as exc:
            if "ORDER_NOT_APPROVED" in (exc.name, exc.issue):
                raise PaymentError(409, "buyer has not approved the payment yet") from exc
            raise PaymentError(502, f"PayPal capture failed: {exc}") from exc
        if out.get("capture_status") not in ("COMPLETED", "PENDING"):
            raise PaymentError(402, f"capture not completed: {out.get('capture_status')}")
        p.status, p.capture_id = CAPTURED, out["capture_id"]
        self._audit(p, "captured", capture_id=p.capture_id)
        return p

    def settle(self, order_id: str, rto: bool) -> dict:
        """Delivery outcome: refund a delivered parcel's deposit, retain an RTO'd parcel's deposit."""
        p = self.get(order_id)
        if p is None:
            return {"action": "none", "reason": "no payment for this order"}
        if p.kind != DEPOSIT:
            return {"action": "none", "reason": "prepaid orders are settled by the merchant", "status": p.status}
        if p.status in (REFUNDED, RETAINED):
            return {"action": "none", "reason": "already settled", "status": p.status}
        if p.status != CAPTURED:
            return {"action": "none", "reason": "deposit was never paid", "status": p.status}
        if rto:
            p.status = RETAINED
            self._audit(p, "deposit_retained")
            return {"action": "retained", "status": p.status, "amount": p.amount, "currency": p.currency}
        try:
            out = self.client.refund_capture(p.capture_id, amount=None, currency=p.currency, note="Your deposit is refunded: the parcel was delivered. Thank you!",
                                             request_id=f"chakra-{order_id}-refund", invoice_id=f"refund-{order_id}")
        except PayPalError as exc:
            raise PaymentError(502, f"PayPal refund failed: {exc}") from exc
        p.status, p.refund_id = REFUNDED, out["id"]
        self._audit(p, "deposit_refunded", refund_id=p.refund_id)
        return {"action": "refunded", "status": p.status, "refund_id": p.refund_id, "amount": p.amount, "currency": p.currency}

    # ------------------------------------------------------------------ webhooks
    def handle_webhook(self, headers: dict[str, str], event: dict) -> dict:
        if not self.client.verify_webhook(headers, event):
            raise PaymentError(401, "webhook signature not verified")
        eid, etype = event.get("id", ""), event.get("event_type", "")
        with self._lock:
            if eid and eid in self._seen_events:
                return {"handled": False, "duplicate": True, "event_type": etype}
            if eid:
                self._seen_events[eid] = time.time()
                while len(self._seen_events) > 10_000:
                    self._seen_events.popitem(last=False)
        res = event.get("resource") or {}
        if etype == "CHECKOUT.ORDER.APPROVED":
            oid = self._by_pp_order.get(res.get("id", ""))
            if oid:
                p = self._payments[oid]
                if p.status == CREATED:
                    p.status = APPROVED
                    self._audit(p, "approved_webhook")
                self.capture(oid)
                return {"handled": True, "event_type": etype, "order_id": oid, "status": self._payments[oid].status}
        elif etype in ("PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"):
            oid = res.get("custom_id") or self._by_pp_order.get(((res.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id", ""))
            p = self._payments.get(oid or "")
            if p is not None:
                if etype == "PAYMENT.CAPTURE.COMPLETED" and p.status in (CREATED, APPROVED):
                    p.status, p.capture_id = CAPTURED, res.get("id")
                elif etype == "PAYMENT.CAPTURE.REFUNDED":
                    p.status = REFUNDED
                elif etype == "PAYMENT.CAPTURE.REVERSED":
                    p.status = REVERSED
                self._audit(p, f"webhook:{etype}")
                return {"handled": True, "event_type": etype, "order_id": p.order_id, "status": p.status}
        elif etype.startswith("CUSTOMER.DISPUTE."):
            did = res.get("dispute_id") or res.get("id")
            if did:
                self.disputes_seen[did] = {"dispute_id": did, "event_type": etype, "status": res.get("status"),
                                           "reason": res.get("reason"), "amount": res.get("dispute_amount")}
                return {"handled": True, "event_type": etype, "dispute_id": did}
        return {"handled": False, "event_type": etype}

    # ------------------------------------------------------------------ disputes
    def submit_dispute_evidence(self, dispute_id: str, transaction_id: str, *, dispute_date: str | None = None,
                                only_if_eligible: bool = True) -> dict:
        """Compile the deterministic CE3.0 packet for ``transaction_id`` and send it to PayPal for ``dispute_id``.

        The notes carry the verdict, the narrative and the packet's SHA-256, plus a link to the printable
        packet; no file is attached because PayPal restricts evidence file types and we have not
        verified which ones a sandbox accepts.
        """
        if self.ledger is None:
            raise PaymentError(503, "no transaction ledger loaded")
        packet = compile_ce3(self.ledger, transaction_id, "10.4", dispute_date)
        if not packet["criteria"]:
            raise PaymentError(404, packet["reason"])
        if only_if_eligible and not packet["eligible"]:
            return {"submitted": False, "eligible": False, "reason": packet["reason"], "packet_hash": packet["packet_hash"]}
        link = f"{self.public_url}/v1/dispute/packet/{transaction_id}.html" if self.public_url else ""
        notes = (f"[{packet['standard']}] {packet['evidence']['merchant_narrative']} "
                 f"Packet SHA-256: {packet['packet_hash']}. Eligible: {packet['eligible']} ({packet['reason']})."
                 + (f" Printable packet: {link}" if link else ""))
        try:
            self.client.provide_evidence(dispute_id, notes=notes, evidence_type="OTHER")
        except PayPalError as exc:
            raise PaymentError(502, f"PayPal provide-evidence failed: {exc}") from exc
        return {"submitted": True, "eligible": packet["eligible"], "packet_hash": packet["packet_hash"], "dispute_id": dispute_id,
                "notes_chars": len(notes), "packet_url": link or None, "render_bytes": len(render_packet_html(packet))}
