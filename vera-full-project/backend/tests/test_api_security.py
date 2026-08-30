"""End-to-end API tests against a RUNNING backend.

These cover the security-critical paths: authorization, price manipulation,
placeholder isolation, publishing visibility and stock validation.

    # backend must be running, e.g.
    uvicorn app.main:app --port 8010
    VERA_API=http://127.0.0.1:8010/api python -m pytest tests/test_api_security.py -q

Every product this module creates is deleted again in the fixture teardown.
"""
import os
import uuid

import pytest
import requests

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@hairshalo.com", "password": "ChangeMe123!"}

pytestmark = pytest.mark.skipif(
    os.getenv("VERA_SKIP_API_TESTS") == "1", reason="API tests disabled"
)


def _alive():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="module")
def token():
    if not _alive():
        pytest.skip(f"backend not reachable at {API}")
    r = requests.post(f"{API}/auth/login", json=ADMIN, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def category_id(auth):
    return requests.get(f"{API}/categories", timeout=10).json()[0]["id"]


@pytest.fixture
def product(auth, category_id):
    """A published product with one variant (stock 5) at 25000 - 20% = 20000."""
    suffix = uuid.uuid4().hex[:8]
    body = {
        "name": f"Test Wig {suffix}",
        "category_id": category_id,
        "description": "test product",
        "original_price": 25000,
        "discount_type": "percentage",
        "discount_value": 20,
    }
    p = requests.post(f"{API}/products", json=body, headers=auth, timeout=15).json()
    requests.post(f"{API}/products/{p['id']}/media",
                  json={"url": "https://example.com/a.jpg", "is_primary": True},
                  headers=auth, timeout=15)
    requests.post(f"{API}/products/{p['id']}/variants",
                  json={"sku": f"TEST-{suffix}", "length": '16"', "color": "Natural Black",
                        "stock": 5},
                  headers=auth, timeout=15)
    requests.post(f"{API}/products/{p['id']}/status",
                  json={"action": "publish"}, headers=auth, timeout=15)
    full = requests.get(f"{API}/products/{p['id']}/preview", headers=auth, timeout=15).json()
    yield full
    # Teardown: delete outright, but a product that acquired order history
    # cannot be deleted (by design) — archive it instead so test fixtures never
    # linger on the public storefront.
    r = requests.delete(f"{API}/products/{p['id']}", headers=auth, timeout=15)
    if r.status_code != 204:
        requests.post(f"{API}/products/{p['id']}/status",
                      json={"action": "archive"}, headers=auth, timeout=15)


# ---------------- authorization ----------------

@pytest.mark.parametrize("method,path", [
    ("post", "/products"),
    ("post", "/categories"),
])
def test_write_endpoints_require_auth(method, path):
    r = getattr(requests, method)(f"{API}{path}", json={}, timeout=10)
    assert r.status_code == 401


def test_customer_cannot_create_product():
    r = requests.post(f"{API}/products", json={"name": "hacked"}, timeout=10)
    assert r.status_code == 401


def test_admin_can_create_product(product):
    assert product["status"] == "Published"
    assert product["price"] == "20000.00"
    assert product["compare_at_price"] == "25000.00"
    assert product["discount_percent"] == 20


# ---------------- pricing ----------------

@pytest.mark.parametrize("dtype,dvalue,expected_status", [
    ("percentage", 150, 400),
    ("fixed_amount", 30000, 400),
])
def test_invalid_discounts_rejected(auth, category_id, dtype, dvalue, expected_status):
    r = requests.post(f"{API}/products", headers=auth, timeout=15, json={
        "name": f"Bad {uuid.uuid4().hex[:6]}", "category_id": category_id,
        "original_price": 25000, "discount_type": dtype, "discount_value": dvalue,
    })
    assert r.status_code == expected_status


def test_negative_discount_rejected_by_schema(auth, category_id):
    r = requests.post(f"{API}/products", headers=auth, timeout=15, json={
        "name": "Bad neg", "category_id": category_id,
        "original_price": 25000, "discount_type": "percentage", "discount_value": -5,
    })
    assert r.status_code in (400, 422)


# ---------------- checkout security ----------------

def test_client_supplied_price_is_ignored(product):
    variant = product["variants"][0]
    r = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Attacker", "customer_email": f"atk{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"],
                   "quantity": 1, "price": 1, "total": 1}],
    })
    assert r.status_code == 201
    order = r.json()
    assert order["total"] == "20000.00"
    assert order["items"][0]["price"] == "20000.00"


