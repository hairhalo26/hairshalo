"""Order lifecycle (2026-10-01): Packed, tracking only after shipping, map pins,
colour photos, and the customer/staff boundaries around all of it.

The rules these protect:

* An order is placed with NO tracking — no number, no link, no email about it.
* Packed is required: Processing -> Packed -> Shipped; Processing -> Shipped is refused.
* Staff may record tracking from Packed onwards; the customer sees it only
  from Shipped onwards. Before Packed, new tracking is refused outright.
* Only staff can move an order or touch tracking; a customer token cannot.
* A map pin travels with the address (checkout snapshot and address book),
  is range-checked, and is never a way to reach another customer's address.
* A photo filed on one length of a colour pictures every length of it.

API tests: they need a backend on port 8010 and DATABASE_URL pointing at the
same database (see the other modules in this directory).
"""
import os
import uuid

import pytest
import requests

from shipping import SHIPPING, valid_shipping

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@hairshalo.com", "password": "ChangeMe123!"}
MARKER = "pytest-lifecycle"
PASSWORD = "correct-horse-battery-91"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
PIN = {"latitude": 17.385044, "longitude": 78.486671,
       "place_id": "ChIJx9Lr6tqZyzsRWJbn0vZwqnk",
       "formatted_address": "12 MG Road, Hyderabad, Telangana 500001, India"}


def _api_up():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _api_up(), reason="backend not running on port 8010")


def _uid():
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def auth():
    r = requests.post(f"{API}/auth/login", timeout=10, json=ADMIN)
    if r.status_code != 200:
        pytest.skip("admin credentials rejected")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def category(auth):
    r = requests.post(f"{API}/categories", headers=auth, timeout=10, json={
        "name": f"Lifecycle {_uid()}", "description": "Lifecycle fixtures"})
    assert r.status_code == 201, r.text
    yield r.json()
    requests.put(f"{API}/categories/{r.json()['id']}", headers=auth, timeout=10,
                 json={"is_active": False})


