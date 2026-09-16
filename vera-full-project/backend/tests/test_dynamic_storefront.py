"""Admin-managed catalog and content, validated addresses, and order snapshots.

Four rules under test here, each of which the storefront now depends on:

* **A product reaches customers only through the admin workflow.** Created as a
  Draft, invisible publicly until it is published, and refused at checkout in
  every other state.
* **An address is validated on the SERVER, against the country it claims.**
  A six-digit PIN is right for India and wrong for the United States.
* **The delivery address on an order is an immutable snapshot.** Editing the
  saved address it came from must not rewrite where a past order went.
* **One committed order produces exactly one admin alert**, linked to that
  order, with read state that survives a page refresh.

These are API tests: they need a backend on port 8010 and DATABASE_URL pointing
at the same database (see the other modules in this directory).
"""
import os
import uuid

import pytest
import requests

from app import addresses
from shipping import SHIPPING, valid_shipping

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@hairshalo.com", "password": "ChangeMe123!"}
MARKER = "pytest-dyn"


# ---------------------------------------------------------------- fixtures

def _api_up():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


live = pytest.mark.skipif(not _api_up(), reason="backend not running on port 8010")


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{API}/auth/login", timeout=10, json=ADMIN)
    if r.status_code != 200:
        pytest.skip("admin credentials rejected")
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def draft_product(auth):
    """A product created through the admin API, cleaned up afterwards."""
    body = {
        "name": f"Test Unit {uuid.uuid4().hex[:6]}",
        "description": "A product created by the regression suite to exercise the workflow.",
        "short_description": "Suite fixture",
        "original_price": "20000",
        "discount_type": "percentage",
        "discount_value": "25",
    }
    r = requests.post(f"{API}/products", headers=auth, timeout=15, json=body)
    assert r.status_code == 201, r.text
    product = r.json()
    yield product
    # Archive rather than delete: the API refuses to delete a product that
    # appears in an order, and a failed teardown must not leave a Published
    # fixture in the live catalog.
    requests.post(f"{API}/products/{product['id']}/status", headers=auth,
                  timeout=10, json={"action": "archive"})
    requests.delete(f"{API}/products/{product['id']}", headers=auth, timeout=10)


def _new_customer(verify=False):
    """Register a customer and return (email, token).

    `verify` confirms the mailbox the way a click in the email would. Order
    history is gated on that, so a test about ORDER OWNERSHIP has to pass it —
    otherwise the request stops at the verification gate (403) and never
    reaches the ownership check it means to exercise.
    """
    email = f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com"
    password = "correct-horse-battery-91"
    requests.post(f"{API}/account/register", timeout=15,
                  json={"email": email, "password": password, "name": "Suite Shopper"})
    if verify:
        from app.database import SessionLocal
        from app import models
        db = SessionLocal()
        try:
            row = db.query(models.Customer).filter(models.Customer.email == email).first()
            if row is None:
                pytest.skip("registration did not create a customer row")
            row.email_verified = True
            db.commit()
        finally:
            db.close()
    r = requests.post(f"{API}/account/login", timeout=15,
                      json={"email": email, "password": password})
    if r.status_code != 200:
        pytest.skip("could not create a customer account")
    return email, r.json()["access_token"]


def _sellable():
    products = requests.get(f"{API}/products", timeout=10).json()
    for p in products:
        for v in p.get("variants", []):
            if v.get("is_available") and (v.get("stock") or 0) > 0:
                return p, v
    pytest.skip("no variant in the catalog has stock")


# =================================================================
# Address validation — unit level, no server needed
# =================================================================

def test_indian_pin_must_be_six_digits_not_starting_with_zero():
    for bad in ("50001", "5000123", "050001", "ABC123", ""):
        with pytest.raises(addresses.AddressError) as exc:
            addresses.validate(valid_shipping(postal_code=bad))
        assert "postal_code" in exc.value.errors
    # The real thing passes.
    assert addresses.validate(SHIPPING)["postal_code"] == "500001"


def test_the_postal_rule_follows_the_country():
    """A six-digit Indian PIN is not a valid US ZIP, and vice versa."""
    with pytest.raises(addresses.AddressError) as exc:
        addresses.validate(valid_shipping(country="United States", state="CA",
                                          postal_code="500001"))
    assert "ZIP Code" in exc.value.errors["postal_code"]

    ok = addresses.validate(valid_shipping(country="United States", state="CA",
                                           postal_code="94103-1234"))
    assert ok["postal_code"] == "94103-1234"


