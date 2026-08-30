"""Growth tests — reviews, loyalty and marketing consent.

The rule under test throughout: a growth number is never manufactured. Every
star traces to a review of a real purchase, every point to a ledger entry, and
every marketing email to a confirmed opt-in.

Layers, as elsewhere in this suite: pure unit, then database, then HTTP against
a backend running on port 8010.
"""
import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import requests
from sqlalchemy import text

from app import loyalty, marketing, models, notifications as notify, reviews
from app.config import settings

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@hairshalo.com", "password": "ChangeMe123!"}
MARKER = "pytest-growth"


# ---------------- unit: loyalty arithmetic ----------------

def test_points_are_earned_on_completed_hundreds_only():
    """Rounding up would mint value out of nothing on every order."""
    assert loyalty.points_for_spend(12000) == 120
    assert loyalty.points_for_spend(12099) == 120
    assert loyalty.points_for_spend(99) == 0
    assert loyalty.points_for_spend(0) == 0


def test_points_have_a_server_defined_value():
    assert loyalty.points_value(120) == Decimal("120.00")
    assert loyalty.points_value(0) == Decimal("0.00")
    assert loyalty.points_value(-50) == Decimal("0.00")     # never negative


def test_redemption_is_capped_at_a_share_of_the_order():
    """A large balance discounts an order; it does not replace paying for one."""
    customer = models.Customer(id="c1", name="A", email="a@b.c", loyalty_points=5000)
    # 20% of a ₹10,000 basket = ₹2,000 = 2,000 points at ₹1 each.
    assert loyalty.max_redeemable(None, customer, Decimal("10000")) == 2000
    # …and never more than the balance.
    customer.loyalty_points = 300
    assert loyalty.max_redeemable(None, customer, Decimal("10000")) == 300


def test_redemption_clamps_instead_of_over_discounting():
    customer = models.Customer(id="c1", name="A", email="a@b.c", loyalty_points=5000)
    spent, discount = loyalty.redeem_for_order(None, customer, 4000, Decimal("10000"))
    assert spent == 2000                       # clamped to the 20% ceiling
    assert discount == Decimal("2000.00")


def test_redemption_refuses_more_points_than_exist():
    customer = models.Customer(id="c1", name="A", email="a@b.c", loyalty_points=100)
    with pytest.raises(loyalty.LoyaltyError) as exc:
        loyalty.redeem_for_order(None, customer, 500, Decimal("10000"))
    assert "100 points are available" in str(exc.value)


def test_a_basket_too_small_to_discount_says_so():
    customer = models.Customer(id="c1", name="A", email="a@b.c", loyalty_points=500)
    with pytest.raises(loyalty.LoyaltyError):
        loyalty.redeem_for_order(None, customer, 100, Decimal("4"))


def test_asking_for_no_points_is_not_an_error():
    customer = models.Customer(id="c1", name="A", email="a@b.c", loyalty_points=500)
    assert loyalty.redeem_for_order(None, customer, 0, Decimal("10000")) == (0, Decimal("0.00"))


# ---------------- unit: reviews ----------------

def test_a_review_shows_a_display_name_never_an_address():
    review = models.Review(author_name="Bhargavi Ramanathan", author_email="b@example.com",
                           rating=5)
    assert review.author_display == "Bhargavi R."
    assert models.Review(author_name="Asha", author_email="a@b.c", rating=5).author_display == "Asha"
    assert models.Review(author_name="  ", author_email="a@b.c", rating=5).author_display \
        == "Verified customer"


def test_ratings_outside_one_to_five_are_refused():
    for bad in (0, 6, -1, None):
        with pytest.raises(reviews.ReviewError):
            reviews._validate(bad, "body", "title")
    reviews._validate(5, "body", "title")       # valid, raises nothing


def test_only_delivered_orders_can_be_reviewed():
    """A review of something that has not arrived is not a verified purchase."""
    assert reviews.REVIEWABLE_ORDER_STATUSES == {models.OrderStatus.delivered}


