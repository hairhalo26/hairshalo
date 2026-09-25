"""The production-readiness pass (2026-09-18): what the audit found broken.

Each test names the rule it protects. The failures they guard against were all
observed against the live shop, not imagined:

* Drafts were readable by anyone — `?status=all` and `GET /products/{id}`.
* A manual-payment order could not be moved past Pending Payment from the
  Admin Panel, and had no payment row at all if the browser never asked.
* Customers were told to pay "the account on your invoice", which did not exist.
* A double-clicked Place Order could create two orders.
* Collection images were saved but never returned by `/api/categories`.

API tests: they need a backend on port 8010 and DATABASE_URL pointing at the
same database (see the other modules in this directory).
"""
import os
import uuid
from datetime import datetime, timedelta

import pytest
import requests

from app import models
from shipping import SHIPPING

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@hairshalo.com", "password": "ChangeMe123!"}
MARKER = "pytest-prod"
PASSWORD = "correct-horse-battery-91"

# The smallest byte sequence the upload validator accepts as a JPEG. The
# storage layer checks magic bytes, not that the picture decodes.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


def _api_up():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _api_up(), reason="backend not running on port 8010")


def _provider():
    try:
        return requests.get(f"{API}/payments/config", timeout=5).json()["provider"]
    except Exception:
        return None


manual_only = pytest.mark.skipif(_provider() != "manual",
                                 reason="needs PAYMENT_PROVIDER=manual")


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def auth():
    r = requests.post(f"{API}/auth/login", timeout=10, json=ADMIN)
    if r.status_code != 200:
        pytest.skip("admin credentials rejected")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _uid():
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def category(auth):
    r = requests.post(f"{API}/categories", headers=auth, timeout=10, json={
        "name": f"Crochet {_uid()}", "description": "Crochet hair",
        "tagline": "Protective styles", "image_url": "/media/example-collection.jpg"})
    assert r.status_code == 201, r.text
    yield r.json()
    # Products archived by the tests still reference it, so it cannot be
    # deleted; hiding it keeps repeated runs from cluttering the storefront.
    requests.put(f"{API}/categories/{r.json()['id']}", headers=auth, timeout=10,
                 json={"is_active": False})


def _make_product(auth, category, *, stock=20, publish=True, price="4999"):
    """The flow an admin goes through, end to end, through the API alone."""
    tag = _uid()
    r = requests.post(f"{API}/products", headers=auth, timeout=15, json={
        "name": f"Synthetic Crochet Test Product {tag}",
        "description": "A crochet unit created by the production-readiness suite.",
        "short_description": "Suite fixture",
        "category_id": category["id"],
        "original_price": price,
        "new_arrival": True,
        "variants": [
            {"sku": f"TST-{tag}-BLK-18", "color": "Natural Black", "length": '18"', "stock": stock},
            {"sku": f"TST-{tag}-BRN-18", "color": "Brown", "length": '18"', "stock": stock},
        ],
    })
    assert r.status_code == 201, r.text
    product = r.json()
    up = requests.post(f"{API}/products/{product['id']}/media/upload", headers=auth,
                       timeout=15, files={"file": ("photo.jpg", JPEG, "image/jpeg")})
    assert up.status_code == 201, up.text
    if publish:
        pub = requests.post(f"{API}/products/{product['id']}/status", headers=auth,
                            timeout=10, json={"action": "publish"})
        assert pub.status_code == 200, pub.text
    return requests.get(f"{API}/products/{product['id']}/preview", headers=auth,
                        timeout=10).json()


@pytest.fixture
def product(auth, category):
    p = _make_product(auth, category)
    yield p
    requests.post(f"{API}/products/{p['id']}/status", headers=auth, timeout=10,
                  json={"action": "archive"})
    requests.delete(f"{API}/products/{p['id']}", headers=auth, timeout=10)


