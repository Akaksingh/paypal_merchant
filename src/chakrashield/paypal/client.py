"""PayPal REST clients: a real one (sandbox / live) and a mock with the same interface.

Only the endpoints ChakraShield needs are wrapped:

    POST /v1/oauth2/token                                   client-credentials token
    POST /v2/checkout/orders                                create the deposit / prepaid order
    POST /v2/checkout/orders/{id}/capture                   capture after the buyer approves
    POST /v2/payments/captures/{id}/refund                  refund a deposit when the parcel is delivered
    GET  /v1/customer/disputes[/{id}]                       list / read disputes
    POST /v1/customer/disputes/{id}/provide-evidence        multipart evidence submission
    POST /v1/notifications/verify-webhook-signature         webhook authenticity

Every money-moving call carries a ``PayPal-Request-Id`` so a retry after a timeout cannot charge
or refund twice. The mock keeps the same idempotency rule and the same state machine
(CREATED -> APPROVED -> COMPLETED, capture refused before approval), so code written against it
behaves the same against the sandbox.
"""
from __future__ import annotations

import json
import threading
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
LIVE_BASE = "https://api-m.paypal.com"


class PayPalError(Exception):
    """A PayPal call failed. ``name`` is PayPal's error name (e.g. ORDER_NOT_APPROVED) when it gave one."""

    def __init__(self, status: int, name: str = "", message: str = "", debug_id: str = "", issue: str = ""):
        super().__init__(f"PayPal {status} {name}{' / ' + issue if issue else ''}: {message}".strip())
        self.status, self.name, self.message, self.debug_id, self.issue = status, name, message, debug_id, issue