# ---------------- unit: marketing ----------------

def test_confirmation_links_are_signed_per_address():
    url = marketing.confirm_url("Person@Example.com")
    token = url.split("token=")[1]
    assert notify.verify_unsubscribe_token(token) == "person@example.com"


def test_email_normalisation_is_consistent():
    assert marketing.normalise("  Person@Example.COM ") == "person@example.com"
    assert marketing.normalise(None) == ""


# ---------------- database ----------------

@pytest.fixture
def db():
    """A session that removes the rows these tests create."""
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
        # Only customers with no orders: the HTTP tests below place real orders
        # for MARKER addresses, and those customers must outlive this teardown
        # or the delete violates the orders foreign key.
        orphans = [
            c.id for c in session.query(models.Customer).filter(
                models.Customer.email.like(f"%{MARKER}%")).all()
            if not c.orders
        ]
        if orphans:
            session.query(models.LoyaltyTransaction).filter(
                models.LoyaltyTransaction.customer_id.in_(orphans)).delete(
                    synchronize_session=False)
            session.query(models.Customer).filter(
                models.Customer.id.in_(orphans)).delete(synchronize_session=False)
        session.query(models.MarketingSubscriber).filter(
            models.MarketingSubscriber.email.like(f"%{MARKER}%")).delete(
                synchronize_session=False)
        session.query(models.NotificationSuppression).filter(
            models.NotificationSuppression.email.like(f"%{MARKER}%")).delete(
                synchronize_session=False)
        session.query(models.Notification).filter(
            models.Notification.recipient.like(f"%{MARKER}%")).delete(
                synchronize_session=False)
        session.commit()
        session.close()


