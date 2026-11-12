"""PayPal integration: request shapes of the real client (no network), the mock's state machine, the money loop,
webhooks, dispute evidence, and the gateway routes end to end in mock mode."""
import json

import httpx
import pytest

from chakrashield.config import MODEL_DIR
from chakrashield.dispute.ce3 import TransactionLedger, TxnRecord
from chakrashield.paypal import MockPayPal, PayPalClient, PayPalError, PaymentError, PaymentService
from chakrashield.paypal.service import charge_amount

DAY = 86400.0
T = 1_750_000_000.0


# --------------------------------------------------------------------------- real client request shapes
def _real_client(seen: list, webhook_id="WH-1"):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        p = req.url.path
        if p == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})
        if p == "/v2/checkout/orders":
            return httpx.Response(201, json={"id": "ORD1", "status": "PAYER_ACTION_REQUIRED",
                                             "links": [{"rel": "self", "href": "x"}, {"rel": "payer-action", "href": "https://pp/approve"}]})
        if p == "/v2/checkout/orders/ORD1/capture":
            return httpx.Response(201, json={"status": "COMPLETED", "purchase_units": [{"payments": {"captures": [
                {"id": "CAP1", "status": "COMPLETED", "amount": {"value": "0.58"}}]}}]})
        if p == "/v2/payments/captures/CAP1/refund":
            return httpx.Response(201, json={"id": "REF1", "status": "COMPLETED"})
        if p == "/v1/notifications/verify-webhook-signature":
            return httpx.Response(200, json={"verification_status": "SUCCESS"})
        if p.endswith("/provide-evidence"):
            return httpx.Response(200, json={"links": []})
        if p == "/v2/checkout/orders/BAD/capture":
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "message": "x", "debug_id": "d1",
                                             "details": [{"issue": "ORDER_NOT_APPROVED", "description": "d"}]})
        return httpx.Response(404, json={"name": "NOT_FOUND"})
    return PayPalClient("cid", "sec", webhook_id=webhook_id, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_real_client_create_order_shape_and_token_reuse():
    seen: list[httpx.Request] = []
    c = _real_client(seen)
    out = c.create_order(amount="0.58", currency="USD", reference_id="o1", custom_id="o1", description="d", request_id="rid-1",
                         return_url="https://me/return", cancel_url="https://me/cancel")
    assert out == {"id": "ORD1", "status": "PAYER_ACTION_REQUIRED", "approve_url": "https://pp/approve"}
    tok, order = seen[0], seen[1]
    assert tok.headers["authorization"].startswith("Basic ")
    assert order.headers["authorization"] == "Bearer tok-123" and order.headers["paypal-request-id"] == "rid-1"
    body = json.loads(order.content)
    assert body["intent"] == "CAPTURE"
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "0.58"}
    assert body["purchase_units"][0]["custom_id"] == "o1"
    assert body["payment_source"]["paypal"]["experience_context"]["return_url"] == "https://me/return"
    c.capture_order("ORD1", request_id="rid-2")
    assert sum(r.url.path == "/v1/oauth2/token" for r in seen) == 1      # token cached across calls


def test_real_client_capture_refund_and_errors():
    seen: list[httpx.Request] = []
    c = _real_client(seen)
    cap = c.capture_order("ORD1", request_id="r")
    assert cap["capture_id"] == "CAP1" and cap["capture_status"] == "COMPLETED" and cap["amount"] == "0.58"
    creq = [r for r in seen if r.url.path.endswith("/capture")][0]
    assert creq.headers["content-type"] == "application/json" and creq.content == b"{}"     # the real sandbox 415s without it
    ref = c.refund_capture("CAP1", amount=None, note="thanks", request_id="r2", invoice_id="inv")
    assert ref == {"id": "REF1", "status": "COMPLETED"}
    rbody = json.loads([r for r in seen if r.url.path.endswith("/refund")][0].content)
    assert "amount" not in rbody and rbody["note_to_payer"] == "thanks" and rbody["invoice_id"] == "inv"      # full refund
    with pytest.raises(PayPalError) as e:
        c.capture_order("BAD", request_id="r3")
    assert e.value.status == 422 and e.value.debug_id == "d1" and e.value.issue == "ORDER_NOT_APPROVED"
    # the real API reports "not approved" as name=UNPROCESSABLE_ENTITY + details[0].issue; the service must map it to 409
    svc = PaymentService(c, public_url="")
    svc.start("BAD-ORDER", "STEP_UP_DEPOSIT", 1000.0, 49.0)
    svc.get("BAD-ORDER").paypal_order_id = "BAD"
    with pytest.raises(PaymentError) as e2:
        svc.capture("BAD-ORDER")
    assert e2.value.status == 409