def _make_product(auth, category, *, publish=True, stock=20):
    """Two colours x two lengths, and one photo of Black filed on Black 14in."""
    tag = _uid()
    r = requests.post(f"{API}/products", headers=auth, timeout=15, json={
        "name": f"Lifecycle Wig {tag}", "description": "Lifecycle suite fixture.",
        "category_id": category["id"], "original_price": "5999",
        "variants": [
            {"sku": f"LC-{tag}-BLK-14", "color": "Natural Black", "length": '14"', "stock": stock},
            {"sku": f"LC-{tag}-BLK-18", "color": "Natural Black", "length": '18"', "stock": stock},
            {"sku": f"LC-{tag}-613-14", "color": "613", "length": '14"', "stock": stock},
            {"sku": f"LC-{tag}-613-18", "color": "613", "length": '18"', "stock": 0},
        ],
    })
    assert r.status_code == 201, r.text
    product = r.json()
    by_sku = {v["sku"]: v for v in product["variants"]}
    # A product-level photo (the gallery fallback) and one of Black on 14in.
    for variant_id in ("", by_sku[f"LC-{tag}-BLK-14"]["id"]):
        up = requests.post(f"{API}/products/{product['id']}/media/upload", headers=auth,
                           timeout=15, data={"variant_id": variant_id},
                           files={"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert up.status_code == 201, up.text
    if publish:
        pub = requests.post(f"{API}/products/{product['id']}/status", headers=auth,
                            timeout=10, json={"action": "publish"})
        assert pub.status_code == 200, pub.text
    fresh = requests.get(f"{API}/products/{product['id']}/preview", headers=auth,
                         timeout=10).json()
    fresh["by_sku"] = {v["sku"]: v for v in fresh["variants"]}
    fresh["tag"] = tag
    return fresh


@pytest.fixture
def product(auth, category):
    p = _make_product(auth, category)
    yield p
    requests.post(f"{API}/products/{p['id']}/status", headers=auth, timeout=10,
                  json={"action": "archive"})
    requests.delete(f"{API}/products/{p['id']}", headers=auth, timeout=10)


def _variant(product, colour, length):
    return product["by_sku"][f"LC-{product['tag']}-{colour}-{length}"]


def _customer():
    email = f"{MARKER}-{_uid()}@example.com"
    r = requests.post(f"{API}/account/register", timeout=15, json={
        "email": email, "password": PASSWORD, "name": "Lifecycle Customer"})
    assert r.status_code == 202, r.text
    login = requests.post(f"{API}/account/login", timeout=15,
                          json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return email, {"Authorization": f"Bearer {login.json()['access_token']}"}


def _order(product, variant, *, headers=None, email=None, shipping=None, extra_item=None, **body):
    item = {"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}
    item.update(extra_item or {})
    payload = {"customer_name": "Lifecycle Shopper",
               "customer_email": email or f"{MARKER}-{_uid()}@example.com",
               "shipping": shipping or SHIPPING, "items": [item], **body}
    return requests.post(f"{API}/orders", json=payload, headers=headers or {}, timeout=15)


def _to_processing(auth, order):
    """Whatever the payment provider, bring a fresh order to Processing."""
    oid = order["id"]
    if order["status"] == "Pending Payment":
        staff = requests.get(f"{API}/orders/{oid}", headers=auth, timeout=10).json()
        r = requests.post(f"{API}/payments/{staff['payment_id']}/mark-paid",
                          headers=auth, timeout=10, json={})
        assert r.status_code == 200, r.text
        r = requests.put(f"{API}/orders/{oid}/status", headers=auth, timeout=10,
                         json={"status": "Processing"})
        assert r.status_code == 200, r.text
    return requests.get(f"{API}/orders/{oid}", headers=auth, timeout=10).json()


def _move(auth, oid, status, **body):
    return requests.put(f"{API}/orders/{oid}/status", headers=auth, timeout=10,
                        json={"status": status, **body})


def _mine(headers, oid):
    return requests.get(f"{API}/account/orders/{oid}", headers=headers, timeout=10)


TRACK = {"carrier": "Delhivery", "tracking_number": "DLV-LC-1234",
         "tracking_url": "https://www.delhivery.com/track/package/DLV-LC-1234"}


# ------------------------------------------------------- tracking lifecycle

def test_a_new_order_carries_no_tracking_at_all(auth, product):
    email, me = _customer()
    r = _order(product, _variant(product, "BLK", "14"), headers=me, email=email)
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["tracking_number"] is None and order["tracking_url"] is None
    assert order["carrier"] is None
    seen = _mine(me, order["id"]).json()
    assert seen["tracking_number"] is None and seen["tracking_url"] is None
    # ...and the confirmation email says nothing about tracking.
    rows = requests.get(f"{API}/notifications", headers=auth, timeout=10,
                        params={"reference_id": order["id"]}).json()
    placed = [n for n in rows if n["event_type"] == "order.placed"]
    assert placed, "the order confirmation was not queued"
    body = requests.get(f"{API}/notifications/{placed[0]['id']}", headers=auth,
                        timeout=10).json()
    assert "track" not in body["body_text"].lower()
    assert not [n for n in rows if n["event_type"] == "order.shipped"]


def test_tracking_is_refused_before_the_order_is_packed(auth, product):
    order = _to_processing(auth, _order(product, _variant(product, "BLK", "14")).json())
    oid = order["id"]
    r = requests.patch(f"{API}/orders/{oid}/fulfilment", headers=auth, timeout=10, json=TRACK)
    assert r.status_code == 400, r.text
    assert "Packed" in r.json()["detail"]
    # Staff notes are still fine at any stage.
    r = requests.patch(f"{API}/orders/{oid}/fulfilment", headers=auth, timeout=10,
                       json={"internal_notes": "Gift wrap", "tracking_number": "", "carrier": ""})
    assert r.status_code == 200, r.text
    assert r.json()["tracking_number"] is None


def test_packed_tracking_is_staff_only_until_shipped(auth, product):
    email, me = _customer()
    order = _order(product, _variant(product, "BLK", "18"), headers=me, email=email).json()
    oid = _to_processing(auth, order)["id"]

    r = _move(auth, oid, "Packed", note="Boxed with care card", **TRACK)
    assert r.status_code == 200, r.text
    staff = r.json()
    assert staff["status"] == "Packed"
    assert staff["tracking_number"] == TRACK["tracking_number"], "staff must see what they saved"

    seen = _mine(me, oid).json()
    assert seen["status"] == "Packed"
    assert seen["tracking_number"] is None and seen["tracking_url"] is None
    assert seen["carrier"] is None
    rows = requests.get(f"{API}/notifications", headers=auth, timeout=10,
                        params={"reference_id": oid}).json()
    assert not [n for n in rows if n["event_type"] == "order.shipped"]

    r = _move(auth, oid, "Shipped")
    assert r.status_code == 200, r.text
    seen = _mine(me, oid).json()
    assert seen["tracking_number"] == TRACK["tracking_number"]
    assert seen["tracking_url"] == TRACK["tracking_url"]
    assert seen["carrier"] == "Delhivery"
    steps = [s["status"] for s in seen["timeline"]]
    assert steps[-2:] == ["Packed", "Shipped"], steps

    # The shipped email is the one that carries tracking, link included.
    rows = requests.get(f"{API}/notifications", headers=auth, timeout=10,
                        params={"reference_id": oid, "event_type": "order.shipped"}).json()
    assert len(rows) == 1
    mail = requests.get(f"{API}/notifications/{rows[0]['id']}", headers=auth, timeout=10).json()
    assert TRACK["tracking_number"] in mail["body_text"]
    assert TRACK["tracking_url"] in mail["body_html"]


def test_packed_cannot_be_skipped(auth, product):
    order = _to_processing(auth, _order(product, _variant(product, "613", "14")).json())
    oid = order["id"]
    # Processing -> Shipped is refused, with or without tracking...
    for body in ({}, TRACK):
        r = _move(auth, oid, "Shipped", **body)
        assert r.status_code == 400, r.text
        assert "Cannot move an order from Processing to Shipped" in r.json()["detail"]
    # ...and nothing was recorded by the refused attempts.
    staff = requests.get(f"{API}/orders/{oid}", headers=auth, timeout=10).json()
    assert staff["status"] == "Processing" and staff["tracking_number"] is None
    assert [s["status"] for s in staff["timeline"]][-1] == "Processing"


def test_the_full_lifecycle_placed_to_delivered(auth, product):
    """Placed -> Processing -> Packed -> Shipped -> Delivered, as the customer sees it."""
    email, me = _customer()
    order = _order(product, _variant(product, "613", "14"), headers=me, email=email).json()
    oid = _to_processing(auth, order)["id"]
    seen = []
    for status, body in (("Packed", TRACK), ("Shipped", {}), ("Delivered", {})):
        r = _move(auth, oid, status, **body)
        assert r.status_code == 200, (status, r.text)
        mine = _mine(me, oid).json()
        seen.append((mine["status"], mine["tracking_number"]))
    assert seen == [("Packed", None),
                    ("Shipped", TRACK["tracking_number"]),
                    ("Delivered", TRACK["tracking_number"])]
    steps = [s["status"] for s in _mine(me, oid).json()["timeline"]]
    assert steps[-4:] == ["Processing", "Packed", "Shipped", "Delivered"], steps
    # A delivered order cannot be moved back.
    for back in ("Packed", "Shipped", "Processing"):
        assert _move(auth, oid, back).status_code == 400


def test_moving_to_processing_cannot_carry_tracking(auth, product):
    order = _order(product, _variant(product, "BLK", "14")).json()
    if order["status"] != "Pending Payment":
        pytest.skip("payments disabled; the order is already Processing")
    staff = requests.get(f"{API}/orders/{order['id']}", headers=auth, timeout=10).json()
    requests.post(f"{API}/payments/{staff['payment_id']}/mark-paid", headers=auth,
                  timeout=10, json={})
    r = _move(auth, order["id"], "Processing", **TRACK)
    assert r.status_code == 400 and "Packed" in r.json()["detail"]
    assert requests.get(f"{API}/orders/{order['id']}", headers=auth,
                        timeout=10).json()["status"] == "Paid"


def test_a_customer_cannot_move_an_order_or_add_tracking(auth, product):
    email, me = _customer()
    order = _order(product, _variant(product, "BLK", "14"), headers=me, email=email).json()
    oid = order["id"]
    for status in ("Packed", "Shipped", "Delivered"):
        r = requests.put(f"{API}/orders/{oid}/status", headers=me, timeout=10,
                         json={"status": status, **TRACK})
        assert r.status_code in (401, 403), (status, r.status_code)
    r = requests.patch(f"{API}/orders/{oid}/fulfilment", headers=me, timeout=10, json=TRACK)
    assert r.status_code in (401, 403)
    r = requests.patch(f"{API}/orders/{oid}/fulfilment", timeout=10, json=TRACK)
    assert r.status_code == 401
    # Nothing changed.
    staff = requests.get(f"{API}/orders/{oid}", headers=auth, timeout=10).json()
    assert staff["status"] == order["status"] and staff["tracking_number"] is None


# --------------------------------------------------- customers kept apart

def test_customer_a_cannot_see_or_use_customer_b_data(auth, product):
    email_a, a = _customer()
    email_b, b = _customer()
    order_b = _order(product, _variant(product, "BLK", "14"), headers=b, email=email_b).json()
    addr_b = requests.post(f"{API}/account/addresses", headers=b, timeout=10,
                           json={**SHIPPING, **PIN}).json()

    assert _mine(a, order_b["id"]).status_code == 404
    assert order_b["id"] not in [o["id"] for o in
                                 requests.get(f"{API}/account/orders", headers=a, timeout=10).json()]
    # B's saved address can be neither changed, deleted nor shipped to by A.
    r = requests.put(f"{API}/account/addresses/{addr_b['id']}", headers=a, timeout=10,
                     json={**SHIPPING, "line1": "99 Elsewhere Street"})
    assert r.status_code == 404
    assert requests.delete(f"{API}/account/addresses/{addr_b['id']}", headers=a,
                           timeout=10).status_code == 404
    r = requests.post(f"{API}/orders", headers=a, timeout=15, json={
        "customer_name": "A", "customer_email": email_a,
        "shipping_address_id": addr_b["id"],
        "items": [{"product_id": product["id"],
                   "variant_id": _variant(product, "BLK", "14")["id"], "quantity": 1}]})
    assert r.status_code == 404
    still = requests.get(f"{API}/account/addresses", headers=b, timeout=10).json()
    assert [x["line1"] for x in still] == [SHIPPING["line1"]]


# ---------------------------------------------------------- catalogue rules

def test_drafts_and_sold_out_variants_cannot_be_bought(auth, category, product):
    draft = _make_product(auth, category, publish=False)
    try:
        assert requests.get(f"{API}/products/{draft['id']}", timeout=10).status_code == 404
        assert draft["id"] not in [p["id"] for p in requests.get(f"{API}/products", timeout=10).json()]
        r = _order(draft, _variant(draft, "BLK", "14"))
        assert r.status_code == 400 and "not available" in r.json()["detail"]
    finally:
        requests.delete(f"{API}/products/{draft['id']}", headers=auth, timeout=10)
    r = _order(product, _variant(product, "613", "18"))     # stock 0
    assert r.status_code == 400 and "Insufficient stock" in r.json()["detail"]


def test_the_browser_cannot_set_the_price_or_the_total(product):
    variant = _variant(product, "BLK", "14")
    r = _order(product, variant, extra_item={"price": "1.00", "unit_price": "1.00"},
               total="1.00", subtotal="1.00", shipping_fee="0")
    assert r.status_code == 201, r.text
    order = r.json()
    assert float(order["items"][0]["price"]) == 5999.0
    assert float(order["subtotal"]) == 5999.0
    assert float(order["total"]) == 5999.0 + float(order["shipping_fee"])


def test_a_refreshed_or_double_clicked_checkout_is_one_order(auth, product):
    key, email = str(uuid.uuid4()), f"{MARKER}-{_uid()}@example.com"
    variant = _variant(product, "BLK", "14")
    first = _order(product, variant, email=email, idempotency_key=key)
    second = _order(product, variant, email=email, idempotency_key=key)
    assert first.status_code == 201 and second.status_code in (200, 201)
    assert first.json()["id"] == second.json()["id"]
    hits = requests.get(f"{API}/orders", headers=auth, timeout=10, params={"q": email}).json()
    assert len(hits) == 1


# ----------------------------------------------------------------- map pins

def test_a_map_pin_is_kept_with_the_order(auth, product):
    email, me = _customer()
    r = _order(product, _variant(product, "BLK", "14"), headers=me, email=email,
               shipping={**SHIPPING, **PIN})
    assert r.status_code == 201, r.text
    for view in (_mine(me, r.json()["id"]).json(),
                 requests.get(f"{API}/orders/{r.json()['id']}", headers=auth, timeout=10).json()):
        ship = view["shipping"]
        assert abs(ship["latitude"] - PIN["latitude"]) < 1e-6
        assert abs(ship["longitude"] - PIN["longitude"]) < 1e-6
        assert ship["place_id"] == PIN["place_id"]
        assert ship["formatted_address"] == PIN["formatted_address"]
        # The typed address is still the address.
        assert ship["line1"] == SHIPPING["line1"] and ship["postal_code"] == SHIPPING["postal_code"]


def test_an_order_without_a_pin_still_works(product):
    r = _order(product, _variant(product, "BLK", "14"))
    assert r.status_code == 201, r.text
    assert r.json()["shipping"]["latitude"] is None


@pytest.mark.parametrize("bad", [
    {"latitude": 17.3},                                   # half a pair
    {"latitude": 123.0, "longitude": 78.4},               # off the planet
    {"latitude": 17.3, "longitude": -200},
    {"latitude": 17.3, "longitude": 78.4, "place_id": "<script>alert(1)</script>"},
])
def test_a_bad_map_pin_is_refused(product, bad):
    r = _order(product, _variant(product, "BLK", "14"), shipping=valid_shipping(**bad))
    assert r.status_code == 422, r.text
    assert "location" in r.json()["detail"]["fields"]


def test_a_saved_pin_travels_from_the_address_book_to_the_order(auth, product):
    email, me = _customer()
    saved = requests.post(f"{API}/account/addresses", headers=me, timeout=10,
                          json={**SHIPPING, **PIN, "is_default": True})
    assert saved.status_code == 201, saved.text
    assert saved.json()["place_id"] == PIN["place_id"]
    r = requests.post(f"{API}/orders", headers=me, timeout=15, json={
        "customer_name": "Lifecycle Shopper", "customer_email": email,
        "shipping_address_id": saved.json()["id"],
        "items": [{"product_id": product["id"],
                   "variant_id": _variant(product, "BLK", "14")["id"], "quantity": 1}]})
    assert r.status_code == 201, r.text
    assert r.json()["shipping"]["place_id"] == PIN["place_id"]


def test_the_maps_config_never_invents_a_key():
    r = requests.get(f"{API}/orders/maps-config", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"api_key", "map_id"}
    # Unset key -> null, so the storefront hides the picker.
    assert body["api_key"] is None or isinstance(body["api_key"], str)


# ------------------------------------------------------------ colour photos

def test_a_colour_photo_pictures_every_length_of_that_colour(auth, product):
    black_photo = next(m for m in product["media"] if m["variant_id"])
    assert black_photo["variant_id"] == _variant(product, "BLK", "14")["id"]
    email, me = _customer()
    order = _order(product, _variant(product, "BLK", "18"), headers=me, email=email).json()
    line = _mine(me, order["id"]).json()["items"][0]
    assert line["image_url"] == black_photo["url"], "18in Black should show the Black photo"
    # A colour with no photo of its own falls back to the product's main image.
    other = _order(product, _variant(product, "613", "14"), headers=me, email=email).json()
    line = _mine(me, other["id"]).json()["items"][0]
    assert line["image_url"] and line["image_url"] != black_photo["url"]