def _customer(db, points=0):
    customer = models.Customer(
        name="Ledger Test", email=f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com")
    db.add(customer)
    db.flush()
    if points:
        loyalty.apply(db, customer, points, models.LoyaltyReason.adjustment,
                      note="test opening balance", actor="pytest")
        db.flush()
    return customer


def test_the_ledger_always_explains_the_balance(db):
    customer = _customer(db, points=500)
    loyalty.apply(db, customer, -200, models.LoyaltyReason.redeemed, note="spent")
    loyalty.apply(db, customer, 50, models.LoyaltyReason.earned, note="earned")
    db.flush()

    entries = db.query(models.LoyaltyTransaction).filter(
        models.LoyaltyTransaction.customer_id == customer.id).all()
    assert sum(e.delta for e in entries) == customer.loyalty_points == 350
    assert entries[-1].balance_after == 350


def test_a_balance_cannot_go_negative_by_accident(db):
    customer = _customer(db, points=100)
    with pytest.raises(loyalty.LoyaltyError):
        loyalty.apply(db, customer, -500, models.LoyaltyReason.redeemed)
    assert customer.loyalty_points == 100

    # An admin correction may, deliberately — a chargeback is a fact, not a
    # reason to leave phantom points in circulation.
    loyalty.apply(db, customer, -500, models.LoyaltyReason.adjustment,
                  note="chargeback", actor="admin", allow_negative=True)
    assert customer.loyalty_points == -400


def test_publishing_a_review_is_what_moves_a_rating(db):
    product = db.query(models.Product).filter(
        models.Product.status == models.ProductStatus.published).first()
    if not product:
        pytest.skip("no published product to review")

    before_rating, before_count = product.rating, product.review_count
    review = models.Review(
        product_id=product.id, author_name="Pending Person",
        author_email=f"{MARKER}-pending@example.com", rating=1,
        body="Held for moderation", status=models.ReviewStatus.pending)
    db.add(review)
    db.flush()

    # Pending: the rating must not move at all.
    reviews.recalculate(db, product.id)
    assert (product.rating, product.review_count) == (before_rating, before_count)

    reviews.moderate(db, review, models.ReviewStatus.published, actor="pytest")
    assert product.review_count == before_count + 1

    # Rejecting it takes the contribution straight back out.
    reviews.moderate(db, review, models.ReviewStatus.rejected, actor="pytest")
    assert (product.rating, product.review_count) == (before_rating, before_count)
    db.rollback()


def test_subscribing_creates_consent_pending_not_consent(db):
    address = f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com"
    subscriber, queued = marketing.subscribe(db, address, name="Test", source="pytest")
    db.flush()

    assert subscriber.status == models.SubscriberStatus.pending
    assert subscriber.confirmed_at is None
    assert queued is True
    # Exactly one email, and it is the confirmation.
    rows = db.query(models.Notification).filter(
        models.Notification.recipient == address).all()
    assert [r.event_type for r in rows] == ["marketing.confirm"]
    # A pending subscriber is not in the audience.
    assert subscriber not in marketing.confirmed_subscribers(db)


def test_only_confirming_the_link_grants_consent(db):
    address = f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com"
    marketing.subscribe(db, address, source="pytest")
    db.flush()

    with pytest.raises(marketing.MarketingError):
        marketing.confirm(db, "forged.token")

    subscriber = marketing.confirm(db, notify.unsubscribe_token(address))
    db.flush()
    assert subscriber.status == models.SubscriberStatus.confirmed
    assert subscriber.confirmed_at is not None
    assert subscriber.is_mailable


def test_a_campaign_reaches_only_confirmed_subscribers(db):
    confirmed = f"{MARKER}-yes-{uuid.uuid4().hex[:6]}@example.com"
    pending = f"{MARKER}-no-{uuid.uuid4().hex[:6]}@example.com"
    marketing.subscribe(db, confirmed, source="pytest")
    marketing.subscribe(db, pending, source="pytest")
    marketing.confirm(db, notify.unsubscribe_token(confirmed))
    db.flush()

    campaign = models.Campaign(
        name="Test campaign", subject="New arrivals",
        body="Two new units landed this week.", status=models.CampaignStatus.draft)
    db.add(campaign)
    db.flush()
    marketing.send_campaign(db, campaign, actor="pytest")
    db.flush()

    recipients = {
        r.recipient for r in db.query(models.Notification).filter(
            models.Notification.reference_id == campaign.id).all()
    }
    assert confirmed in recipients
    assert pending not in recipients            # the headline requirement
    assert campaign.status == models.CampaignStatus.sent


def test_an_unsubscribe_survives_the_subscriber_row(db):
    """Send time checks the suppression list, so an opt-out holds even if this
    table is edited by hand."""
    address = f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com"
    marketing.subscribe(db, address, source="pytest")
    marketing.confirm(db, notify.unsubscribe_token(address))
    db.flush()

    marketing.unsubscribe(db, address)
    db.flush()
    assert notify.is_suppressed(db, address, models.NotificationCategory.marketing)
    # Transactional mail is unaffected: a receipt is not a newsletter.
    assert not notify.is_suppressed(db, address, models.NotificationCategory.transactional)


def test_a_hard_bounce_cannot_be_re_subscribed(db):
    address = f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com"
    notify.suppress(db, address, models.SuppressionScope.all, reason="hard_bounce")
    db.flush()
    with pytest.raises(marketing.MarketingError):
        marketing.subscribe(db, address, source="pytest")


def test_campaign_messages_are_marketing_category_and_carry_an_unsubscribe(db):
    address = f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com"
    marketing.subscribe(db, address, source="pytest")
    marketing.confirm(db, notify.unsubscribe_token(address))
    db.flush()
    campaign = models.Campaign(name="c", subject="s", body="b",
                               status=models.CampaignStatus.draft)
    db.add(campaign)
    db.flush()
    marketing.send_campaign(db, campaign)
    db.flush()

    row = db.query(models.Notification).filter(
        models.Notification.reference_id == campaign.id,
        models.Notification.recipient == address).one()
    assert row.category == models.NotificationCategory.marketing
    assert "unsubscribe" in row.body_text.lower()
    assert "Unsubscribe from offers" in row.body_html


# ---------------- HTTP ----------------

def _alive():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


live = pytest.mark.skipif(not _alive(), reason="backend not reachable")


@pytest.fixture(scope="module")
def auth():
    if not _alive():
        pytest.skip("backend not reachable")
    token = requests.post(f"{API}/auth/login", json=ADMIN, timeout=10).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _sellable(products, minimum=2):
    for product in products:
        for variant in product.get("variants") or []:
            if (variant.get("stock") or 0) > minimum and variant.get("is_available", True):
                return product, variant
    pytest.skip("no variant in the catalog has stock")


@live
def test_the_programme_is_public_but_balances_are_not(auth):
    """No customer accounts exist yet, so a public balance lookup would be an
    address-enumeration oracle that also leaks spending."""
    programme = requests.get(f"{API}/loyalty/programme", timeout=10)
    assert programme.status_code == 200
    assert programme.json()["max_redeem_pct"] > 0

    assert requests.get(f"{API}/loyalty/customers/anything", timeout=10).status_code == 401
    assert requests.get(f"{API}/loyalty/customers/anything/history",
                        timeout=10).status_code == 401


@live
def test_points_are_spendable_at_checkout_and_recorded(auth):
    product, variant = _sellable(requests.get(f"{API}/products", timeout=10).json())
    email = f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com"

    first = requests.post(f"{API}/orders", timeout=20, json={
        "customer_name": "Loyalty Test", "customer_email": email,
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    })
    assert first.status_code == 201, first.text
    order = first.json()

    customers = requests.get(f"{API}/customers", headers=auth, timeout=15).json()
    customer = next(c for c in customers if c["email"] == email)
    balance = requests.get(f"{API}/loyalty/customers/{customer['id']}",
                           headers=auth, timeout=10).json()

    if balance["balance"] == 0:
        pytest.skip("payments are enabled, so this order has not earned points yet")

    # Points earned on a paid order equal spend / earn_per, floored.
    assert balance["balance"] == int(Decimal(order["total"]) // Decimal(balance["earn_per"]))

    second = requests.post(f"{API}/orders", timeout=20, json={
        "customer_name": "Loyalty Test", "customer_email": email,
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
        "redeem_loyalty_points": balance["balance"],
    })
    assert second.status_code == 201, second.text
    paid_with_points = second.json()
    assert paid_with_points["loyalty_points_redeemed"] > 0
    assert Decimal(paid_with_points["loyalty_discount"]) > 0
    # The discount actually came off the total.
    assert Decimal(paid_with_points["total"]) < Decimal(order["total"])

    history = requests.get(f"{API}/loyalty/customers/{customer['id']}/history",
                           headers=auth, timeout=10).json()
    reasons = [h["reason"] for h in history]
    assert "redeemed" in reasons and "earned" in reasons
    assert history[0]["balance_after"] >= 0


@live
def test_a_client_cannot_state_what_its_points_are_worth(auth):
    """The request carries a number of points, never a discount."""
    product, variant = _sellable(requests.get(f"{API}/products", timeout=10).json())
    email = f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com"
    response = requests.post(f"{API}/orders", timeout=20, json={
        "customer_name": "Forger", "customer_email": email,
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
        "loyalty_discount": "9999.00", "loyalty_points_redeemed": 9999,
    })
    assert response.status_code == 201
    order = response.json()
    # Both injected fields were ignored: a new customer has no points.
    assert order["loyalty_points_redeemed"] == 0
    assert Decimal(order["loyalty_discount"] or 0) == 0


@live
def test_redeeming_points_you_do_not_have_is_refused(auth):
    product, variant = _sellable(requests.get(f"{API}/products", timeout=10).json())
    response = requests.post(f"{API}/orders", timeout=20, json={
        "customer_name": "No Points",
        "customer_email": f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
        "redeem_loyalty_points": 5000,
    })
    assert response.status_code == 400
    assert "points" in response.json()["detail"].lower()


@live
def test_the_moderation_queue_is_readable_and_admin_only(auth):
    """Regression: the admin list helper was shadowed by the dependency
    parameter of the same name, so this endpoint returned 500 for every call
    that had anything to return."""
    assert requests.get(f"{API}/reviews", timeout=10).status_code == 401

    for status in ("", "Pending", "Published", "Rejected"):
        params = {"limit": 5}
        if status:
            params["status"] = status
        response = requests.get(f"{API}/reviews", headers=auth, params=params, timeout=15)
        assert response.status_code == 200, f"{status}: {response.text}"
        for row in response.json():
            assert row["status"] in ("Pending", "Published", "Rejected")
            assert "author_email" in row          # the admin view, not the public one


@live
def test_a_review_needs_a_delivered_order(auth):
    product, _variant = _sellable(requests.get(f"{API}/products", timeout=10).json())
    response = requests.post(f"{API}/reviews", timeout=15, json={
        "product_id": product["id"], "order_number": "VR-0000",
        "email": "stranger@example.com", "rating": 5, "body": "Never bought this",
    })
    assert response.status_code == 400
    # The same message for every failure — otherwise this is an order oracle.
    assert "could not match" in response.json()["detail"].lower()


@live
def test_pending_reviews_are_invisible_and_do_not_move_the_rating(auth):
    products = requests.get(f"{API}/products", timeout=10).json()
    product = products[0]
    summary = requests.get(f"{API}/reviews/product/{product['id']}/summary",
                           timeout=10).json()
    published = requests.get(f"{API}/reviews/product/{product['id']}", timeout=10).json()
    assert summary["count"] == len(published)
    # The stored aggregate and the live count agree, always.
    assert abs(float(product["rating"] or 0) - summary["average"]) < 0.05
    assert (product["review_count"] or 0) == summary["count"]


@live
def test_subscribing_says_the_same_thing_whoever_asks(auth):
    """A different answer for a known address would report who is on the list."""
    fresh = f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com"
    first = requests.post(f"{API}/marketing/subscribe", timeout=15,
                          json={"email": fresh, "source": "pytest"})
    second = requests.post(f"{API}/marketing/subscribe", timeout=15,
                           json={"email": fresh, "source": "pytest"})
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()

    subscribers = requests.get(f"{API}/marketing/subscribers", headers=auth,
                               timeout=15).json()
    entry = next(s for s in subscribers if s["email"] == fresh)
    assert entry["status"] == "Pending"


@live
def test_the_audience_is_stated_as_a_breakdown_not_a_flattering_total(auth):
    audience = requests.get(f"{API}/marketing/audience", headers=auth, timeout=10).json()
    assert set(audience) >= {"mailable", "awaiting_confirmation", "unsubscribed"}
    assert isinstance(audience["mailable"], int)
    assert "consent" in audience["note"]


@live
def test_campaigns_are_admin_only():
    assert requests.get(f"{API}/marketing/campaigns", timeout=10).status_code == 401
    assert requests.post(f"{API}/marketing/campaigns", timeout=10,
                         json={"name": "x", "subject": "y", "body": "z"}).status_code == 401
    assert requests.get(f"{API}/marketing/subscribers", timeout=10).status_code == 401


@live
def test_a_sent_campaign_cannot_be_sent_twice(auth):
    campaign = requests.post(f"{API}/marketing/campaigns", headers=auth, timeout=15, json={
        "name": f"pytest {uuid.uuid4().hex[:6]}", "subject": "Test send",
        "body": "This is a test campaign body.",
    }).json()
    assert campaign["status"] == "Draft"          # creating one sends nothing

    sent = requests.post(f"{API}/marketing/campaigns/{campaign['id']}/send",
                         headers=auth, timeout=30).json()
    assert sent["campaign"]["status"] == "Sent"
    assert sent["queued"] + sent["skipped"] == sent["audience"]

    again = requests.post(f"{API}/marketing/campaigns/{campaign['id']}/send",
                          headers=auth, timeout=30)
    assert again.status_code == 400