def test_real_client_provide_evidence_is_multipart_with_json_input():
    seen: list[httpx.Request] = []
    _real_client(seen).provide_evidence("PP-D-1", notes="hello", attachments=[("a.txt", b"data", "text/plain")])
    req = seen[-1]
    assert req.url.path == "/v1/customer/disputes/PP-D-1/provide-evidence"
    assert req.headers["content-type"].startswith("multipart/form-data")
    raw = req.content.decode("latin-1")
    assert 'name="input"' in raw and '"evidence_type": "OTHER"' in raw and "hello" in raw and 'filename="a.txt"' in raw


def test_real_client_webhook_verification_body():
    seen: list[httpx.Request] = []
    c = _real_client(seen)
    hdr = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://c", "PAYPAL-TRANSMISSION-ID": "t1",
           "PAYPAL-TRANSMISSION-SIG": "sig", "PAYPAL-TRANSMISSION-TIME": "2026-01-01T00:00:00Z"}
    assert c.verify_webhook(hdr, {"id": "E1"}) is True
    body = json.loads(seen[-1].content)
    assert body["webhook_id"] == "WH-1" and body["transmission_sig"] == "sig" and body["webhook_event"] == {"id": "E1"}
    assert c.verify_webhook({}, {"id": "E1"}) is False                       # missing headers never reach PayPal
    assert _real_client([], webhook_id=None).verify_webhook(hdr, {}) is False  # no webhook id configured -> reject


# --------------------------------------------------------------------------- mock state machine
def test_mock_refuses_capture_before_approval_and_is_idempotent():
    m = MockPayPal()
    o = m.create_order(amount="0.58", reference_id="o", custom_id="o", description="d", request_id="k1")
    assert m.create_order(amount="0.58", reference_id="o", custom_id="o", description="d", request_id="k1") == o
    with pytest.raises(PayPalError) as e:
        m.capture_order(o["id"], request_id="c1")
    assert e.value.name == "ORDER_NOT_APPROVED"
    m.approve(o["id"])
    cap = m.capture_order(o["id"], request_id="c2")
    assert m.capture_order(o["id"], request_id="c2") == cap                     # same request id: replay, not a second capture
    m.refund_capture(cap["capture_id"], amount=None, note="n", request_id="r1")
    with pytest.raises(PayPalError) as e2:
        m.refund_capture(cap["capture_id"], amount=None, note="n", request_id="r2")
    assert e2.value.name == "CAPTURE_FULLY_REFUNDED"


# --------------------------------------------------------------------------- money loop
def _svc(ledger=None):
    return PaymentService(MockPayPal(public_base="http://gw"), inr_per_usd=85.0, public_url="http://gw", ledger=ledger)


def _pay_deposit(svc, oid="o1"):
    p = svc.start(oid, "STEP_UP_DEPOSIT", 1500.0, 49.0)
    svc.client.approve(p.paypal_order_id)
    return svc.capture(oid)


def test_charge_amount_inr_as_is_and_usd_converted():
    assert charge_amount(49, "INR", 85) == "49.00" and charge_amount(4999, "INR", 85) == "4999.00"
    assert charge_amount(49, "USD", 85) == "0.58" and charge_amount(1500, "USD", 85) == "17.65"


def test_deposit_is_refunded_when_delivered():
    svc = _svc()
    p = _pay_deposit(svc)
    assert p.kind == "DEPOSIT" and p.amount == "0.58" and p.currency == "USD" and p.status == "CAPTURED"
    out = svc.settle("o1", rto=False)
    assert out["action"] == "refunded" and svc.get("o1").status == "REFUNDED"
    assert svc.settle("o1", rto=False)["action"] == "none"                         # no second refund
    assert len(svc.client.refunds) == 1


def test_deposit_is_retained_on_rto():
    svc = _svc()
    _pay_deposit(svc)
    assert svc.settle("o1", rto=True)["action"] == "retained"
    assert svc.get("o1").status == "RETAINED" and not svc.client.refunds


