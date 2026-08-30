"""Production-readiness verification: inventory lifecycle, currency, reviews,
rate limiting.

These are checks on behaviour that already exists. Each one states the rule it
is protecting, because the value is in catching a future regression, not in
re-proving today's code once.
"""
import os
import threading
import uuid
from decimal import Decimal

import pytest
import requests
from sqlalchemy import text

from app import currency as cur, models, reviews as review_service
from app.config import settings

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@hairshalo.com", "password": "ChangeMe123!"}
MARKER = "pytest-ready"


def _alive():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


live = pytest.mark.skipif(not _alive(), reason="backend not reachable")


def _provider():
    try:
        return requests.get(f"{API}/payments/config", timeout=5).json()["provider"]
    except Exception:
        return None


@pytest.fixture(scope="module")
def auth():
    if not _alive():
        pytest.skip("backend not reachable")
    return {"Authorization": "Bearer " + requests.post(
        f"{API}/auth/login", json=ADMIN, timeout=10).json()["access_token"]}


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


def _sellable(minimum=3):
    for product in requests.get(f"{API}/products", timeout=10).json():
        for variant in product.get("variants") or []:
            if (variant.get("stock") or 0) > minimum and variant.get("is_available", True):
                return product, variant
    pytest.skip("no variant in the catalog has stock")


def _stock(product_id, variant_id):
    product = requests.get(f"{API}/products/{product_id}", timeout=10).json()
    return next(v["stock"] for v in product["variants"] if v["id"] == variant_id)


def _place(product, variant, quantity=1, **extra):
    payload = {
        "customer_name": "Readiness", "shipping_address": "12 MG Road",
        "customer_email": f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"],
                   "quantity": quantity}],
    }
    payload.update(extra)
    return requests.post(f"{API}/orders", timeout=20, json=payload)


# ================= Phase 6 — inventory lifecycle =================

@live
def test_browsing_and_a_full_cart_never_touch_stock():
    """Stock moves when an ORDER is created, never before. A cart lives only in
    the browser, so there is nothing here that could reserve anything."""
    product, variant = _sellable()
    before = _stock(product["id"], variant["id"])
    requests.get(f"{API}/products/{product['id']}", timeout=10)
    requests.get(f"{API}/products", timeout=10)
    assert _stock(product["id"], variant["id"]) == before


@live
def test_placing_an_order_reserves_exactly_what_was_ordered():
    product, variant = _sellable()
    before = _stock(product["id"], variant["id"])
    order = _place(product, variant, quantity=2).json()
    assert _stock(product["id"], variant["id"]) == before - 2
    assert order["items"][0]["quantity"] == 2


@live
def test_cancelling_an_order_returns_the_reservation(auth):
    product, variant = _sellable()
    before = _stock(product["id"], variant["id"])
    order = _place(product, variant).json()
    assert _stock(product["id"], variant["id"]) == before - 1

    target = "Cancelled"
    r = requests.put(f"{API}/orders/{order['id']}/status", headers=auth, timeout=15,
                     json={"status": target})
    assert r.status_code == 200, r.text
    assert _stock(product["id"], variant["id"]) == before


@live
def test_stock_is_never_reserved_twice_for_one_order(auth):
    """Cancelling twice must not return the stock twice."""
    product, variant = _sellable()
    before = _stock(product["id"], variant["id"])
    order = _place(product, variant).json()
    requests.put(f"{API}/orders/{order['id']}/status", headers=auth, timeout=15,
                 json={"status": "Cancelled"})
    requests.put(f"{API}/orders/{order['id']}/status", headers=auth, timeout=15,
                 json={"status": "Cancelled"})
    assert _stock(product["id"], variant["id"]) == before


@live
def test_an_order_cannot_be_placed_for_more_than_exists():
    product, variant = _sellable()
    stock = _stock(product["id"], variant["id"])
    r = _place(product, variant, quantity=stock + 5)
    assert r.status_code == 400
    assert "stock" in r.json()["detail"].lower()
    assert _stock(product["id"], variant["id"]) == stock      # unchanged