def test_a_country_without_postal_codes_does_not_demand_one():
    """The UAE has no postal code system. Requiring one would be asking for
    something that does not exist."""
    ok = addresses.validate(valid_shipping(country="United Arab Emirates",
                                           state=None, postal_code=None))
    assert ok["postal_code"] is None
    assert ok["country"] == "United Arab Emirates"


def test_an_unknown_country_is_accepted_rather_than_refused():
    """Inventing a format for a country nobody verified would block real
    customers to no benefit."""
    ok = addresses.validate(valid_shipping(country="Latvia", postal_code="LV-1010"))
    assert ok["country"] == "Latvia"


@pytest.mark.parametrize("field,value", [
    ("full_name", ""), ("full_name", "12345"),
    ("line1", ""), ("line1", "x"),
    ("city", ""), ("state", ""),
    ("phone", ""), ("phone", "12"), ("phone", "not-a-phone"),
])
def test_each_required_field_is_checked(field, value):
    with pytest.raises(addresses.AddressError) as exc:
        addresses.validate(valid_shipping(**{field: value}))
    assert field in exc.value.errors


def test_an_indian_phone_must_be_ten_digits():
    addresses.validate(valid_shipping(phone="9876543210"))
    addresses.validate(valid_shipping(phone="+91 98765 43210"))
    with pytest.raises(addresses.AddressError):
        addresses.validate(valid_shipping(phone="+91 98765 4321"))


def test_the_postal_label_is_the_local_one():
    assert addresses.postal_label_for("India") == "PIN Code"
    assert addresses.postal_label_for("United States") == "ZIP Code"
    assert addresses.postal_label_for("United Kingdom") == "Postcode"
    # An unknown country still gets a sensible label rather than a blank one.
    assert addresses.postal_label_for("Latvia") == "Postal Code"


def test_whitespace_is_trimmed_but_the_address_is_not_rewritten():
    """A courier delivers to what the customer wrote. Only runs of whitespace
    are collapsed; nothing else is 'corrected'."""
    out = addresses.validate(valid_shipping(line1="  12   MG   Road  "))
    assert out["line1"] == "12 MG Road"


# =================================================================
# Product admin workflow
# =================================================================

@live
def test_a_new_product_is_a_draft_and_is_not_public(auth, draft_product):
    assert draft_product["status"] == "Draft"
    public = requests.get(f"{API}/products", timeout=10).json()
    assert draft_product["id"] not in [p["id"] for p in public]


@live
def test_the_backend_derives_the_selling_price(draft_product):
    """The admin submits an original price and a discount; the server decides
    what is charged. There is no selling_price input to send."""
    assert float(draft_product["price"]) == 15000.0            # 20000 less 25%
    assert float(draft_product["compare_at_price"]) == 20000.0
    assert draft_product["discount_percent"] == 25


@live
def test_a_product_cannot_be_published_until_it_is_ready(auth, draft_product):
    """No image, no category, no variants — publishing is refused, and the
    reasons are named rather than a generic failure."""
    r = requests.post(f"{API}/products/{draft_product['id']}/status", headers=auth,
                      timeout=10, json={"action": "publish"})
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    assert "cannot publish" in detail
    assert "category" in detail and "variant" in detail


@live
def test_editing_a_draft_persists(auth, draft_product):
    r = requests.put(f"{API}/products/{draft_product['id']}", headers=auth, timeout=10,
                     json={"short_description": "Edited by the suite"})
    assert r.status_code == 200
    again = requests.get(f"{API}/products/{draft_product['id']}/preview",
                         headers=auth, timeout=10).json()
    assert again["short_description"] == "Edited by the suite"


@live
def test_a_draft_product_cannot_be_ordered(draft_product):
    """Even with a valid id, an unpublished product is refused at checkout."""
    r = requests.post(f"{API}/orders", timeout=15, json={
        "shipping": SHIPPING,
        "customer_name": "Suite", "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": draft_product["id"], "quantity": 1}],
    })
    assert r.status_code == 400
    assert "not available" in r.text.lower()


