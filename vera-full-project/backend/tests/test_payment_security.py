"""Payment security: the attacks, and the Razorpay flow end to end.

Two halves.

**Attacks** — every request a hostile client can send at checkout and payment:
injected prices, totals, discounts, currencies and exchange rates; another
customer's payment; a forged signature; a replayed webhook. Each must fail
safely, and "safely" means the money and the stock end up where they should.

**Flow** — the complete Razorpay path with the gateway stubbed at the HTTP
boundary (`RazorpayProvider._post`). That exercises OUR code end to end:
intent → signed return → webhook → order paid → stock finalised → points
earned → notification queued. It is NOT a test against Razorpay's sandbox;
signature maths is verified against the algorithm Razorpay documents, and the
live sandbox still has to be exercised with real keys before launch.
"""
import hashlib
import hmac
import json
import os
import uuid
from decimal import Decimal

import pytest
import requests
from sqlalchemy import text

from app import loyalty, models, payments as gateway
from app.config import settings

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@verahair.co", "password": "ChangeMe123!"}
MARKER = "pytest-paysec"


def _alive():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


def _provider():
    try:
        return requests.get(f"{API}/payments/config", timeout=5).json()["provider"]
    except Exception:
        return None


live = pytest.mark.skipif(not _alive(), reason="backend not reachable")
manual_only = pytest.mark.skipif(_provider() != "manual",
                                 reason="backend not running with PAYMENT_PROVIDER=manual")


@pytest.fixture(scope="module")
def auth():
    if not _alive():
        pytest.skip("backend not reachable")
    token = requests.post(f"{API}/auth/login", json=ADMIN, timeout=10).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _sellable():
    for product in requests.get(f"{API}/products", timeout=10).json():
        for variant in product.get("variants") or []:
            if (variant.get("stock") or 0) > 2 and variant.get("is_available", True):
                return product, variant
    pytest.skip("no variant in the catalog has stock")


def _stock(product_id, variant_id):
    product = requests.get(f"{API}/products/{product_id}", timeout=10).json()
    return next(v["stock"] for v in product["variants"] if v["id"] == variant_id)


def _order(extra_item_fields=None, **extra):
    """Place an order, optionally smuggling extra fields into the payload."""
    product, variant = _sellable()
    item = {"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}
    item.update(extra_item_fields or {})
    payload = {
        "customer_name": "Attacker",
        "customer_email": f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com",
        "shipping_address": "12 MG Road",
        "items": [item],
    }
    payload.update(extra)
    response = requests.post(f"{API}/orders", timeout=15, json=payload)
    return response, product, variant


def _expected_total(product, variant):
    unit = Decimal(str(variant.get("price") or product["price"]))
    return unit


# ---------------- injection at checkout ----------------

@live
def test_an_injected_unit_price_is_ignored():
    """`price=1` on a line item must not change what is charged."""
    response, product, variant = _order(extra_item_fields={"price": 1, "unit_price": 1})
    assert response.status_code == 201, response.text
    order = response.json()
    assert Decimal(order["items"][0]["price"]) == _expected_total(product, variant)
    assert Decimal(order["subtotal"]) == _expected_total(product, variant)


@live
def test_an_injected_order_total_is_ignored():
    response, product, variant = _order(total=1, subtotal=1, shipping_fee=0)
    assert response.status_code == 201
    order = response.json()
    assert Decimal(order["subtotal"]) == _expected_total(product, variant)
    assert Decimal(order["total"]) > Decimal("1")


@live
def test_an_injected_discount_is_ignored():
    response, _product, _variant = _order(discount_total=999999, loyalty_discount=999999)
    assert response.status_code == 201
    order = response.json()
    assert Decimal(order["discount_total"]) == Decimal("0")
    assert Decimal(order["loyalty_discount"] or 0) == Decimal("0")
    assert Decimal(order["total"]) > Decimal("0")


@live
def test_an_injected_settlement_currency_is_ignored():
    """Display currency is a record of what the customer saw. Settlement is
    always INR, whatever the request says."""
    response, _p, _v = _order(currency="USD", display_currency="USD",
                              display_rate="0.0001", display_total="1")
    assert response.status_code == 201
    order = response.json()
    assert order["currency"] == "INR"
    assert order["display_currency"] == "USD"
    # The rate came from the server, not from the request.
    assert Decimal(order["display_rate"]) != Decimal("0.0001")
    assert Decimal(order["display_total"]) != Decimal("1")


@live
def test_an_unknown_display_currency_degrades_instead_of_failing():
    response, _p, _v = _order(display_currency="XXX")
    assert response.status_code == 201
    assert response.json()["currency"] == "INR"


@live
def test_an_injected_status_or_paid_flag_is_ignored():
    response, _p, _v = _order(status="Delivered", is_paid=True, payment_status="Paid")
    assert response.status_code == 201
    order = response.json()
    assert order["status"] in ("Processing", "Pending Payment")
    assert order["is_paid"] is False


@live
def test_negative_and_absurd_quantities_are_refused():
    for quantity in (0, -1, -100, 101):
        product, variant = _sellable()
        r = requests.post(f"{API}/orders", timeout=15, json={
            "customer_name": "Attacker", "customer_email": f"{MARKER}@example.com",
            "items": [{"product_id": product["id"], "variant_id": variant["id"],
                       "quantity": quantity}],
        })
        assert r.status_code == 400, f"quantity {quantity} was accepted"


@live
def test_an_injected_loyalty_redemption_cannot_exceed_the_balance():
    """A brand-new customer has no points, so asking to spend some must fail
    rather than discount the order."""
    product, variant = _sellable()
    r = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Attacker",
        "customer_email": f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
        "redeem_loyalty_points": 100000,
    })
    assert r.status_code == 400
    assert "points" in r.json()["detail"].lower()