def money(value: Decimal | float | str) -> str:
    """PayPal wants the amount as a string with two decimals (INR and USD both use two)."""
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class PayPalClient:
    """The real thing. Synchronous httpx: the gateway calls it from threadpool routes."""

    mode = "sandbox"

    def __init__(self, client_id: str, client_secret: str, *, base_url: str = SANDBOX_BASE,
                 webhook_id: str | None = None, http: httpx.Client | None = None, timeout: float = 10.0):
        self._id, self._secret, self.base_url, self.webhook_id = client_id, client_secret, base_url.rstrip("/"), webhook_id
        self._http = http or httpx.Client(timeout=timeout)
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = threading.Lock()
        if base_url.rstrip("/") == LIVE_BASE:
            self.mode = "live"

    # ------------------------------------------------------------------ plumbing
    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            r = self._http.post(f"{self.base_url}/v1/oauth2/token", auth=(self._id, self._secret),
                                data={"grant_type": "client_credentials"}, headers={"Accept": "application/json"})
            if r.status_code != 200:
                raise PayPalError(r.status_code, "AUTH_FAILED", "could not obtain an access token")
            body = r.json()
            self._token = body["access_token"]
            self._token_exp = time.time() + float(body.get("expires_in", 300))
            return self._token

    def _request(self, method: str, path: str, *, json_body: dict | None = None, files: dict | None = None,
                 request_id: str | None = None, params: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        r = self._http.request(method, f"{self.base_url}{path}", headers=headers, params=params,
                               content=json.dumps(json_body) if json_body is not None else None, files=files)
        if r.status_code >= 400:
            try:
                err = r.json()
            except ValueError:
                err = {}
            # PayPal's 422s all carry name UNPROCESSABLE_ENTITY; the actionable code is details[0].issue
            # (e.g. ORDER_NOT_APPROVED, INSTRUMENT_DECLINED, CAPTURE_FULLY_REFUNDED).
            details = err.get("details") or [{}]
            issue = details[0].get("issue", "") if isinstance(details[0], dict) else ""
            raise PayPalError(r.status_code, err.get("name", ""), err.get("message", r.text[:200]), err.get("debug_id", ""), issue)
        return r.json() if r.content else {}

    # ------------------------------------------------------------------ orders
    def create_order(self, *, amount: str, currency: str = "INR", reference_id: str, custom_id: str, description: str,
                     request_id: str, return_url: str | None = None, cancel_url: str | None = None) -> dict:
        body: dict[str, Any] = {
            "intent": "CAPTURE",
            "purchase_units": [{"reference_id": reference_id, "custom_id": custom_id, "description": description[:127],
                                "amount": {"currency_code": currency, "value": amount}}],
        }
        if return_url and cancel_url:
            body["payment_source"] = {"paypal": {"experience_context": {
                "return_url": return_url, "cancel_url": cancel_url, "user_action": "PAY_NOW"}}}
        out = self._request("POST", "/v2/checkout/orders", json_body=body, request_id=request_id)
        links = {link["rel"]: link["href"] for link in out.get("links", [])}
        return {"id": out["id"], "status": out["status"], "approve_url": links.get("payer-action") or links.get("approve")}

    def capture_order(self, order_id: str, *, request_id: str) -> dict:
        # PayPal answers 415 UNSUPPORTED_MEDIA_TYPE unless the capture is sent as JSON, even with an empty body.
        out = self._request("POST", f"/v2/checkout/orders/{order_id}/capture", json_body={}, request_id=request_id)
        cap = ((out.get("purchase_units") or [{}])[0].get("payments", {}).get("captures") or [{}])[0]
        return {"status": out.get("status"), "capture_id": cap.get("id"), "capture_status": cap.get("status"),
                "amount": (cap.get("amount") or {}).get("value")}

    def refund_capture(self, capture_id: str, *, amount: str | None, currency: str = "INR", note: str, request_id: str,
                       invoice_id: str | None = None) -> dict:
        body: dict[str, Any] = {"note_to_payer": note[:255]}
        if amount:
            body["amount"] = {"currency_code": currency, "value": amount}
        if invoice_id:
            body["invoice_id"] = invoice_id
        out = self._request("POST", f"/v2/payments/captures/{capture_id}/refund", json_body=body, request_id=request_id)
        return {"id": out.get("id"), "status": out.get("status")}

    # ------------------------------------------------------------------ disputes
    def list_disputes(self, page_size: int = 10) -> list[dict]:
        out = self._request("GET", "/v1/customer/disputes", params={"page_size": page_size})
        return out.get("items", [])

    def get_dispute(self, dispute_id: str) -> dict:
        return self._request("GET", f"/v1/customer/disputes/{dispute_id}")

    def provide_evidence(self, dispute_id: str, *, notes: str, evidence_type: str = "OTHER",
                         attachments: list[tuple[str, bytes, str]] | None = None) -> dict:
        """multipart/form-data: a JSON ``input`` part plus optional file parts."""
        payload = {"evidences": [{"evidence_type": evidence_type, "notes": notes[:2000]}]}
        files: dict[str, tuple] = {"input": (None, json.dumps(payload), "application/json")}
        for i, (name, data, mime) in enumerate(attachments or [], start=1):
            files[f"file{i}"] = (name, data, mime)
        return self._request("POST", f"/v1/customer/disputes/{dispute_id}/provide-evidence", files=files)

    # ------------------------------------------------------------------ webhooks
    def verify_webhook(self, headers: dict[str, str], event: dict) -> bool:
        if not self.webhook_id:
            return False
        h = {k.lower(): v for k, v in headers.items()}
        body = {"auth_algo": h.get("paypal-auth-algo"), "cert_url": h.get("paypal-cert-url"),
                "transmission_id": h.get("paypal-transmission-id"), "transmission_sig": h.get("paypal-transmission-sig"),
                "transmission_time": h.get("paypal-transmission-time"), "webhook_id": self.webhook_id, "webhook_event": event}
        if not all(body[k] for k in ("auth_algo", "cert_url", "transmission_id", "transmission_sig", "transmission_time")):
            return False
        out = self._request("POST", "/v1/notifications/verify-webhook-signature", json_body=body)
        return out.get("verification_status") == "SUCCESS"


class MockPayPal:
    """Offline stand-in with the same interface and the same rules. Used when no credentials are set."""

    mode = "mock"
    webhook_id = "MOCK-WEBHOOK"
    VALID_SIGNATURE = "mock-valid"

    def __init__(self, public_base: str = ""):
        self._lock = threading.Lock()
        self._seq = 0
        self._public = public_base.rstrip("/")
        self.orders: dict[str, dict] = {}
        self.captures: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self._idem: dict[str, dict] = {}
        self.disputes: dict[str, dict] = {
            "PP-D-MOCK-1001": {"dispute_id": "PP-D-MOCK-1001", "reason": "UNAUTHORISED", "status": "WAITING_FOR_SELLER_RESPONSE",
                               "dispute_amount": {"currency_code": "USD", "value": "42.10"}, "evidence": []},
            "PP-D-MOCK-1002": {"dispute_id": "PP-D-MOCK-1002", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                               "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_amount": {"currency_code": "USD", "value": "18.75"},
                               "evidence": []},
        }

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"MOCK-{prefix}-{self._seq:05d}"

    def _once(self, request_id: str, make):
        """PayPal-Request-Id semantics: a replay returns the first response and does nothing."""
        with self._lock:
            if request_id in self._idem:
                return self._idem[request_id]
            out = make()
            self._idem[request_id] = out
            return out

    def create_order(self, *, amount: str, currency: str = "INR", reference_id: str, custom_id: str, description: str,
                     request_id: str, return_url: str | None = None, cancel_url: str | None = None) -> dict:
        def make():
            oid = self._id("ORD")
            self.orders[oid] = {"status": "CREATED", "amount": amount, "custom_id": custom_id, "reference_id": reference_id}
            return {"id": oid, "status": "CREATED", "approve_url": f"{self._public}/v1/paypal/mock/approve/{oid}"}
        return self._once(request_id, make)

    def approve(self, order_id: str) -> dict:
        """Stands in for the buyer clicking Pay in the PayPal window."""
        with self._lock:
            o = self.orders.get(order_id)
            if o is None:
                raise PayPalError(404, "RESOURCE_NOT_FOUND", f"order {order_id} not found")
            if o["status"] == "CREATED":
                o["status"] = "APPROVED"
            return {"id": order_id, "status": o["status"]}

    def capture_order(self, order_id: str, *, request_id: str) -> dict:
        def make():
            o = self.orders.get(order_id)
            if o is None:
                raise PayPalError(404, "RESOURCE_NOT_FOUND", f"order {order_id} not found")
            if o["status"] == "CREATED":
                raise PayPalError(422, "ORDER_NOT_APPROVED", "payer has not approved the order")
            if o["status"] == "COMPLETED":
                raise PayPalError(422, "ORDER_ALREADY_CAPTURED", "order already captured")
            cid = self._id("CAP")
            o["status"] = "COMPLETED"
            self.captures[cid] = {"order_id": order_id, "amount": o["amount"], "status": "COMPLETED", "refunded": False}
            return {"status": "COMPLETED", "capture_id": cid, "capture_status": "COMPLETED", "amount": o["amount"]}
        return self._once(request_id, make)

    def refund_capture(self, capture_id: str, *, amount: str | None, currency: str = "INR", note: str, request_id: str,
                       invoice_id: str | None = None) -> dict:
        def make():
            c = self.captures.get(capture_id)
            if c is None:
                raise PayPalError(404, "RESOURCE_NOT_FOUND", f"capture {capture_id} not found")
            if c["refunded"]:
                raise PayPalError(422, "CAPTURE_FULLY_REFUNDED", "capture already refunded")
            rid = self._id("REF")
            c["refunded"], c["status"] = True, "REFUNDED"
            self.refunds[rid] = {"capture_id": capture_id, "amount": amount or c["amount"], "note": note}
            return {"id": rid, "status": "COMPLETED"}
        return self._once(request_id, make)

    def list_disputes(self, page_size: int = 10) -> list[dict]:
        return [{k: v for k, v in d.items() if k != "evidence"} for d in list(self.disputes.values())[:page_size]]

    def get_dispute(self, dispute_id: str) -> dict:
        d = self.disputes.get(dispute_id)
        if d is None:
            raise PayPalError(404, "INVALID_RESOURCE_ID", f"dispute {dispute_id} not found")
        return dict(d)

    def provide_evidence(self, dispute_id: str, *, notes: str, evidence_type: str = "OTHER",
                         attachments: list[tuple[str, bytes, str]] | None = None) -> dict:
        with self._lock:
            d = self.disputes.get(dispute_id)
            if d is None:
                raise PayPalError(404, "INVALID_RESOURCE_ID", f"dispute {dispute_id} not found")
            d["evidence"].append({"evidence_type": evidence_type, "notes": notes, "files": [a[0] for a in attachments or []]})
            d["status"] = "UNDER_REVIEW"
            return {"links": [{"rel": "self", "href": f"/v1/customer/disputes/{dispute_id}"}]}

    def verify_webhook(self, headers: dict[str, str], event: dict) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        return h.get("paypal-transmission-sig") == self.VALID_SIGNATURE