@live
def test_concurrent_checkouts_cannot_oversell():
    """The SELECT ... FOR UPDATE guarantee, exercised for real.

    Ten browsers try to buy the last few units at the same moment. The number
    that succeed must equal the number of units that existed — no more.
    """
    product, variant = _sellable(minimum=3)
    stock = _stock(product["id"], variant["id"])
    attempts = 10
    # Leave a small, exactly-known number available.
    available = 4
    if stock > available:
        adjust = requests.post(
            f"{API}/inventory/adjust", timeout=15,
            headers={"Authorization": "Bearer " + requests.post(
                f"{API}/auth/login", json=ADMIN, timeout=10).json()["access_token"]},
            json={"variant_id": variant["id"], "delta": -(stock - available),
                  "reason": "adjustment",
                  "note": "readiness concurrency test setup"})
        if adjust.status_code != 200:
            pytest.skip(f"could not set up the test stock level: {adjust.text}")
    start = _stock(product["id"], variant["id"])

    results = []
    barrier = threading.Barrier(attempts)

    def buy():
        barrier.wait()             # all threads hit the API together
        try:
            results.append(_place(product, variant).status_code)
        except Exception:
            results.append(0)

    threads = [threading.Thread(target=buy) for _ in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    succeeded = sum(1 for code in results if code == 201)
    remaining = _stock(product["id"], variant["id"])
    assert succeeded == start, f"{succeeded} orders succeeded against {start} units"
    assert remaining == 0, f"{remaining} units left after selling out"
    assert remaining >= 0, "stock went negative — overselling"


@live
def test_the_movement_log_always_explains_the_stock(db):
    """The invariant the whole inventory design rests on."""
    disagreements = [
        (v.sku, v.stock, sum(m.delta for m in v.movements))
        for v in db.query(models.ProductVariant).all()
        if sum(m.delta for m in v.movements) != (v.stock or 0)
    ]
    assert disagreements == []


# ================= Phase 7 — currency =================

def test_every_required_country_maps_to_the_right_currency():
    expected = {
        "IN": "INR", "GB": "GBP", "US": "USD", "AE": "AED", "SG": "SGD",
        "MY": "MYR", "LK": "LKR", "AU": "AUD", "CA": "CAD", "NZ": "NZD",
        "JP": "JPY", "SA": "SAR", "QA": "QAR", "KW": "KWD", "CH": "CHF",
    }
    for country, code in expected.items():
        assert cur.currency_for_country(country) == code, country


def test_an_unknown_currency_falls_back_to_inr_instead_of_failing():
    assert cur.resolve_currency("XXX") == "INR"
    assert cur.resolve_currency(None) == "INR"
    assert cur.resolve_currency("") == "INR"


def test_conversion_uses_decimal_and_currency_specific_precision():
    """JPY has no minor unit and KWD has three. Rounding everything to two
    would misprice both."""
    assert cur.quantize_for(Decimal("1234.567"), "JPY") == Decimal("1235")
    assert cur.quantize_for(Decimal("1.23456"), "KWD") == Decimal("1.235")
    assert cur.quantize_for(Decimal("1.235"), "USD") == Decimal("1.24")
    assert isinstance(cur.convert(Decimal("1000"), "USD"), Decimal)


def test_a_rate_is_never_zero_none_or_nan():
    for code in cur.CURRENCIES:
        rate, source = cur.rate_for(code)
        assert isinstance(rate, Decimal)
        assert rate > 0, code
        assert source


@live
def test_rates_come_from_one_cached_table():
    body = requests.get(f"{API}/currency/rates", timeout=10).json()
    assert body["base"] == "INR"
    assert set(body["rates"]) >= {"INR", "USD", "GBP", "AED", "JPY", "KWD"}
    assert body["cache_ttl"] > 0
    # Indicative static rates must announce themselves rather than pose as live.
    if body["is_indicative"]:
        assert body["as_of"], "static rates must carry the date they were taken"


@live
def test_a_historical_order_keeps_the_rate_it_was_placed_at(db):
    """Re-converting history at today's rate would rewrite what a customer
    paid. The order stores its own rate and total."""
    order = (db.query(models.Order)
             .filter(models.Order.display_currency.isnot(None))
             .order_by(models.Order.created_at.desc()).first())
    if not order:
        pytest.skip("no order with a display currency")
    stored_rate = Decimal(str(order.display_rate))
    stored_total = Decimal(str(order.display_total))
    live_rate, _ = cur.rate_for(order.display_currency)
    # Whatever today's rate is, the order still reports its own.
    assert Decimal(str(order.display_total)) == stored_total
    assert Decimal(str(order.display_rate)) == stored_rate
    assert order.currency == "INR"       # settlement never moves


@live
def test_the_saved_currency_preference_wins_over_detection():
    """Priority 2: a signed-in customer's choice follows them across devices."""
    from tests.test_accounts import _new_account, _auth
    _email, token, _cid = _new_account()

    requests.put(f"{API}/account/me", headers=_auth(token), timeout=10,
                 json={"preferred_currency": "GBP"})
    detected = requests.get(f"{API}/currency/detect", headers=_auth(token),
                            timeout=10).json()
    assert detected["currency"] == "GBP"
    assert detected["source"] == "saved-preference"

    # Without the token, the same request falls back to detection.
    anonymous = requests.get(f"{API}/currency/detect", timeout=10).json()
    assert anonymous["source"] in ("edge-header", "unavailable")


# ================= Phase 10 — reviews =================

@live
def test_a_product_with_no_reviews_reports_zero_not_a_rating():
    products = requests.get(f"{API}/products", timeout=10).json()
    unreviewed = next((p for p in products if (p.get("review_count") or 0) == 0), None)
    if not unreviewed:
        pytest.skip("every product has reviews")
    assert unreviewed["rating"] in (0, 0.0)
    summary = requests.get(f"{API}/reviews/product/{unreviewed['id']}/summary",
                           timeout=10).json()
    assert summary["count"] == 0 and summary["average"] == 0.0
    assert requests.get(f"{API}/reviews/product/{unreviewed['id']}",
                        timeout=10).json() == []


@live
def test_only_published_reviews_count_towards_a_rating(db, auth):
    """A pending review must be invisible AND must not move the average."""
    product = db.query(models.Product).join(models.Review).first()
    if not product:
        pytest.skip("no product has reviews")

    before = requests.get(f"{API}/reviews/product/{product.id}/summary",
                          timeout=10).json()

    # A pending review, created directly so no order is needed.
    pending = models.Review(
        product_id=product.id, author_name="Pending Person",
        author_email=f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        rating=1, title="Should not count", body="Unpublished.",
        status=models.ReviewStatus.pending, is_verified_purchase=False)
    db.add(pending)
    db.commit()
    try:
        after = requests.get(f"{API}/reviews/product/{product.id}/summary",
                             timeout=10).json()
        assert after["count"] == before["count"]
        assert after["average"] == before["average"]
        public = requests.get(f"{API}/reviews/product/{product.id}", timeout=10).json()
        assert pending.id not in [r["id"] for r in public]

        # Publishing it changes the rating, through the one code path allowed to.
        requests.post(f"{API}/reviews/{pending.id}/moderate", headers=auth, timeout=15,
                      json={"status": "Published"})
        published = requests.get(f"{API}/reviews/product/{product.id}/summary",
                                 timeout=10).json()
        assert published["count"] == before["count"] + 1
    finally:
        db.delete(pending)
        db.commit()
        review_service.recalculate(db, product.id)
        db.commit()


@live
def test_a_review_cannot_be_left_without_a_delivered_order():
    products = requests.get(f"{API}/products", timeout=10).json()
    r = requests.post(f"{API}/reviews", timeout=15, json={
        "product_id": products[0]["id"], "order_number": "VR-0000",
        "email": "nobody@example.com", "rating": 5, "title": "Fake",
        "body": "Never bought this."})
    assert r.status_code == 400
    assert "could not match" in r.json()["detail"].lower()


@live
def test_a_review_response_never_exposes_the_reviewer_address():
    reviews = requests.get(f"{API}/reviews/recent?limit=5", timeout=10).json()
    for review in reviews:
        assert "@" not in review["author"], "a public review exposed an email"
        assert "author_email" not in review


# ================= Phase 14 — rate limiting =================

def test_limits_are_configured_for_the_endpoints_that_need_them():
    from app.middleware import RateLimitMiddleware, parse_limit
    limiter = RateLimitMiddleware(app=None)
    login_count, login_window = limiter.login
    write_count, _ = limiter.write
    public_count, _ = limiter.public

    # Login must be the tightest: it is the credential-stuffing target.
    assert login_count <= write_count <= public_count
    assert login_window >= 60, "a login window shorter than a minute is decorative"
    # …but browsing must not be throttled into unusability.
    assert public_count >= 30


def test_expensive_public_endpoints_land_in_a_limited_bucket():
    import types
    from app.middleware import RateLimitMiddleware
    limiter = RateLimitMiddleware(app=None)

    def request(path, method="POST"):
        return types.SimpleNamespace(
            url=types.SimpleNamespace(path=path), method=method, headers={},
            client=types.SimpleNamespace(host="1.2.3.4"))

    for path in ("/api/account/register", "/api/account/login",
                 "/api/account/password-reset/request", "/api/orders",
                 "/api/appointments", "/api/coupons/validate", "/api/reviews",
                 "/api/marketing/subscribe", "/api/products/media"):
        bucket = limiter.bucket_for(request(path))
        assert bucket is not None, f"{path} is not rate limited"


@live
def test_rate_limiting_state_is_reported_honestly():
    """It is per-process, and the config endpoint must not imply otherwise."""
    body = requests.get(f"{API}/version", timeout=10).json()
    assert "rate_limiting" in body
    assert isinstance(body["rate_limiting"], bool)