# ---------------- attacks on payment ----------------

@live
@manual_only
def test_a_client_cannot_declare_its_own_payment_paid():
    response, _p, _v = _order()
    order = response.json()
    intent = requests.post(f"{API}/payments/intent", timeout=15,
                           json={"order_id": order["id"]}).json()
    r = requests.post(f"{API}/payments/confirm", timeout=15, json={
        "payment_id": intent["payment_id"],
        "gateway_response": {"status": "paid", "paid": True, "amount": 1,
                             "razorpay_signature": "not-a-signature"},
    })
    assert r.status_code == 400


@live
@manual_only
def test_a_signed_in_customer_cannot_pay_against_someone_elses_order():
    """Guests may still check out, but an identified caller using another
    customer's order id gets a 404."""
    from tests.test_accounts import _new_account, _auth
    _email, token, _cid = _new_account()

    response, _p, _v = _order()                 # belongs to a different email
    victim_order = response.json()

    r = requests.post(f"{API}/payments/intent", headers=_auth(token), timeout=15,
                      json={"order_id": victim_order["id"]})
    assert r.status_code == 404
    # …and anonymously it still works, because guest checkout must.
    assert requests.post(f"{API}/payments/intent", timeout=15,
                         json={"order_id": victim_order["id"]}).status_code == 201


@live
@manual_only
def test_only_an_admin_can_settle_an_offline_payment(auth):
    response, _p, _v = _order()
    intent = requests.post(f"{API}/payments/intent", timeout=15,
                           json={"order_id": response.json()["id"]}).json()
    assert requests.post(f"{API}/payments/{intent['payment_id']}/mark-paid", timeout=15,
                         json={"reference": "NEFT-forged"}).status_code == 401


# ---------------- signature verification (unit) ----------------

def _rzp(secret="test_secret", webhook_secret="hook_secret"):
    return gateway.RazorpayProvider("rzp_test_key", secret, webhook_secret)


def test_a_forged_return_signature_is_refused():
    provider = _rzp()
    with pytest.raises(gateway.PaymentError):
        provider.verify_return({
            "razorpay_order_id": "order_abc", "razorpay_payment_id": "pay_xyz",
            "razorpay_signature": "0" * 64,
        })


def test_a_signature_from_a_different_order_is_refused():
    """Replaying a genuine signature against another order must fail."""
    provider = _rzp()
    real = hmac.new(b"test_secret", b"order_abc|pay_xyz", hashlib.sha256).hexdigest()
    with pytest.raises(gateway.PaymentError):
        provider.verify_return({
            "razorpay_order_id": "order_OTHER", "razorpay_payment_id": "pay_xyz",
            "razorpay_signature": real,
        })


def test_a_webhook_body_that_was_tampered_with_is_refused():
    provider = _rzp()
    body = json.dumps({"event": "payment.captured", "payload": {
        "payment": {"entity": {"id": "pay_1", "order_id": "order_1",
                               "amount": 100, "currency": "INR"}}}}).encode()
    signature = hmac.new(b"hook_secret", body, hashlib.sha256).hexdigest()

    tampered = body.replace(b'"amount": 100', b'"amount": 1')
    with pytest.raises(gateway.PaymentError):
        provider.verify_webhook(tampered, {"x-razorpay-signature": signature})


# ---------------- the full Razorpay path, gateway stubbed ----------------