def test_stock_is_validated_server_side(product):
    """Quantity within the per-line cap but above stock must fail on stock."""
    variant = product["variants"][0]
    r = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Greedy", "customer_email": f"greedy{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 50}],
    })
    assert r.status_code == 400
    assert "stock" in r.json()["detail"].lower()


def test_absurd_quantity_hits_the_per_line_cap(product):
    """A quantity beyond the cap is refused before any stock work happens."""
    variant = product["variants"][0]
    r = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Greedy", "customer_email": f"greedy{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 9999}],
    })
    assert r.status_code == 400
    assert "maximum" in r.json()["detail"].lower()


def test_variant_from_another_product_rejected(auth, product, category_id):
    other = requests.get(f"{API}/products", timeout=10).json()
    foreign = None
    for p in other:
        if p["id"] != product["id"] and p["variants"]:
            foreign = p["variants"][0]["id"]
            break
    if not foreign:
        pytest.skip("no other product with variants")
    r = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "X", "customer_email": f"x{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": foreign, "quantity": 1}],
    })
    assert r.status_code == 400
    assert "does not belong" in r.json()["detail"]


def test_placeholder_cannot_be_ordered():
    placeholders = requests.get(f"{API}/product-placeholders", timeout=10).json()
    if not placeholders:
        pytest.skip("no placeholders seeded")
    r = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "X", "customer_email": f"ph{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": placeholders[0]["id"], "quantity": 1}],
    })
    assert r.status_code == 400
    assert "placeholder" in r.json()["detail"].lower()


def test_placeholder_is_not_a_product():
    placeholders = requests.get(f"{API}/product-placeholders", timeout=10).json()
    if not placeholders:
        pytest.skip("no placeholders seeded")
    assert requests.get(f"{API}/products/{placeholders[0]['id']}", timeout=10).status_code == 404


# ---------------- publishing visibility ----------------

@pytest.mark.parametrize("action,expected_visible", [
    ("unpublish", False),
    ("submit_for_review", False),
    ("publish", True),
    ("archive", False),
])
def test_publishing_controls_public_visibility(auth, product, action, expected_visible):
    requests.post(f"{API}/products/{product['id']}/status",
                  json={"action": action}, headers=auth, timeout=15)
    public = requests.get(f"{API}/products", timeout=10).json()
    assert any(p["id"] == product["id"] for p in public) is expected_visible


def test_unpublished_product_cannot_be_ordered(auth, product):
    requests.post(f"{API}/products/{product['id']}/status",
                  json={"action": "unpublish"}, headers=auth, timeout=15)
    variant = product["variants"][0]
    r = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "X", "customer_email": f"np{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    })
    assert r.status_code == 400
    assert "not available" in r.json()["detail"].lower()


# ---------------- historical immutability ----------------

def test_price_change_does_not_rewrite_history(auth, product):
    variant = product["variants"][0]
    order = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Buyer", "customer_email": f"hist{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    }).json()
    assert order["items"][0]["price"] == "20000.00"

    requests.put(f"{API}/products/{product['id']}", headers=auth, timeout=15,
                 json={"original_price": 999, "discount_type": "none", "discount_value": 0})

    orders = requests.get(f"{API}/orders", headers=auth, timeout=15).json()
    stored = next(o for o in orders if o["order_number"] == order["order_number"])
    assert stored["items"][0]["price"] == "20000.00"
    assert stored["total"] == "20000.00"


# ---------------- uploads ----------------

def test_upload_rejects_non_media(auth, product):
    files = {"file": ("evil.sh", b"#!/bin/sh\nrm -rf /\n", "application/x-sh")}
    r = requests.post(f"{API}/products/{product['id']}/media/upload",
                      files=files, headers=auth, timeout=20)
    assert r.status_code == 400


def test_upload_rejects_disguised_file(auth, product):
    files = {"file": ("fake.jpg", b"#!/bin/sh\nrm -rf /\n", "image/jpeg")}
    r = requests.post(f"{API}/products/{product['id']}/media/upload",
                      files=files, headers=auth, timeout=20)
    assert r.status_code == 400
    assert "do not match" in r.json()["detail"]