def test_unpaid_deposit_is_not_settled_and_prepaid_is_left_to_merchant():
    svc = _svc()
    svc.start("o1", "STEP_UP_DEPOSIT", 1500.0, 49.0)                                # created, never approved
    assert svc.settle("o1", rto=False)["reason"] == "deposit was never paid"
    p = svc.start("o2", "FORCE_PREPAID", 1500.0, 49.0)
    assert p.kind == "PREPAID" and p.amount == "17.65"
    assert svc.settle("o2", rto=True)["action"] == "none"


def test_allow_needs_no_payment_and_capture_before_approval_is_409():
    svc = _svc()
    with pytest.raises(PaymentError) as e:
        svc.start("o1", "ALLOW_COD", 1500.0, 49.0)
    assert e.value.status == 409
    svc.start("o2", "STEP_UP_DEPOSIT", 1500.0, 49.0)
    with pytest.raises(PaymentError) as e2:
        svc.capture("o2")
    assert e2.value.status == 409
    assert svc.start("o2", "STEP_UP_DEPOSIT", 1500.0, 49.0) is svc.get("o2")      # start is idempotent


# --------------------------------------------------------------------------- webhooks
SIG = {"PAYPAL-TRANSMISSION-SIG": MockPayPal.VALID_SIGNATURE}


def test_webhook_rejects_bad_signature():
    with pytest.raises(PaymentError) as e:
        _svc().handle_webhook({"PAYPAL-TRANSMISSION-SIG": "forged"}, {"id": "E1", "event_type": "CHECKOUT.ORDER.APPROVED"})
    assert e.value.status == 401


