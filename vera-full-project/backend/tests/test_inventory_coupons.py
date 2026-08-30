"""Regression tests for the Phase 2 defects.

One test (or more) per confirmed defect, so a fix cannot silently regress.
Requires a running backend; see test_api_security.py for the invocation.
"""
import os
import uuid

import pytest
import requests

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@verahair.co", "password": "ChangeMe123!"}

pytestmark = pytest.mark.skipif(
    os.getenv("VERA_SKIP_API_TESTS") == "1", reason="API tests disabled"
)


def _alive():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="module")
def auth():
    if not _alive():
        pytest.skip(f"backend not reachable at {API}")
    tok = requests.post(f"{API}/auth/login", json=ADMIN, timeout=10).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def product(auth):
    """Published product, one variant, stock 10, priced 25000 - 20% = 20000."""
    sfx = uuid.uuid4().hex[:8]
    cat = requests.get(f"{API}/categories", timeout=10).json()[0]["id"]
    p = requests.post(f"{API}/products", headers=auth, timeout=15, json={
        "name": f"Inv Test {sfx}", "category_id": cat, "description": "d",
        "original_price": 25000, "discount_type": "percentage", "discount_value": 20,
    }).json()
    requests.post(f"{API}/products/{p['id']}/media", headers=auth, timeout=15,
                  json={"url": "https://example.com/a.jpg", "is_primary": True})
    requests.post(f"{API}/products/{p['id']}/variants", headers=auth, timeout=15,
                  json={"sku": f"INV-{sfx}", "length": '16"', "color": "Natural Black", "stock": 10})
    requests.post(f"{API}/products/{p['id']}/status", headers=auth, timeout=15,
                  json={"action": "publish"})
    full = requests.get(f"{API}/products/{p['id']}/preview", headers=auth, timeout=15).json()
    yield full
    r = requests.delete(f"{API}/products/{p['id']}", headers=auth, timeout=15)
    if r.status_code != 204:
        requests.post(f"{API}/products/{p['id']}/status",
                      json={"action": "archive"}, headers=auth, timeout=15)


def _stock(product_id):
    return requests.get(f"{API}/products/{product_id}", timeout=10).json()["variants"][0]["stock"]


# ---------- D-01 / D-02: inventory consistency ----------

def test_new_product_appears_in_inventory(auth, product):
    """D-02: a product created through the API must be visible in Inventory."""
    rows = requests.get(f"{API}/inventory", headers=auth, timeout=15).json()
    assert any(r["variant_id"] == product["variants"][0]["id"] for r in rows)


def test_inventory_matches_variant_stock_after_order(auth, product):
    """D-01: the inventory view must never drift from sellable stock."""
    variant = product["variants"][0]
    requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Inv", "customer_email": f"inv{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 3}],
    })
    rows = requests.get(f"{API}/inventory", headers=auth, timeout=15).json()
    row = next(r for r in rows if r["variant_id"] == variant["id"])
    assert row["stock"] == 7
    assert row["stock"] == _stock(product["id"])


def test_order_writes_an_inventory_movement(auth, product):
    variant = product["variants"][0]
    requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Mv", "customer_email": f"mv{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 2}],
    })
    moves = requests.get(f"{API}/inventory/movements/{variant['id']}", headers=auth, timeout=15).json()
    reasons = [m["reason"] for m in moves]
    assert "order" in reasons
    sale = next(m for m in moves if m["reason"] == "order")
    assert sale["delta"] == -2
    assert sale["stock_after"] == 8
    assert sale["reference_type"] == "order"


def test_movement_log_reconciles_with_running_total(auth, product):
    """The sum of all movements must equal current stock."""
    variant = product["variants"][0]
    requests.post(f"{API}/inventory/adjust", headers=auth, timeout=15, json={
        "variant_id": variant["id"], "delta": 4, "reason": "restock", "note": "test",
    })
    moves = requests.get(f"{API}/inventory/movements/{variant['id']}", headers=auth, timeout=15).json()
    assert sum(m["delta"] for m in moves) == _stock(product["id"])


def test_cancelling_an_order_restocks(auth, product):
    variant = product["variants"][0]
    order = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Cx", "customer_email": f"cx{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 2}],
    }).json()
    assert _stock(product["id"]) == 8
    requests.put(f"{API}/orders/{order['id']}/status", headers=auth, timeout=15,
                 json={"status": "Cancelled"})
    assert _stock(product["id"]) == 10
    moves = requests.get(f"{API}/inventory/movements/{variant['id']}", headers=auth, timeout=15).json()
    assert any(m["reason"] == "cancellation" and m["delta"] == 2 for m in moves)