def _order(product, *, email=None, token=None, qty=1, key=None, coupon=None, variant=0):
    body = {
        "customer_name": "Readiness Shopper",
        "customer_email": email or f"{MARKER}-{_uid()}@example.com",
        "shipping": SHIPPING,
        "items": [{"product_id": product["id"],
                   "variant_id": product["variants"][variant]["id"], "quantity": qty}],
    }
    if key:
        body["idempotency_key"] = key
    if coupon:
        body["coupon_code"] = coupon
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.post(f"{API}/orders", json=body, headers=headers, timeout=15)


def _customer(verify=False):
    email = f"{MARKER}-{_uid()}@example.com"
    r = requests.post(f"{API}/account/register", timeout=15, json={
        "email": email, "password": PASSWORD, "name": "Readiness Customer"})
    assert r.status_code == 202, r.text
    login = requests.post(f"{API}/account/login", timeout=15,
                          json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return email, login.json()["access_token"]


# ---------------------------------------------------- the admin → shop flow

def test_a_published_product_reaches_the_shop_with_no_code_change(auth, category):
    """Admin creates, uploads, publishes — and the storefront API has it."""
    p = _make_product(auth, category)
    try:
        public = requests.get(f"{API}/products", timeout=10).json()
        mine = next((x for x in public if x["id"] == p["id"]), None)
        assert mine is not None, "published product missing from the public catalog"
        assert mine["category"] == category["name"]
        assert mine["image_url"] and mine["image_url"].startswith("/media/")
        assert {v["color"] for v in mine["variants"]} == {"Natural Black", "Brown"}
        assert float(mine["price"]) == 4999.0
        # The image it points at is really served.
        base = API.rsplit("/api", 1)[0]
        img = requests.get(base + mine["image_url"], timeout=10)
        assert img.status_code == 200 and img.content[:3] == b"\xff\xd8\xff"
        # Filtering by category is how the shop page lists a collection.
        by_cat = requests.get(f"{API}/products", params={"category_id": category["id"]},
                              timeout=10).json()
        assert [x["id"] for x in by_cat] == [p["id"]]
        # New-arrival flag drives its storefront rail.
        assert p["id"] in [x["id"] for x in requests.get(
            f"{API}/products", params={"new_arrival": "true"}, timeout=10).json()]
    finally:
        requests.post(f"{API}/products/{p['id']}/status", headers=auth, timeout=10,
                      json={"action": "archive"})


def test_a_draft_is_invisible_to_the_public_by_every_route(auth, category):
    p = _make_product(auth, category, publish=False)
    try:
        assert p["id"] not in [x["id"] for x in requests.get(
            f"{API}/products", params={"status": "all", "limit": 200}, timeout=10).json()]
        assert p["id"] not in [x["id"] for x in requests.get(
            f"{API}/products", params={"status": "Draft", "limit": 200}, timeout=10).json()]
        paged = requests.get(f"{API}/products/paged", params={"status": "Draft"}, timeout=10).json()
        assert p["id"] not in [x["id"] for x in paged["items"]]
        assert requests.get(f"{API}/products/{p['id']}", timeout=10).status_code == 404
        assert requests.get(f"{API}/products/{p['id']}/variants", timeout=10).status_code == 404
        # ...while staff still see it everywhere.
        assert p["id"] in [x["id"] for x in requests.get(
            f"{API}/products", params={"status": "all", "limit": 200},
            headers=auth, timeout=10).json()]
        assert requests.get(f"{API}/products/{p['id']}", headers=auth, timeout=10).status_code == 200
    finally:
        requests.delete(f"{API}/products/{p['id']}", headers=auth, timeout=10)


def test_a_customer_token_does_not_unlock_drafts(auth, category):
    p = _make_product(auth, category, publish=False)
    _email, token = _customer()
    try:
        r = requests.get(f"{API}/products/{p['id']}", timeout=10,
                         headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404
    finally:
        requests.delete(f"{API}/products/{p['id']}", headers=auth, timeout=10)


def test_unpublished_and_inactive_variants_cannot_be_bought(auth, product):
    requests.post(f"{API}/products/{product['id']}/status", headers=auth, timeout=10,
                  json={"action": "unpublish"})
    r = _order(product)
    assert r.status_code == 400 and "not available" in r.json()["detail"]
    requests.post(f"{API}/products/{product['id']}/status", headers=auth, timeout=10,
                  json={"action": "publish"})
    v = product["variants"][0]
    requests.put(f"{API}/products/variants/{v['id']}", headers=auth, timeout=10,
                 json={"is_available": False})
    r = _order(product)
    assert r.status_code == 400 and "unavailable" in r.json()["detail"]


def test_more_than_the_stock_cannot_be_bought(auth, category):
    p = _make_product(auth, category, stock=2)
    try:
        r = _order(p, qty=3)
        assert r.status_code == 400 and "Insufficient stock" in r.json()["detail"]
    finally:
        requests.post(f"{API}/products/{p['id']}/status", headers=auth, timeout=10,
                      json={"action": "archive"})


# ------------------------------------------------------------- categories

def test_collection_images_reach_the_storefront(category):
    """`tagline` and `image_url` were saved but dropped from the response."""
    cats = requests.get(f"{API}/categories", timeout=10).json()
    mine = next(c for c in cats if c["id"] == category["id"])
    assert mine["image_url"] == "/media/example-collection.jpg"
    assert mine["tagline"] == "Protective styles"


def test_subcategories_nest_one_level_only(auth, category):
    child = requests.post(f"{API}/categories", headers=auth, timeout=10, json={
        "name": f"Curly {_uid()}", "parent_id": category["id"]})
    assert child.status_code == 201, child.text
    assert child.json()["parent_id"] == category["id"]
    grandchild = requests.post(f"{API}/categories", headers=auth, timeout=10, json={
        "name": f"Tight curl {_uid()}", "parent_id": child.json()["id"]})
    assert grandchild.status_code == 400
    own = requests.put(f"{API}/categories/{category['id']}", headers=auth, timeout=10,
                       json={"parent_id": category["id"]})
    assert own.status_code == 400
    requests.delete(f"{API}/categories/{child.json()['id']}", headers=auth, timeout=10)


def test_hidden_categories_are_a_staff_view(auth):
    hidden = requests.post(f"{API}/categories", headers=auth, timeout=10, json={
        "name": f"Hidden {_uid()}", "is_active": False}).json()
    try:
        public = requests.get(f"{API}/categories", params={"include_inactive": "true"},
                              timeout=10).json()
        assert hidden["id"] not in [c["id"] for c in public]
        staff = requests.get(f"{API}/categories", params={"include_inactive": "true"},
                             headers=auth, timeout=10).json()
        assert hidden["id"] in [c["id"] for c in staff]
    finally:
        requests.delete(f"{API}/categories/{hidden['id']}", headers=auth, timeout=10)


# ---------------------------------------------------------------- payments

def test_payment_details_are_not_in_the_public_bundle():
    assert "payment" not in requests.get(f"{API}/site-content", timeout=10).json()


@manual_only
def test_a_manual_order_carries_a_payment_staff_can_confirm(auth, product):
    order = _order(product).json()
    admin_view = requests.get(f"{API}/orders/{order['id']}", headers=auth, timeout=10).json()
    assert admin_view["status"] == "Pending Payment"
    assert admin_view["payment_id"], "no payment row — staff would have nothing to confirm"
    assert admin_view["payment_provider"] == "manual"

    # Asking for the payment step again hands back the SAME payment.
    a = requests.post(f"{API}/payments/intent", json={"order_id": order["id"]}, timeout=10).json()
    b = requests.post(f"{API}/payments/intent", json={"order_id": order["id"]}, timeout=10).json()
    assert a["payment_id"] == b["payment_id"] == admin_view["payment_id"]

    paid = requests.post(f"{API}/payments/{admin_view['payment_id']}/mark-paid", headers=auth,
                         timeout=10, json={"reference": "UTR123456", "note": "UPI"})
    assert paid.status_code == 200, paid.text
    after = requests.get(f"{API}/orders/{order['id']}", headers=auth, timeout=10).json()
    assert after["status"] == "Paid" and after["is_paid"]
    assert [s["status"] for s in after["timeline"]][-1] == "Paid"


@manual_only
def test_a_cancelled_order_cannot_then_be_marked_paid(auth, product):
    order = _order(product).json()
    r = requests.put(f"{API}/orders/{order['id']}/status", headers=auth, timeout=10,
                     json={"status": "Cancelled"})
    assert r.status_code == 200, r.text
    pid = r.json()["payment_id"]
    r = requests.post(f"{API}/payments/{pid}/mark-paid", headers=auth, timeout=10, json={})
    assert r.status_code == 400


@manual_only
def test_the_payment_step_shows_the_shops_own_details(auth, product):
    requests.put(f"{API}/site-content/blocks/payment", headers=auth, timeout=10, json={
        "payload": {"upi_id": "hairshalo@okaxis", "upi_name": "Hairshalo"}})
    try:
        order = _order(product).json()
        intent = requests.post(f"{API}/payments/intent", json={"order_id": order["id"]},
                               timeout=10).json()
        assert intent["extra"]["configured"] is True
        assert intent["extra"]["details"]["upi_id"] == "hairshalo@okaxis"
        assert "hairshalo@okaxis" in intent["instructions"]
        assert "invoice" not in intent["instructions"].lower()
    finally:
        requests.post(f"{API}/site-content/blocks/payment/reset", headers=auth, timeout=10)


@manual_only
def test_with_no_details_entered_the_customer_is_not_sent_to_an_invoice(product):
    order = _order(product).json()
    intent = requests.post(f"{API}/payments/intent", json={"order_id": order["id"]},
                           timeout=10).json()
    assert intent["extra"]["configured"] is False
    assert "invoice" not in intent["instructions"].lower()


# ------------------------------------------------------------------ orders

def test_the_same_checkout_submitted_twice_is_one_order(product):
    key = str(uuid.uuid4())
    email = f"{MARKER}-{_uid()}@example.com"
    first = _order(product, email=email, key=key, qty=2)
    second = _order(product, email=email, key=key, qty=2)
    assert first.status_code == 201 and second.status_code in (200, 201)
    assert first.json()["id"] == second.json()["id"]
    stock = requests.get(f"{API}/products/{product['id']}", timeout=10).json()["variants"][0]["stock"]
    assert stock == product["variants"][0]["stock"] - 2, "stock was taken twice"
    # The key is bound to that checkout's email.
    assert _order(product, key=key).status_code == 409


def test_a_signed_in_order_belongs_to_the_account_not_the_typed_email(product):
    email, token = _customer()
    order = _order(product, email=f"typo-{_uid()}@example.com", token=token).json()
    mine = requests.get(f"{API}/account/orders", timeout=10,
                        headers={"Authorization": f"Bearer {token}"}).json()
    assert order["id"] in [o["id"] for o in mine]


def test_customers_see_tracking_and_timeline_but_never_internal_notes(auth, product):
    email, token = _customer()
    order = _order(product, email=email, token=token).json()
    oid = order["id"]
    if order["status"] == "Pending Payment":
        pid = requests.get(f"{API}/orders/{oid}", headers=auth, timeout=10).json()["payment_id"]
        requests.post(f"{API}/payments/{pid}/mark-paid", headers=auth, timeout=10, json={})
        requests.put(f"{API}/orders/{oid}/status", headers=auth, timeout=10,
                     json={"status": "Processing"})
    requests.patch(f"{API}/orders/{oid}/fulfilment", headers=auth, timeout=10,
                   json={"internal_notes": "Customer asked for discreet packaging"})
    r = requests.put(f"{API}/orders/{oid}/status", headers=auth, timeout=10, json={
        "status": "Shipped", "carrier": "Delhivery", "tracking_number": "DLV998877",
        "tracking_url": "https://www.delhivery.com/track/package/DLV998877"})
    assert r.status_code == 200, r.text

    seen = requests.get(f"{API}/account/orders/{oid}", timeout=10,
                        headers={"Authorization": f"Bearer {token}"}).json()
    assert seen["status"] == "Shipped"
    assert seen["tracking_number"] == "DLV998877" and seen["carrier"] == "Delhivery"
    steps = [s["status"] for s in seen["timeline"]]
    assert steps[0] in ("Pending Payment", "Processing") and steps[-1] == "Shipped"
    assert "internal_notes" not in seen
    assert seen["items"][0]["image_url"], "order history should show what was bought"

    staff = requests.get(f"{API}/orders/{oid}", headers=auth, timeout=10).json()
    assert staff["internal_notes"] == "Customer asked for discreet packaging"


def test_a_tracking_link_must_be_a_web_address(auth, product):
    order = _order(product).json()
    r = requests.patch(f"{API}/orders/{order['id']}/fulfilment", headers=auth, timeout=10,
                       json={"tracking_url": "javascript:alert(1)"})
    assert r.status_code == 400


def test_staff_can_search_orders(auth, product):
    email = f"{MARKER}-findme-{_uid()}@example.com"
    order = _order(product, email=email).json()
    hits = requests.get(f"{API}/orders", params={"q": email}, headers=auth, timeout=10).json()
    assert [o["id"] for o in hits] == [order["id"]]
    hits = requests.get(f"{API}/orders", params={"q": order["order_number"]},
                        headers=auth, timeout=10).json()
    assert order["id"] in [o["id"] for o in hits]


def test_order_management_is_staff_only(product):
    order = _order(product).json()
    _email, token = _customer()
    customer = {"Authorization": f"Bearer {token}"}
    for method, path, body in [
        ("get", "/orders", None),
        ("get", f"/orders/{order['id']}", None),
        ("put", f"/orders/{order['id']}/status", {"status": "Cancelled"}),
        ("patch", f"/orders/{order['id']}/fulfilment", {"internal_notes": "x"}),
        ("get", "/coupons", None),
        ("post", "/coupons", {"code": "HACK", "discount_value": 50}),
        ("put", "/site-content/blocks/payment", {"payload": {"upi_id": "evil@upi"}}),
    ]:
        for headers in ({}, customer):
            r = getattr(requests, method)(f"{API}{path}", json=body, headers=headers, timeout=10)
            assert r.status_code in (401, 403), f"{method.upper()} {path} -> {r.status_code}"


# ----------------------------------------------------------------- coupons

def _coupon(auth, **fields):
    body = {"code": f"T{_uid()}".upper(), "discount_type": "percent", "discount_value": 10}
    body.update(fields)
    return requests.post(f"{API}/coupons", headers=auth, timeout=10, json=body)


def test_a_coupon_that_could_never_work_is_refused(auth):
    assert _coupon(auth, discount_value=150).status_code == 400
    assert _coupon(auth, discount_type="bogus").status_code == 400
    now = datetime.utcnow()
    assert _coupon(auth, starts_at=(now + timedelta(days=5)).isoformat(),
                   expires_at=(now + timedelta(days=1)).isoformat()).status_code == 400


def test_coupon_rules_are_enforced_at_checkout(auth, product):
    subtotal = float(product["price"])
    # Not started yet.
    later = _coupon(auth, starts_at=(datetime.utcnow() + timedelta(days=3)).isoformat()).json()
    r = requests.post(f"{API}/coupons/preview", timeout=10,
                      json={"code": later["code"], "subtotal": subtotal})
    assert r.json()["valid"] is False and "not valid until" in r.json()["message"]
    # Percentage capped.
    capped = _coupon(auth, discount_value=50, max_discount_amount=100).json()
    r = requests.post(f"{API}/coupons/preview", timeout=10,
                      json={"code": capped["code"], "subtotal": subtotal}).json()
    assert r["valid"] and float(r["discount_amount"]) == 100.0
    # Editing a coupon changes what checkout applies.
    upd = requests.put(f"{API}/coupons/{capped['id']}", headers=auth, timeout=10,
                       json={"max_discount_amount": 200})
    assert upd.status_code == 200 and float(upd.json()["max_discount_amount"]) == 200.0
    order = _order(product, coupon=capped["code"]).json()
    assert float(order["discount_total"]) == 200.0


def test_per_customer_limit_and_cancellation_gives_the_use_back(auth, product):
    once = _coupon(auth, per_customer_limit=1).json()
    email, token = _customer()
    first = _order(product, email=email, token=token, coupon=once["code"])
    assert first.status_code == 201, first.text
    second = _order(product, email=email, token=token, coupon=once["code"])
    assert second.status_code == 400 and "per customer" in second.json()["detail"]
    # Someone else may still use it.
    assert _order(product, coupon=once["code"]).status_code == 201

    requests.put(f"{API}/orders/{first.json()['id']}/status", headers=auth, timeout=10,
                 json={"status": "Cancelled"})
    coupons = requests.get(f"{API}/coupons", headers=auth, timeout=10).json()
    assert next(c for c in coupons if c["id"] == once["id"])["usage_count"] == 1
    assert _order(product, email=email, token=token, coupon=once["code"]).status_code == 201


# --------------------------------------------------------------- inventory

def test_low_stock_threshold_is_set_per_variant(auth, product):
    v = product["variants"][0]
    r = requests.put(f"{API}/products/variants/{v['id']}", headers=auth, timeout=10,
                     json={"low_stock_threshold": 25})
    assert r.status_code == 200 and r.json()["low_stock_threshold"] == 25
    rows = requests.get(f"{API}/inventory", params={"q": v["sku"]}, headers=auth, timeout=10).json()
    row = next(x for x in rows if x["variant_id"] == v["id"])
    assert row["low_stock_threshold"] == 25 and row["stock_level"] == "low"   # 20 <= 25


# ------------------------------------------------------------------ pricing

def test_variants_follow_the_product_price_until_given_their_own(auth, product):
    """The editor used to write each variant's inherited price back as an
    override, so a later product price change never reached customers."""
    v = product["variants"][0]
    assert v["has_price_override"] is False
    requests.put(f"{API}/products/{product['id']}", headers=auth, timeout=10,
                 json={"original_price": "5999"})
    after = requests.get(f"{API}/products/{product['id']}", timeout=10).json()
    assert {float(x["price"]) for x in after["variants"]} == {5999.0}

    r = requests.put(f"{API}/products/variants/{v['id']}", headers=auth, timeout=10,
                     json={"original_price": "7500"})
    assert r.json()["has_price_override"] is True and float(r.json()["price"]) == 7500.0
    r = requests.put(f"{API}/products/variants/{v['id']}", headers=auth, timeout=10,
                     json={"clear_price_override": True})
    assert r.json()["has_price_override"] is False and float(r.json()["price"]) == 5999.0


def test_a_draft_can_be_saved_before_it_has_a_price_or_category(auth):
    """Admins build a product up over time. Only publishing needs the category
    — a shop with no categories yet must still be able to start. A price is not
    a publish requirement: an unpriced product lists as "Price on request" and
    POST /orders refuses it, so `force` may waive it but never the category."""
    r = requests.post(f"{API}/products", headers=auth, timeout=10,
                      json={"name": f"Unfinished Wig {uuid.uuid4().hex[:6]}", "status": "Draft"})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["price"] is None and p["category_id"] is None and p["status"] == "Draft"
    # Editing other fields keeps working while it is still unpriced.
    r = requests.put(f"{API}/products/{p['id']}", headers=auth, timeout=10,
                     json={"description": "Still being written", "discount_type": "none"})
    assert r.status_code == 200, r.text
    r = requests.post(f"{API}/products/{p['id']}/status", headers=auth, timeout=10,
                      json={"action": "publish", "force": True})
    assert r.status_code == 400
    assert "Choose a category" in r.json()["detail"]
    assert "Set a price" not in r.json()["detail"]
    requests.delete(f"{API}/products/{p['id']}", headers=auth, timeout=10)


def test_delete_removes_unordered_products_and_refuses_ordered_ones(auth, category):
    """The Admin Panel's delete button: gone for good if nobody bought it;
    refused (so it can be archived) once an order refers to it."""
    spare = _make_product(auth, category, publish=False)
    r = requests.delete(f"{API}/products/{spare['id']}", headers=auth, timeout=10)
    assert r.status_code == 204
    assert requests.get(f"{API}/products/{spare['id']}", headers=auth, timeout=10).status_code == 404

    sold = _make_product(auth, category)
    assert _order(sold).status_code == 201
    r = requests.delete(f"{API}/products/{sold['id']}", headers=auth, timeout=10)
    assert r.status_code == 400 and "archive" in r.json()["detail"].lower()
    assert requests.get(f"{API}/products/{sold['id']}", timeout=10).status_code == 200

    # Customers and anonymous callers can never delete.
    _email, token = _customer()
    for headers in ({}, {"Authorization": f"Bearer {token}"}):
        assert requests.delete(f"{API}/products/{sold['id']}", headers=headers,
                               timeout=10).status_code in (401, 403)
    requests.post(f"{API}/products/{sold['id']}/status", headers=auth, timeout=10,
                  json={"action": "archive"})


# ----------------------------------------------------------- staff sign-out

def _staff_account():
    """A throwaway staff user, so signing out never touches the shared admin
    whose token the rest of this suite is using."""
    from app.database import SessionLocal
    from app.security import hash_password
    from app import models as m
    email, pw = f"staff-{uuid.uuid4().hex[:8]}@example.com", "Staff-password-2026!"
    db = SessionLocal()
    try:
        user = m.User(email=email, hashed_password=hash_password(pw), full_name="Test Staff", role="staff")
        db.add(user)
        db.commit()
        return user.id, email, pw
    finally:
        db.close()


def _staff_login(email, pw):
    r = requests.post(f"{API}/auth/login", timeout=10, json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_signing_out_revokes_staff_tokens_on_the_server():
    """Sign out used to only forget the token in the page; a copied token kept
    working for 24 hours. Now every token issued before the sign-out is refused."""
    _uid_, email, pw = _staff_account()
    first = _staff_login(email, pw)
    second = _staff_login(email, pw)                  # e.g. another device
    assert requests.get(f"{API}/orders?limit=1", headers=first, timeout=10).status_code == 200

    r = requests.post(f"{API}/auth/logout", headers=first, timeout=10)
    assert r.status_code == 200
    for old in (first, second):
        assert requests.get(f"{API}/orders?limit=1", headers=old, timeout=10).status_code == 401
        assert requests.get(f"{API}/auth/me", headers=old, timeout=10).status_code == 401
        assert requests.post(f"{API}/auth/logout", headers=old, timeout=10).status_code == 401
    # A revoked token on a public endpoint just gets the public view: no drafts.
    public = requests.get(f"{API}/products", params={"status": "all", "limit": 200},
                          headers=first, timeout=10).json()
    assert all(p["status"] == "Published" for p in public)
    # Signing in again works.
    fresh = _staff_login(email, pw)
    assert requests.get(f"{API}/orders?limit=1", headers=fresh, timeout=10).status_code == 200


def test_a_token_from_before_versioning_still_works_until_the_first_sign_out():
    """Tokens issued before this change carry no version; they count as 0, so
    the upgrade signs nobody out — but a sign-out still revokes them."""
    from app.security import create_access_token
    user_id, email, pw = _staff_account()
    legacy = {"Authorization": "Bearer " + create_access_token({"sub": user_id, "role": "staff"})}
    assert requests.get(f"{API}/orders?limit=1", headers=legacy, timeout=10).status_code == 200
    requests.post(f"{API}/auth/logout", headers=_staff_login(email, pw), timeout=10)
    assert requests.get(f"{API}/orders?limit=1", headers=legacy, timeout=10).status_code == 401


def test_staff_sign_out_is_staff_only_and_leaves_customers_alone():
    email, token = _customer()
    customer = {"Authorization": f"Bearer {token}"}
    for headers in ({}, customer):
        assert requests.post(f"{API}/auth/logout", headers=headers, timeout=10).status_code == 401
    _uid_, s_email, s_pw = _staff_account()
    requests.post(f"{API}/auth/logout", headers=_staff_login(s_email, s_pw), timeout=10)
    assert requests.get(f"{API}/account/me", headers=customer, timeout=10).status_code == 200