def test_approval_webhook_captures_and_duplicates_are_ignored():
    svc = _svc()
    p = svc.start("o1", "STEP_UP_DEPOSIT", 1500.0, 49.0)
    svc.client.approve(p.paypal_order_id)
    ev = {"id": "E1", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": p.paypal_order_id}}
    out = svc.handle_webhook(SIG, ev)
    assert out["handled"] and svc.get("o1").status == "CAPTURED"
    assert svc.handle_webhook(SIG, ev) == {"handled": False, "duplicate": True, "event_type": "CHECKOUT.ORDER.APPROVED"}


def test_refund_and_dispute_webhooks_update_state():
    svc = _svc()
    p = _pay_deposit(svc)
    svc.handle_webhook(SIG, {"id": "E2", "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {"id": p.capture_id, "custom_id": "o1"}})
    assert svc.get("o1").status == "REFUNDED"
    svc.handle_webhook(SIG, {"id": "E3", "event_type": "CUSTOMER.DISPUTE.CREATED",
                             "resource": {"dispute_id": "PP-D-9", "status": "OPEN", "reason": "UNAUTHORISED"}})
    assert svc.disputes_seen["PP-D-9"]["reason"] == "UNAUTHORISED"


# --------------------------------------------------------------------------- dispute evidence
def _ledger(eligible: bool):
    def rec(tid, ts):
        return TxnRecord(transaction_id=tid, ts=ts, card_token="tok1", ip_hash="ipA", device_hash="devA", addr_hash="addrA",
                         account_id="accA", amount=999.0, items="1 item", disputed=False, delivered=True)
    led = TransactionLedger()
    led.add(rec("disputed", T))
    led.add(rec("p1", T + 45 * DAY - 150 * DAY))
    if eligible:
        led.add(rec("p2", T + 45 * DAY - 200 * DAY))
    return led


def test_eligible_packet_is_submitted_with_hash_in_notes():
    svc = _svc(_ledger(True))
    out = svc.submit_dispute_evidence("PP-D-MOCK-1001", "disputed")
    assert out["submitted"] and out["eligible"] and len(out["packet_hash"]) == 64
    ev = svc.client.disputes["PP-D-MOCK-1001"]
    assert ev["status"] == "UNDER_REVIEW" and out["packet_hash"] in ev["evidence"][0]["notes"]
    assert "http://gw/v1/dispute/packet/disputed.html" in ev["evidence"][0]["notes"]


def test_ineligible_packet_is_not_submitted_by_default():
    svc = _svc(_ledger(False))
    out = svc.submit_dispute_evidence("PP-D-MOCK-1001", "disputed")
    assert out["submitted"] is False and not out["eligible"]
    assert svc.client.disputes["PP-D-MOCK-1001"]["status"] == "WAITING_FOR_SELLER_RESPONSE"
    assert svc.submit_dispute_evidence("PP-D-MOCK-1001", "disputed", only_if_eligible=False)["submitted"] is True


def test_unknown_transaction_is_404():
    with pytest.raises(PaymentError) as e:
        _svc(_ledger(True)).submit_dispute_evidence("PP-D-MOCK-1001", "nope")
    assert e.value.status == 404


# --------------------------------------------------------------------------- gateway routes (mock mode)
needs_model = pytest.mark.skipif(not (MODEL_DIR / "chakra_rto.txt.json").exists(), reason="train the model first")


@pytest.fixture(scope="module")
def client(monkeypatch_module):
    from fastapi.testclient import TestClient
    from chakrashield.serving.app import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    mp.setenv("PAYPAL_MODE", "mock")
    mp.delenv("PAYPAL_CLIENT_ID", raising=False)
    yield mp
    mp.undo()


def _served(client, tag, prefix):
    """Evaluate a scenario with commit=true under fresh order ids until it is served its policy action
    (the 5 % control band deliberately serves some flagged orders frictionless)."""
    sc = next(s for s in client.get("/v1/scenarios").json() if s["tag"] == tag)
    for i in range(40):
        oid = f"{prefix}_{i}"
        body = client.post("/v1/risk/evaluate?commit=true", json={**sc["req"], "order_id": oid}).json()
        if body["decision"] == body["policy_action"]:
            client.get("/v1/ledger/stats")                                      # drains the async observer
            return oid, body
    pytest.fail("every order landed in the control cohort")


@needs_model
def test_gateway_deposit_is_refunded_when_delivered(client):
    assert client.get("/v1/paypal/status").json()["mode"] == "mock"
    oid, body = _served(client, "step-up", "pp_dep")
    assert body["decision"] == "STEP_UP_DEPOSIT"
    started = client.post(f"/v1/pay/{oid}/start").json()
    assert started["kind"] == "DEPOSIT" and started["amount_inr"] == 49.0 and started["amount"] == "0.58" and started["currency"] == "USD"
    assert started["status"] == "CREATED" and started["mode"] == "mock"
    assert client.post(f"/v1/pay/{oid}/capture").status_code == 409                # buyer has not approved
    r = client.get(f"/v1/paypal/mock/approve/{started['paypal_order_id']}", follow_redirects=True)
    assert r.status_code == 200 and oid in r.text
    assert client.get(f"/v1/pay/{oid}").json()["status"] == "CAPTURED"
    out = client.post(f"/v1/risk/outcome/{oid}?rto=false&stepup_result=paid").json()
    assert out["payment"]["action"] == "refunded"
    assert client.get(f"/v1/pay/{oid}").json()["status"] == "REFUNDED"


@needs_model
def test_gateway_deposit_is_retained_on_rto_and_prepaid_charges_the_basket(client):
    oid, _ = _served(client, "step-up", "pp_rto")
    p = client.post(f"/v1/pay/{oid}/start").json()
    client.get(f"/v1/paypal/mock/approve/{p['paypal_order_id']}", follow_redirects=True)
    assert client.post(f"/v1/risk/outcome/{oid}?rto=true&stepup_result=paid").json()["payment"]["action"] == "retained"
    oid2, body2 = _served(client, "prepaid", "pp_pre")
    assert body2["decision"] == "FORCE_PREPAID"
    p2 = client.post(f"/v1/pay/{oid2}/start").json()
    assert p2["kind"] == "PREPAID" and p2["amount_inr"] > 49 and float(p2["amount"]) > 1


@needs_model
def test_gateway_frictionless_order_needs_no_payment(client):
    sc = next(s for s in client.get("/v1/scenarios").json() if s["tag"] == "frictionless")
    client.post("/v1/risk/evaluate?commit=true", json={**sc["req"], "order_id": "pp_allow_1"})
    client.get("/v1/ledger/stats")
    assert client.post("/v1/pay/pp_allow_1/start").status_code == 409


@needs_model
def test_gateway_unknown_order_and_webhook_rejection(client):
    assert client.post("/v1/pay/never_seen/start").status_code == 404
    assert client.post("/v1/paypal/webhook", json={"id": "x", "event_type": "CHECKOUT.ORDER.APPROVED"}).status_code == 401
    assert client.post("/v1/paypal/webhook", content=b"not json").status_code == 400
    d = client.get("/v1/paypal/disputes").json()
    assert d["mode"] == "mock" and len(d["items"]) >= 1