def test_manual_adjustment_is_audited(auth, product):
    variant = product["variants"][0]
    r = requests.post(f"{API}/inventory/adjust", headers=auth, timeout=15, json={
        "variant_id": variant["id"], "delta": -3, "reason": "damaged", "note": "water damage",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["row"]["stock"] == 7
    assert body["movement"]["reason"] == "damaged"
    assert body["movement"]["actor"] == ADMIN["email"]


def test_adjustment_cannot_drive_stock_negative(auth, product):
    variant = product["variants"][0]
    r = requests.post(f"{API}/inventory/adjust", headers=auth, timeout=15, json={
        "variant_id": variant["id"], "delta": -999, "reason": "correction",
    })
    assert r.status_code == 400
    assert _stock(product["id"]) == 10


def test_order_reason_cannot_be_forged_manually(auth, product):
    r = requests.post(f"{API}/inventory/adjust", headers=auth, timeout=15, json={
        "variant_id": product["variants"][0]["id"], "delta": -1, "reason": "order",
    })
    assert r.status_code == 400


def test_inventory_requires_admin():
    assert requests.get(f"{API}/inventory", timeout=10).status_code == 401
    assert requests.post(f"{API}/inventory/adjust", json={}, timeout=10).status_code == 401


# ---------- D-06: publish validation ----------

def test_force_cannot_publish_without_a_variant(auth):
    cat = requests.get(f"{API}/categories", timeout=10).json()[0]["id"]
    p = requests.post(f"{API}/products", headers=auth, timeout=15, json={
        "name": f"NoVar {uuid.uuid4().hex[:6]}", "category_id": cat,
        "description": "d", "original_price": 1000,
    }).json()
    requests.post(f"{API}/products/{p['id']}/media", headers=auth, timeout=15,
                  json={"url": "https://example.com/a.jpg", "is_primary": True})
    r = requests.post(f"{API}/products/{p['id']}/status", headers=auth, timeout=15,
                      json={"action": "publish", "force": True})
    assert r.status_code == 400
    assert "variant" in r.json()["detail"].lower()
    requests.delete(f"{API}/products/{p['id']}", headers=auth, timeout=15)


# ---------- D-03 / D-04: coupon correctness ----------

def test_zero_value_coupon_is_rejected():
    r = requests.post(f"{API}/coupons/preview", timeout=10,
                      json={"code": "FITKIT", "subtotal": 20000})
    body = r.json()
    assert body["valid"] is False
    assert "not configured" in body["message"]


def test_free_shipping_rejected_when_nothing_to_waive():
    """Above the free-delivery threshold there is no shipping cost to remove."""
    body = requests.post(f"{API}/coupons/preview", timeout=10,
                         json={"code": "FREESHIP", "subtotal": 20000}).json()
    assert body["valid"] is False
    assert "already ships free" in body["message"]


def test_free_shipping_applies_when_shipping_is_charged():
    body = requests.post(f"{API}/coupons/preview", timeout=10,
                         json={"code": "FREESHIP", "subtotal": 3000}).json()
    assert body["valid"] is True
    assert float(body["shipping_discount"]) > 0
    assert float(body["shipping_fee"]) == 0


def test_quote_publishes_the_free_shipping_threshold():
    """The basket's progress meter reads this figure instead of hardcoding it.

    If the threshold and the point at which shipping is actually waived ever
    disagree, the storefront promises free delivery at the wrong basket size.
    """
    below = requests.get(f"{API}/coupons/quote",
                         params={"subtotal": 3000}, timeout=10).json()
    above = requests.get(f"{API}/coupons/quote",
                         params={"subtotal": 20000}, timeout=10).json()

    threshold = float(below["free_shipping_threshold"])
    assert threshold > 0
    assert float(above["free_shipping_threshold"]) == threshold

    assert 3000 < threshold and float(below["shipping_fee"]) > 0
    assert 20000 >= threshold and float(above["shipping_fee"]) == 0


def test_percentage_coupon_reduces_the_total():
    body = requests.post(f"{API}/coupons/preview", timeout=10,
                         json={"code": "WELCOME10", "subtotal": 20000}).json()
    assert body["valid"] is True
    assert float(body["discount_amount"]) == 2000.0
    assert float(body["new_total"]) == 18000.0


def test_unknown_coupon_is_rejected():
    body = requests.post(f"{API}/coupons/preview", timeout=10,
                         json={"code": "NOSUCHCODE", "subtotal": 20000}).json()
    assert body["valid"] is False


def test_coupon_applied_at_checkout_changes_the_charged_total(auth, product):
    variant = product["variants"][0]
    order = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Cp", "customer_email": f"cp{uuid.uuid4().hex[:6]}@example.com",
        "coupon_code": "WELCOME10",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    }).json()
    assert float(order["subtotal"]) == 20000.0
    assert float(order["discount_total"]) == 2000.0
    assert float(order["total"]) == 18000.0
    assert order["coupon_code"] == "WELCOME10"


def test_ineffective_coupon_is_rejected_at_checkout(product):
    variant = product["variants"][0]
    r = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Cp", "customer_email": f"cq{uuid.uuid4().hex[:6]}@example.com",
        "coupon_code": "FITKIT",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    })
    assert r.status_code == 400


# ---------- D-05: quantity handling ----------

def test_quantity_is_respected_and_priced_correctly(product):
    variant = product["variants"][0]
    order = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Q", "customer_email": f"q{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 3}],
    }).json()
    assert order["items"][0]["quantity"] == 3
    assert float(order["total"]) == 60000.0
    assert _stock(product["id"]) == 7


def test_absurd_quantity_is_rejected(product):
    variant = product["variants"][0]
    r = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Q", "customer_email": f"q{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 100000}],
    })
    assert r.status_code == 400


# ---------- order lifecycle ----------

def test_invalid_status_transition_is_blocked(auth, product):
    variant = product["variants"][0]
    order = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "L", "customer_email": f"l{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    }).json()
    r = requests.put(f"{API}/orders/{order['id']}/status", headers=auth, timeout=15,
                     json={"status": "Delivered"})
    assert r.status_code == 400
    assert "Cannot move an order" in r.json()["detail"]


def test_valid_status_transition_is_allowed(auth, product):
    """A legal next step must succeed, whichever state the order starts in.

    With payments disabled an order starts at Processing; with a gateway
    configured it starts at Pending Payment. Pick a valid target for each.
    """
    variant = product["variants"][0]
    order = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "L", "customer_email": f"l{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    }).json()
    next_status = "Cancelled" if order["status"] == "Pending Payment" else "Shipped"
    r = requests.put(f"{API}/orders/{order['id']}/status", headers=auth, timeout=15,
                     json={"status": next_status})
    assert r.status_code == 200
    assert r.json()["status"] == next_status