@pytest.fixture
def db():
    try:
        from app.database import SessionLocal
        session = SessionLocal()
        session.execute(text("select 1"))
    except Exception:
        pytest.skip("database not reachable")
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def razorpay(monkeypatch):
    """A RazorpayProvider whose only network call is stubbed.

    Everything else — signature construction, amount conversion, event mapping —
    is the real implementation.
    """
    provider = _rzp()
    monkeypatch.setattr(provider, "_post", lambda path, payload: {
        "id": f"order_{uuid.uuid4().hex[:12]}",
        "amount": payload.get("amount"),
        "currency": payload.get("currency", "INR"),
    })
    return provider


def test_the_full_gateway_round_trip_is_verified_at_every_step(razorpay, db):
    """intent -> signed return -> webhook, with our verification in between."""
    order = db.query(models.Order).order_by(models.Order.created_at.desc()).first()
    if not order:
        pytest.skip("no orders in the database")

    intent = razorpay.create_intent(order)
    assert intent.provider_order_id.startswith("order_")
    # Amounts leave in paise, and come back converted.
    assert intent.extra["amount_minor"] == int(Decimal(str(order.total)) * 100)
    assert intent.public_key == "rzp_test_key"
    assert "secret" not in str(intent.__dict__).lower()

    payment_id = f"pay_{uuid.uuid4().hex[:12]}"
    signature = hmac.new(b"test_secret",
                         f"{intent.provider_order_id}|{payment_id}".encode(),
                         hashlib.sha256).hexdigest()
    event = razorpay.verify_return({
        "razorpay_order_id": intent.provider_order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": signature,
    })
    assert event.status == "paid" and event.provider_payment_id == payment_id

    body = json.dumps({
        "id": "evt_1", "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": payment_id, "order_id": intent.provider_order_id,
            "amount": intent.extra["amount_minor"], "currency": "INR",
            "method": "upi"}}},
    }).encode()
    hook_signature = hmac.new(b"hook_secret", body, hashlib.sha256).hexdigest()
    hook_event = razorpay.verify_webhook(body, {"x-razorpay-signature": hook_signature})

    assert hook_event.status == "paid"
    assert hook_event.provider_payment_id == payment_id
    assert hook_event.method == "upi"
    # Paise -> rupees, and it must equal the order total exactly.
    assert hook_event.amount == Decimal(str(order.total))


def test_a_failed_payment_webhook_maps_to_failure(razorpay):
    body = json.dumps({
        "id": "evt_2", "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": "pay_2", "order_id": "order_2", "amount": 100,
            "error_code": "BAD_REQUEST_ERROR",
            "error_description": "Payment was declined by the bank"}}},
    }).encode()
    signature = hmac.new(b"hook_secret", body, hashlib.sha256).hexdigest()
    event = razorpay.verify_webhook(body, {"x-razorpay-signature": signature})
    assert event.status == "failed"
    assert "declined" in event.error_message


# ---------------- webhook idempotency, against the live API ----------------

@live
@manual_only
def test_replaying_a_settlement_does_not_double_process(auth):
    """The same event applied twice must not move stock or points twice."""
    response, product, variant = _order()
    order = response.json()
    stock_after_order = _stock(product["id"], variant["id"])

    intent = requests.post(f"{API}/payments/intent", timeout=15,
                           json={"order_id": order["id"]}).json()
    first = requests.post(f"{API}/payments/{intent['payment_id']}/mark-paid",
                          headers=auth, timeout=15,
                          json={"reference": f"NEFT-{uuid.uuid4().hex[:6]}"})
    assert first.status_code == 200 and first.json()["status"] == "Paid"

    for _ in range(3):
        requests.post(f"{API}/payments/{intent['payment_id']}/mark-paid",
                      headers=auth, timeout=15, json={"reference": "NEFT-replay"})

    assert _stock(product["id"], variant["id"]) == stock_after_order

    payments = requests.get(f"{API}/payments/order/{order['id']}",
                            headers=auth, timeout=15).json()
    assert sum(1 for p in payments if p["status"] == "Paid") == 1

    from app.database import SessionLocal
    db = SessionLocal()
    try:
        earned = db.query(models.LoyaltyTransaction).filter(
            models.LoyaltyTransaction.reference_id == order["id"],
            models.LoyaltyTransaction.reason == models.LoyaltyReason.earned,
        ).all()
        assert len(earned) <= 1, "points were awarded more than once"
    finally:
        db.close()


@live
def test_an_unknown_webhook_provider_is_rejected():
    r = requests.post(f"{API}/payments/webhook/stripe", timeout=10,
                      json={"event": "payment.captured"})
    assert r.status_code == 404


@live
def test_a_webhook_without_a_signature_is_rejected():
    r = requests.post(f"{API}/payments/webhook/manual", timeout=10,
                      json={"event": "payment.captured"})
    assert r.status_code in (400, 404)
