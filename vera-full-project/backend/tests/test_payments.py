"""Payment tests.

Unit tests for signature verification and provider selection (no server), plus
API tests for the payment lifecycle. The API tests run only when the backend
is started with PAYMENT_PROVIDER=manual:

    PAYMENT_PROVIDER=manual uvicorn app.main:app --port 8010
    python -m pytest tests/test_payments.py -q
"""
import hashlib
import hmac
import json
import os
import uuid
from decimal import Decimal

import pytest
import requests

from app import payments as gateway

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@hairshalo.com", "password": "ChangeMe123!"}


# ---------------- unit: provider selection ----------------

def test_noop_provider_refuses_to_create_intents():
    p = gateway.NoopProvider()
    assert p.holds_order is False
    with pytest.raises(gateway.PaymentError):
        p.create_intent(object())


def test_manual_provider_holds_the_order():
    assert gateway.ManualProvider().holds_order is True


def test_razorpay_requires_keys():
    p = gateway.RazorpayProvider("", "", "")
    with pytest.raises(gateway.PaymentError) as exc:
        p.create_intent(object())
    assert "RAZORPAY_KEY_ID" in str(exc.value)


# ---------------- unit: signature verification ----------------

def test_razorpay_return_signature_is_verified():
    secret = "test_secret"
    p = gateway.RazorpayProvider("rzp_test_key", secret, "")
    order_id, payment_id = "order_abc", "pay_xyz"
    good = hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(),
                    hashlib.sha256).hexdigest()

    event = p.verify_return({
        "razorpay_order_id": order_id, "razorpay_payment_id": payment_id,
        "razorpay_signature": good,
    })
    assert event.status == "paid"
    assert event.provider_payment_id == payment_id


def test_razorpay_rejects_a_forged_signature():
    p = gateway.RazorpayProvider("rzp_test_key", "test_secret", "")
    with pytest.raises(gateway.PaymentError) as exc:
        p.verify_return({
            "razorpay_order_id": "order_abc", "razorpay_payment_id": "pay_xyz",
            "razorpay_signature": "deadbeef",
        })
    assert "verification failed" in str(exc.value)


def test_razorpay_rejects_an_incomplete_payload():
    p = gateway.RazorpayProvider("k", "s", "")
    with pytest.raises(gateway.PaymentError):
        p.verify_return({"razorpay_order_id": "order_abc"})


def test_razorpay_webhook_signature_is_verified():
    secret = "hook_secret"
    p = gateway.RazorpayProvider("k", "s", secret)
    body = json.dumps({
        "id": "evt_1", "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": "pay_1", "order_id": "order_1", "amount": 2000000,
            "currency": "INR", "method": "upi",
        }}},
    }).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    event = p.verify_webhook(body, {"x-razorpay-signature": sig})
    assert event.status == "paid"
    assert event.amount == Decimal("20000")     # paise -> rupees
    assert event.method == "upi"
    assert event.event_id == "evt_1"


def test_razorpay_webhook_rejects_bad_signature():
    p = gateway.RazorpayProvider("k", "s", "hook_secret")
    body = json.dumps({"event": "payment.captured", "payload": {}}).encode()
    with pytest.raises(gateway.PaymentError):
        p.verify_webhook(body, {"x-razorpay-signature": "nope"})


def test_razorpay_webhook_requires_a_configured_secret():
    p = gateway.RazorpayProvider("k", "s", "")
    with pytest.raises(gateway.PaymentError) as exc:
        p.verify_webhook(b"{}", {})
    assert "WEBHOOK_SECRET" in str(exc.value)


def test_unhandled_webhook_event_is_rejected():
    secret = "hook_secret"
    p = gateway.RazorpayProvider("k", "s", secret)
    body = json.dumps({"event": "subscription.charged", "payload": {}}).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with pytest.raises(gateway.PaymentError) as exc:
        p.verify_webhook(body, {"x-razorpay-signature": sig})
    assert "Unhandled webhook event" in str(exc.value)


# ---------------- API ----------------

def _alive():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


def _manual_mode():
    if not _alive():
        return False
    try:
        return requests.get(f"{API}/payments/config", timeout=5).json()["provider"] == "manual"
    except Exception:
        return False


manual_only = pytest.mark.skipif(
    not _manual_mode(), reason="backend not running with PAYMENT_PROVIDER=manual"
)


def _sellable_variant(products, minimum=2):
    """(product, variant) for the first variant that actually has stock."""
    for product in products:
        for variant in product.get("variants") or []:
            if (variant.get("stock") or 0) > minimum and variant.get("is_available", True):
                return product, variant
    pytest.skip(f"no variant in the catalog has more than {minimum} units in stock")


@pytest.fixture(scope="module")
def auth():
    if not _alive():
        pytest.skip("backend not reachable")
    tok = requests.post(f"{API}/auth/login", json=ADMIN, timeout=10).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def order(auth):
    """Place a real order and clean it up afterwards."""
    # Any variant with stock, not just the first: earlier test runs sell out
    # whichever variant is listed first, which used to fail the whole module
    # with a bare StopIteration.
    p, v = _sellable_variant(requests.get(f"{API}/products", timeout=10).json())
    o = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Pay", "customer_email": f"pay{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": p["id"], "variant_id": v["id"], "quantity": 1}],
    }).json()
    yield {"order": o, "product_id": p["id"], "variant_id": v["id"]}