@live
def test_products_are_only_creatable_by_an_admin():
    r = requests.post(f"{API}/products", timeout=10, json={"name": "No Auth"})
    assert r.status_code in (401, 403)


# =================================================================
# Checkout address
# =================================================================

@live
def test_an_order_without_an_address_is_refused():
    product, variant = _sellable()
    r = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Suite", "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    })
    assert r.status_code == 400
    assert "address" in r.json()["detail"].lower()


@live
def test_a_malformed_address_is_refused_with_per_field_reasons():
    product, variant = _sellable()
    r = requests.post(f"{API}/orders", timeout=15, json={
        "shipping": valid_shipping(postal_code="50001", phone="123"),
        "customer_name": "Suite", "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    })
    assert r.status_code == 422
    fields = r.json()["detail"]["fields"]
    assert "postal_code" in fields and "phone" in fields


@live
def test_a_refused_address_consumes_no_stock():
    """An unshippable address should cost nothing — it must not take a stock
    lock on its way to being rejected."""
    product, variant = _sellable()
    before = requests.get(f"{API}/products/{product['id']}", timeout=10).json()
    before_stock = next(v["stock"] for v in before["variants"] if v["id"] == variant["id"])

    requests.post(f"{API}/orders", timeout=15, json={
        "shipping": valid_shipping(postal_code="nonsense"),
        "customer_name": "Suite", "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    })

    after = requests.get(f"{API}/products/{product['id']}", timeout=10).json()
    after_stock = next(v["stock"] for v in after["variants"] if v["id"] == variant["id"])
    assert after_stock == before_stock


@live
def test_the_shipping_snapshot_is_stored_on_the_order(auth):
    product, variant = _sellable()
    r = requests.post(f"{API}/orders", timeout=15, json={
        "shipping": SHIPPING,
        "customer_name": "Suite", "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    })
    assert r.status_code == 201, r.text
    ship = r.json()["shipping"]
    assert ship["structured"] is True
    assert ship["full_name"] == SHIPPING["full_name"]
    assert ship["city"] == "Hyderabad"
    assert ship["postal_code"] == "500001"
    assert ship["postal_label"] == "PIN Code"
    # The legacy text column is written too, so anything still reading it works.
    assert "500001" in r.json()["shipping_address"]


@live
def test_the_country_list_is_served_by_the_backend():
    """The checkout form's postal label comes from the same table the validator
    uses, so the two cannot drift apart."""
    rows = requests.get(f"{API}/orders/shipping-countries", timeout=10).json()
    india = next(c for c in rows if c["name"] == "India")
    assert india["postal_label"] == "PIN Code"
    assert india["requires_state"] is True


# =================================================================
# Saved addresses and ownership
# =================================================================

@live
def test_a_saved_address_is_validated_too():
    """Otherwise an unchecked address could be stored here and then selected at
    checkout by id, walking straight past the validation."""
    _email, token = _new_customer()
    r = requests.post(f"{API}/account/addresses", timeout=15,
                      headers={"Authorization": f"Bearer {token}"},
                      json=valid_shipping(postal_code="99"))
    assert r.status_code == 422
    assert "postal_code" in r.json()["detail"]["fields"]


@live
def test_a_customer_cannot_ship_to_another_customers_saved_address():
    """The headline IDOR case. 404, not 403 — "you may not see this" would
    confirm the address exists."""
    _email_a, token_a = _new_customer()
    _email_b, token_b = _new_customer()

    created = requests.post(f"{API}/account/addresses", timeout=15,
                            headers={"Authorization": f"Bearer {token_a}"},
                            json=valid_shipping())
    assert created.status_code == 201
    address_id = created.json()["id"]

    product, variant = _sellable()
    r = requests.post(f"{API}/orders", timeout=15,
                      headers={"Authorization": f"Bearer {token_b}"},
                      json={
                          "shipping_address_id": address_id,
                          "customer_name": "Thief",
                          "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
                          "items": [{"product_id": product["id"],
                                     "variant_id": variant["id"], "quantity": 1}],
                      })
    assert r.status_code == 404


@live
def test_a_guest_cannot_use_a_saved_address_id():
    _email, token = _new_customer()
    created = requests.post(f"{API}/account/addresses", timeout=15,
                            headers={"Authorization": f"Bearer {token}"},
                            json=valid_shipping())
    address_id = created.json()["id"]

    product, variant = _sellable()
    r = requests.post(f"{API}/orders", timeout=15, json={
        "shipping_address_id": address_id,
        "customer_name": "Guest", "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    })
    assert r.status_code == 404


@live
def test_editing_a_saved_address_does_not_rewrite_a_past_order(auth):
    """THE snapshot test. An order shipped to Hyderabad stays shipped to
    Hyderabad after the customer moves to Chennai."""
    _email, token = _new_customer()
    headers = {"Authorization": f"Bearer {token}"}

    created = requests.post(f"{API}/account/addresses", timeout=15, headers=headers,
                            json=valid_shipping()).json()

    product, variant = _sellable()
    order = requests.post(f"{API}/orders", timeout=15, headers=headers, json={
        "shipping_address_id": created["id"],
        "customer_name": "Mover", "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    })
    assert order.status_code == 201, order.text
    order_id = order.json()["id"]
    assert order.json()["shipping"]["city"] == "Hyderabad"

    # The customer moves.
    moved = requests.put(f"{API}/account/addresses/{created['id']}", timeout=15,
                         headers=headers,
                         json=valid_shipping(city="Chennai", state="Tamil Nadu",
                                             postal_code="600001",
                                             line1="7 Cathedral Road"))
    assert moved.status_code == 200
    assert moved.json()["city"] == "Chennai"

    # The order has not moved with them.
    again = requests.get(f"{API}/orders/{order_id}", headers=auth, timeout=10).json()
    assert again["shipping"]["city"] == "Hyderabad"
    assert again["shipping"]["postal_code"] == "500001"
    assert again["shipping"]["line1"] == SHIPPING["line1"]


@live
def test_deleting_a_saved_address_does_not_affect_a_past_order(auth):
    _email, token = _new_customer()
    headers = {"Authorization": f"Bearer {token}"}
    created = requests.post(f"{API}/account/addresses", timeout=15, headers=headers,
                            json=valid_shipping()).json()

    product, variant = _sellable()
    order = requests.post(f"{API}/orders", timeout=15, headers=headers, json={
        "shipping_address_id": created["id"],
        "customer_name": "Gone", "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    }).json()

    assert requests.delete(f"{API}/account/addresses/{created['id']}", timeout=10,
                           headers=headers).status_code == 204

    again = requests.get(f"{API}/orders/{order['id']}", headers=auth, timeout=10).json()
    assert again["shipping"]["city"] == "Hyderabad"
    assert again["shipping"]["structured"] is True


@live
def test_a_customer_cannot_read_another_customers_order(auth):
    # Both verified, so the request reaches the OWNERSHIP check rather than
    # stopping at the email-verification gate.
    _email_a, token_a = _new_customer(verify=True)
    _email_b, token_b = _new_customer(verify=True)
    product, variant = _sellable()
    order = requests.post(f"{API}/orders", timeout=15,
                          headers={"Authorization": f"Bearer {token_a}"}, json={
                              "shipping": SHIPPING,
                              "customer_name": "A", "customer_email": _email_a,
                              "items": [{"product_id": product["id"],
                                         "variant_id": variant["id"], "quantity": 1}],
                          }).json()
    r = requests.get(f"{API}/account/orders/{order['id']}", timeout=10,
                     headers={"Authorization": f"Bearer {token_b}"})
    assert r.status_code == 404


@live
def test_a_customer_cannot_change_an_order_status():
    """Status is an admin decision, enforced server-side."""
    _email, token = _new_customer()
    product, variant = _sellable()
    order = requests.post(f"{API}/orders", timeout=15,
                          headers={"Authorization": f"Bearer {token}"}, json={
                              "shipping": SHIPPING,
                              "customer_name": "C", "customer_email": _email,
                              "items": [{"product_id": product["id"],
                                         "variant_id": variant["id"], "quantity": 1}],
                          }).json()
    r = requests.put(f"{API}/orders/{order['id']}/status", timeout=10,
                     headers={"Authorization": f"Bearer {token}"},
                     json={"status": "Delivered"})
    assert r.status_code in (401, 403)


# =================================================================
# Admin order notifications
# =================================================================

@live
def test_a_new_order_creates_exactly_one_admin_alert(auth):
    product, variant = _sellable()
    order = requests.post(f"{API}/orders", timeout=15, json={
        "shipping": SHIPPING,
        "customer_name": "Alert Test",
        "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    }).json()

    rows = requests.get(f"{API}/notifications", headers=auth, timeout=10, params={
        "event_type": "admin.order_placed", "reference_id": order["id"]}).json()
    assert len(rows) == 1
    assert order["order_number"] in rows[0]["subject"]
    assert rows[0]["reference_type"] == "order"
    assert rows[0]["is_read"] is False


@live
def test_the_alert_carries_the_shipping_details(auth):
    """An operations person should be able to act on the alert itself."""
    product, variant = _sellable()
    order = requests.post(f"{API}/orders", timeout=15, json={
        "shipping": SHIPPING,
        "customer_name": "Detail Test",
        "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    }).json()
    row = requests.get(f"{API}/notifications", headers=auth, timeout=10, params={
        "event_type": "admin.order_placed", "reference_id": order["id"]}).json()[0]
    detail = requests.get(f"{API}/notifications/{row['id']}", headers=auth, timeout=10).json()
    body = detail["body_text"]
    for expected in ("NEW ORDER RECEIVED", order["order_number"], "Hyderabad",
                     "Telangana", "500001", SHIPPING["phone"]):
        assert expected in body, f"{expected!r} missing from the alert body"


@live
def test_a_rejected_order_creates_no_alert(auth):
    """A notification is a consequence of a COMMITTED fact. An order that was
    refused must leave no trace saying it arrived."""
    before = requests.get(f"{API}/notifications/summary", headers=auth, timeout=10).json()
    product, variant = _sellable()
    r = requests.post(f"{API}/orders", timeout=15, json={
        "shipping": valid_shipping(postal_code="not-a-pin"),
        "customer_name": "Never Happened",
        "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    })
    assert r.status_code == 422
    after = requests.get(f"{API}/notifications/summary", headers=auth, timeout=10).json()
    assert after["unread_orders"] == before["unread_orders"]


@live
def test_the_alert_links_back_to_its_order(auth):
    product, variant = _sellable()
    order = requests.post(f"{API}/orders", timeout=15, json={
        "shipping": SHIPPING,
        "customer_name": "Link Test",
        "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    }).json()
    row = requests.get(f"{API}/notifications", headers=auth, timeout=10, params={
        "event_type": "admin.order_placed", "reference_id": order["id"]}).json()[0]
    # Following the reference reaches the order the alert is about.
    linked = requests.get(f"{API}/orders/{row['reference_id']}", headers=auth, timeout=10)
    assert linked.status_code == 200
    assert linked.json()["order_number"] == order["order_number"]


@live
def test_read_state_is_stored_server_side(auth):
    """So a refresh cannot reset a badge and two admin sessions agree."""
    product, variant = _sellable()
    order = requests.post(f"{API}/orders", timeout=15, json={
        "shipping": SHIPPING,
        "customer_name": "Read Test",
        "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    }).json()
    row = requests.get(f"{API}/notifications", headers=auth, timeout=10, params={
        "event_type": "admin.order_placed", "reference_id": order["id"]}).json()[0]
    assert row["is_read"] is False

    before = requests.get(f"{API}/notifications/summary", headers=auth, timeout=10).json()
    summary = requests.post(f"{API}/notifications/read", headers=auth, timeout=10,
                            json={"ids": [row["id"]]}).json()
    assert summary["unread_orders"] == before["unread_orders"] - 1

    # A fresh request — a new "session" — sees it read.
    again = requests.get(f"{API}/notifications/{row['id']}", headers=auth, timeout=10).json()
    assert again["is_read"] is True

    # Marking read twice does not double-count.
    requests.post(f"{API}/notifications/read", headers=auth, timeout=10,
                  json={"ids": [row["id"]]})
    after = requests.get(f"{API}/notifications/summary", headers=auth, timeout=10).json()
    assert after["unread_orders"] == summary["unread_orders"]

    # And it can be put back. Checked through the LIST endpoint, then again
    # through the detail one — reading a message must not silently re-mark it,
    # or "mark unread" would be a button that undoes itself.
    requests.post(f"{API}/notifications/{row['id']}/unread", headers=auth, timeout=10)
    listed = requests.get(f"{API}/notifications", headers=auth, timeout=10, params={
        "event_type": "admin.order_placed", "reference_id": order["id"]}).json()[0]
    assert listed["is_read"] is False
    detail = requests.get(f"{API}/notifications/{row['id']}", headers=auth, timeout=10).json()
    assert detail["is_read"] is False


@live
def test_the_notification_summary_needs_an_admin():
    assert requests.get(f"{API}/notifications/summary", timeout=10).status_code in (401, 403)


# =================================================================
# Cancellation returns stock through the ledger
# =================================================================

@live
def test_cancelling_an_order_returns_the_stock(auth):
    product, variant = _sellable()
    before = next(v["stock"] for v in
                  requests.get(f"{API}/products/{product['id']}", timeout=10).json()["variants"]
                  if v["id"] == variant["id"])

    order = requests.post(f"{API}/orders", timeout=15, json={
        "shipping": SHIPPING,
        "customer_name": "Cancel Test",
        "customer_email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    }).json()

    during = next(v["stock"] for v in
                  requests.get(f"{API}/products/{product['id']}", timeout=10).json()["variants"]
                  if v["id"] == variant["id"])
    assert during == before - 1

    r = requests.put(f"{API}/orders/{order['id']}/status", headers=auth, timeout=15,
                     json={"status": "Cancelled"})
    assert r.status_code == 200, r.text

    after = next(v["stock"] for v in
                 requests.get(f"{API}/products/{product['id']}", timeout=10).json()["variants"]
                 if v["id"] == variant["id"])
    assert after == before


# =================================================================
# Site content
# =================================================================

@live
def test_the_storefront_content_is_public_and_complete():
    content = requests.get(f"{API}/site-content", timeout=10).json()
    for key in ("announcement", "hero", "faq", "footer", "business", "navigation"):
        assert key in content, f"{key} missing from the storefront content"
    assert content["hero"]["heading_lines"]


@live
def test_content_blocks_are_admin_only_to_write(auth):
    assert requests.get(f"{API}/site-content/blocks", timeout=10).status_code in (401, 403)
    assert requests.put(f"{API}/site-content/blocks/promo", timeout=10,
                        json={"payload": {"heading": "hacked"}}).status_code in (401, 403)
    assert requests.get(f"{API}/site-content/blocks", headers=auth,
                        timeout=10).status_code == 200


@live
def test_editing_a_block_changes_what_the_storefront_serves(auth):
    marker = f"Suite edit {uuid.uuid4().hex[:6]}"
    try:
        r = requests.put(f"{API}/site-content/blocks/promo", headers=auth, timeout=10,
                         json={"payload": {"heading": marker}})
        assert r.status_code == 200
        assert r.json()["updated_by"] == ADMIN["email"]

        public = requests.get(f"{API}/site-content", timeout=10).json()
        assert public["promo"]["heading"] == marker
        # A partial update must not drop the fields it did not mention.
        assert public["promo"]["cta_label"]
    finally:
        requests.post(f"{API}/site-content/blocks/promo/reset", headers=auth, timeout=10)


@live
def test_a_block_can_be_switched_off_without_losing_its_text(auth):
    try:
        requests.put(f"{API}/site-content/blocks/promo", headers=auth, timeout=10,
                     json={"is_active": False})
        public = requests.get(f"{API}/site-content", timeout=10).json()
        assert "promo" not in public                    # hidden from the storefront
        blocks = requests.get(f"{API}/site-content/blocks", headers=auth, timeout=10).json()
        promo = next(b for b in blocks if b["key"] == "promo")
        assert promo["payload"]["heading"]              # the copy is still there
    finally:
        requests.put(f"{API}/site-content/blocks/promo", headers=auth, timeout=10,
                     json={"is_active": True})


@live
def test_an_unknown_block_is_refused_rather_than_created(auth):
    """A typo'd key would otherwise write a row nothing renders, leaving the
    admin convinced they had edited something."""
    r = requests.put(f"{API}/site-content/blocks/not-a-real-block", headers=auth,
                     timeout=10, json={"payload": {"x": 1}})
    assert r.status_code == 404


@live
def test_categories_carry_their_storefront_presentation(auth):
    """The collection cards are the categories, so the card's tagline and image
    live on the category rather than in the page markup."""
    cats = requests.get(f"{API}/categories", timeout=10).json()
    assert cats, "no categories — the storefront collections section would be empty"
    for c in cats:
        assert "tagline" in c and "image_url" in c
        assert "product_count" in c
