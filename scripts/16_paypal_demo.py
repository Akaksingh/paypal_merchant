"""End-to-end PayPal demo against a running gateway:

    risk decision -> PayPal order -> buyer approval -> capture -> delivery outcome -> refund (or retained)

    python scripts/serve.py --port 8080          # in one terminal
    python scripts/16_paypal_demo.py             # in another; --rto simulates a returned parcel

In mock mode (no PayPal credentials) the buyer approval is simulated. In sandbox mode the script prints the PayPal
approval link, opens it, and waits for you to approve with a sandbox *personal* account and press Enter.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import webbrowser


def call(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw or b"{}")
            except ValueError:
                return r.status, {"html": raw.decode("utf-8", "replace")[:200]}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"detail": raw.decode("utf-8", "replace")[:200]}


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--scenario", default="step-up", help="scenario tag from /v1/scenarios (step-up -> deposit, prepaid -> full basket)")
    ap.add_argument("--rto", action="store_true", help="simulate the parcel coming back (deposit is retained instead of refunded)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    base = a.url.rstrip("/")

    st, status = call(base, "GET", "/v1/paypal/status")
    if st != 200:
        print(f"gateway not reachable at {base} ({st}). Start it with: python scripts/serve.py --port 8080")
        return 1
    mode = status["mode"]
    print(f"PayPal backend: {mode}  |  charge currency: {status['currency']}  |  INR->USD {status['inr_per_usd']}")

    step(1, "Risk decision (ChakraShield, no PayPal call on this path)")
    scenarios = call(base, "GET", "/v1/scenarios")[1]
    sc = next((s for s in scenarios if s["tag"] == a.scenario), None)
    if sc is None:
        print("unknown scenario; choose from:", sorted({s["tag"] for s in scenarios}))
        return 1
    stamp = int(time.time() * 1000)                     # ms: two quick runs must not reuse an order id (start is idempotent per order)
    for i in range(30):
        oid = f"demo_{stamp}_{i}"
        ev = call(base, "POST", "/v1/risk/evaluate?commit=true", {**sc["req"], "order_id": oid})[1]
        if ev["decision"] == ev["policy_action"]:       # skip the 5 % control band (served frictionless on purpose)
            break
    print(f"order {oid}: P(RTO)={ev['p_loss']:.2f}  conformal set={ev['conformal']['prediction_set']}  ->  {ev['decision']}")
    print(f"  {ev['rationale'][:160]}")
    if ev["decision"] == "ALLOW_COD":
        print("This order is frictionless COD: no payment is created. Try --scenario step-up or prepaid.")
        return 0
    call(base, "GET", "/v1/ledger/stats")               # let the async observer register the order

    step(2, "Create the PayPal order for that decision")
    st, p = call(base, "POST", f"/v1/pay/{oid}/start")
    if st != 200:
        print("start failed:", st, p.get("detail"))
        return 1
    print(f"{p['kind']}: Rs.{p['amount_inr']:.0f}  ->  {p['currency']} {p['amount']}   PayPal order {p['paypal_order_id']}  status {p['status']}")

    step(3, "Buyer approves the payment")
    if mode == "mock":
        call(base, "GET", p["approve_url"].replace(status["public_url"], ""))     # follows the redirect, which captures
        print("(mock) buyer approval simulated")
    else:
        print("Approve here with a sandbox PERSONAL account:\n  " + p["approve_url"])
        if not a.no_browser:
            webbrowser.open(p["approve_url"])
        input("Press Enter once PayPal shows 'Thank you'... ")

    step(4, "Capture")
    st, c = call(base, "POST", f"/v1/pay/{oid}/capture")
    if st != 200:
        print("capture failed:", st, c.get("detail"))
        return 1
    print(f"status {c['status']}   capture id {c['capture_id']}")

    step(5, f"Delivery outcome from the 3PL: {'RTO (parcel returned)' if a.rto else 'delivered'}")
    q = f"/v1/risk/outcome/{oid}?rto={'true' if a.rto else 'false'}" + ("&stepup_result=paid" if p["kind"] == "DEPOSIT" else "&prepaid_result=paid")
    st, out = call(base, "POST", q)
    print("payment action:", json.dumps(out.get("payment")))

    step(6, "Final state")
    final = call(base, "GET", f"/v1/pay/{oid}")[1]
    print(f"status {final['status']}   history: {' -> '.join(h['event'] for h in final['history'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