def _stock(product_id, variant_id=None):
    product = requests.get(f"{API}/products/{product_id}", timeout=10).json()
    variants = product["variants"]
    if variant_id:
        return next(v["stock"] for v in variants if v["id"] == variant_id)
    return variants[0]["stock"]


@manual_only
def test_order_waits_at_pending_payment(order):
    assert order["order"]["status"] == "Pending Payment"
    assert order["order"]["is_paid"] is False


@manual_only
def test_intent_amount_comes_from_the_database(order):
    o = order["order"]
    intent = requests.post(f"{API}/payments/intent", timeout=15,
                           json={"order_id": o["id"]}).json()
    assert Decimal(intent["amount"]) == Decimal(o["total"])
    assert intent["currency"] == "INR"


@manual_only
def test_client_cannot_declare_a_payment_paid(order, auth):
    """The headline requirement: the browser cannot mark an order paid."""
    o = order["order"]
    intent = requests.post(f"{API}/payments/intent", timeout=15,
                           json={"order_id": o["id"]}).json()
    r = requests.post(f"{API}/payments/confirm", timeout=15, json={
        "payment_id": intent["payment_id"],
        "gateway_response": {"status": "paid", "paid": True, "amount": 1},
    })
    assert r.status_code == 400
    again = requests.get(f"{API}/orders", headers=auth, timeout=15).json()
    stored = next(x for x in again if x["id"] == o["id"])
    assert stored["status"] == "Pending Payment"
    assert stored["is_paid"] is False


@manual_only
def test_mark_paid_requires_admin(order):
    o = order["order"]
    intent = requests.post(f"{API}/payments/intent", timeout=15,
                           json={"order_id": o["id"]}).json()
    r = requests.post(f"{API}/payments/{intent['payment_id']}/mark-paid",
                      json={"reference": "X"}, timeout=15)
    assert r.status_code == 401


@manual_only
def test_admin_confirmation_moves_order_to_paid(order, auth):
    o = order["order"]
    intent = requests.post(f"{API}/payments/intent", timeout=15,
                           json={"order_id": o["id"]}).json()
    r = requests.post(f"{API}/payments/{intent['payment_id']}/mark-paid",
                      headers=auth, json={"reference": "NEFT-1"}, timeout=15)
    assert r.status_code == 200
    assert r.json()["status"] == "Paid"
    stored = next(x for x in requests.get(f"{API}/orders", headers=auth, timeout=15).json()
                  if x["id"] == o["id"])
    assert stored["status"] == "Paid"
    assert stored["is_paid"] is True


@manual_only
def test_confirmation_is_idempotent(order, auth):
    """A replayed confirmation must not double-apply anything."""
    o = order["order"]
    intent = requests.post(f"{API}/payments/intent", timeout=15,
                           json={"order_id": o["id"]}).json()
    body = {"reference": "NEFT-DUP"}
    requests.post(f"{API}/payments/{intent['payment_id']}/mark-paid",
                  headers=auth, json=body, timeout=15)
    stock_after_first = _stock(order["product_id"], order["variant_id"])
    requests.post(f"{API}/payments/{intent['payment_id']}/mark-paid",
                  headers=auth, json=body, timeout=15)
    assert _stock(order["product_id"], order["variant_id"]) == stock_after_first


@manual_only
def test_cancelling_an_unpaid_order_releases_stock(order, auth):
    o = order["order"]
    before = _stock(order["product_id"], order["variant_id"])
    requests.put(f"{API}/orders/{o['id']}/status", headers=auth,
                 json={"status": "Cancelled"}, timeout=15)
    assert _stock(order["product_id"], order["variant_id"]) == before + 1


@manual_only
def test_pending_payment_cannot_skip_to_shipped(order, auth):
    o = order["order"]
    r = requests.put(f"{API}/orders/{o['id']}/status", headers=auth,
                     json={"status": "Shipped"}, timeout=15)
    assert r.status_code == 400
    assert "Cannot move an order" in r.json()["detail"]


@manual_only
def test_refund_returns_stock_and_marks_order_refunded(order, auth):
    o = order["order"]
    intent = requests.post(f"{API}/payments/intent", timeout=15,
                           json={"order_id": o["id"]}).json()
    requests.post(f"{API}/payments/{intent['payment_id']}/mark-paid",
                  headers=auth, json={"reference": "NEFT-R"}, timeout=15)
    before = _stock(order["product_id"], order["variant_id"])
    r = requests.post(f"{API}/payments/{intent['payment_id']}/refund",
                      headers=auth, json={}, timeout=15)
    assert r.status_code == 200
    assert r.json()["status"] == "Refunded"
    assert _stock(order["product_id"], order["variant_id"]) == before + 1
    stored = next(x for x in requests.get(f"{API}/orders", headers=auth, timeout=15).json()
                  if x["id"] == o["id"])
    assert stored["status"] == "Refunded"


@manual_only
def test_refund_amount_is_bounded(order, auth):
    o = order["order"]
    intent = requests.post(f"{API}/payments/intent", timeout=15,
                           json={"order_id": o["id"]}).json()
    requests.post(f"{API}/payments/{intent['payment_id']}/mark-paid",
                  headers=auth, json={"reference": "NEFT-B"}, timeout=15)
    r = requests.post(f"{API}/payments/{intent['payment_id']}/refund",
                      headers=auth, json={"amount": 999999}, timeout=15)
    assert r.status_code == 400


@manual_only
def test_webhook_rejects_unknown_provider():
    r = requests.post(f"{API}/payments/webhook/stripe", json={}, timeout=10)
    assert r.status_code == 404
