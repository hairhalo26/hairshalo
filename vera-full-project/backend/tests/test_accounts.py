"""Customer account tests.

The rule under test: **who you are comes from the token, never from the request
body.** Most of this file is therefore about what a caller CANNOT do — read
another customer's order, keep using a token after a password change, or learn
whether an address is registered.

HTTP tests need a backend on port 8010 (see the other test modules).
"""
import os
import uuid
from datetime import datetime, timedelta

import pytest
import requests
from sqlalchemy import text

from app import accounts, models
from app.security import decode_access_token

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@hairshalo.com", "password": "ChangeMe123!"}
MARKER = "pytest-acct"
GOOD_PASSWORD = "correct-horse-battery-91"


# ---------------- unit: password policy ----------------

def test_short_and_common_passwords_are_refused():
    for bad in ("short", "password123", "12345678", "changeme123"):
        with pytest.raises(accounts.AccountError):
            accounts.validate_password(bad, "someone@example.com")


def test_a_password_containing_the_email_is_refused():
    with pytest.raises(accounts.AccountError):
        accounts.validate_password("bhargavi-secret-1", "bhargavi@example.com")


def test_an_over_long_password_is_refused_rather_than_truncated():
    """bcrypt ignores everything past 72 bytes. Truncating silently would make
    two different passphrases interchangeable, so this refuses instead."""
    with pytest.raises(accounts.AccountError) as exc:
        accounts.validate_password("a" * 100, "someone@example.com")
    assert "too long" in str(exc.value)
    accounts.validate_password("a" * 72, "someone@example.com")     # exactly at the limit


def test_email_normalisation():
    assert accounts.normalise_email("  Person@Example.COM ") == "person@example.com"


def test_a_customer_token_says_it_is_a_customer_token():
    customer = models.Customer(id="c1", name="A", email="a@b.c", token_version=3)
    payload = decode_access_token(accounts.issue_customer_token(customer))
    assert payload["typ"] == "customer"
    assert payload["sub"] == "c1"
    assert payload["tv"] == 3


# ---------------- database ----------------

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
        orphans = [c.id for c in session.query(models.Customer).filter(
            models.Customer.email.like(f"%{MARKER}%")).all() if not c.orders]
        if orphans:
            for model in (models.WishlistItem, models.CustomerAddress,
                          models.LoyaltyTransaction):
                session.query(model).filter(
                    model.customer_id.in_(orphans)).delete(synchronize_session=False)
            session.query(models.Customer).filter(
                models.Customer.id.in_(orphans)).delete(synchronize_session=False)
        session.query(models.Notification).filter(
            models.Notification.recipient.like(f"%{MARKER}%")).delete(
                synchronize_session=False)
        session.commit()
        session.close()


def _register(db, email=None, password=GOOD_PASSWORD):
    email = email or f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com"
    customer, created = accounts.register(db, email=email, password=password,
                                          name="Test Shopper")
    db.flush()
    return customer


def test_registering_never_reveals_that_an_address_is_taken(db):
    """A second registration on the same address must not raise — the endpoint
    answers identically either way, so it cannot be used to test addresses."""
    customer = _register(db)
    again, created = accounts.register(db, email=customer.email,
                                       password="another-good-password-1",
                                       name="Someone Else")
    assert created is False
    assert again.id == customer.id
    # Crucially, the existing password is NOT replaced by the second attempt.
    assert accounts.authenticate(db, customer.email, GOOD_PASSWORD).id == customer.id


def test_a_new_account_starts_unverified(db):
    customer = _register(db)
    assert customer.email_verified is False
    assert customer.has_account is True
    assert customer.can_see_order_history is False


def test_checkout_created_customers_have_no_account_until_they_register(db):
    """Most rows in `customers` come from checkout. They must not be logging in."""
    walk_in = models.Customer(name="Walk In", email=f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com")
    db.add(walk_in)
    db.flush()
    assert walk_in.has_account is False
    with pytest.raises(accounts.AccountError):
        accounts.authenticate(db, walk_in.email, GOOD_PASSWORD)


def test_authentication_is_uniform_for_wrong_password_and_unknown_address(db):
    customer = _register(db)
    messages = set()
    for email, password in ((customer.email, "wrong-password-here"),
                            (f"{MARKER}-nobody@example.com", GOOD_PASSWORD)):
        with pytest.raises(accounts.AccountError) as exc:
            accounts.authenticate(db, email, password)
        messages.add(str(exc.value))
    assert messages == {"Incorrect email or password."}


def test_verification_token_stops_working_if_the_address_changes(db):
    customer = _register(db)
    token = accounts.verification_token(customer)
    assert accounts.verify_verification_token(db, token).id == customer.id
    customer.email = f"{MARKER}-changed-{uuid.uuid4().hex[:6]}@example.com"
    db.flush()
    assert accounts.verify_verification_token(db, token) is None


def test_a_password_change_invalidates_existing_tokens(db):
    customer = _register(db)
    before = customer.token_version or 0
    accounts.change_password(db, customer, GOOD_PASSWORD, "a-different-password-77")
    assert customer.token_version == before + 1


def test_a_reset_token_is_stored_hashed_and_works_once(db):
    customer = _register(db)
    accounts.request_password_reset(db, customer.email)
    db.flush()
    stored = customer.password_reset_hash
    assert stored and len(stored) == 64          # sha256 hex, not the raw token

    # Recover the raw token the way the email would carry it.
    row = db.query(models.Notification).filter(
        models.Notification.recipient == customer.email,
        models.Notification.event_type == "account.password_reset",
    ).order_by(models.Notification.created_at.desc()).first()
    raw = row.body_text.split("reset-password?token=")[1].split()[0]
    assert raw not in (stored, "")

    accounts.confirm_password_reset(db, raw, "brand-new-password-42")
    db.flush()
    assert customer.password_reset_hash is None
    assert customer.email_verified is True       # completing a reset proves the mailbox
    with pytest.raises(accounts.AccountError):
        accounts.confirm_password_reset(db, raw, "yet-another-password-13")


def test_an_expired_reset_token_is_refused(db):
    customer = _register(db)
    accounts.request_password_reset(db, customer.email)
    db.flush()
    row = db.query(models.Notification).filter(
        models.Notification.recipient == customer.email,
        models.Notification.event_type == "account.password_reset",
    ).order_by(models.Notification.created_at.desc()).first()
    raw = row.body_text.split("reset-password?token=")[1].split()[0]

    customer.password_reset_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.flush()
    with pytest.raises(accounts.AccountError):
        accounts.confirm_password_reset(db, raw, "brand-new-password-42")


def test_a_reset_request_for_an_unknown_address_is_a_no_op(db):
    assert accounts.request_password_reset(db, f"{MARKER}-nobody@example.com") is None


# ---------------- HTTP ----------------

def _alive():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


live = pytest.mark.skipif(not _alive(), reason="backend not reachable")


def _new_account(verify=True):
    """Register through the API and return (email, token, customer_id)."""
    email = f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com"
    r = requests.post(f"{API}/account/register", timeout=15, json={
        "email": email, "password": GOOD_PASSWORD, "name": "API Shopper"})
    assert r.status_code == 202, r.text
    if verify:
        _verify(email)
    login = requests.post(f"{API}/account/login", timeout=15,
                          json={"email": email, "password": GOOD_PASSWORD})
    assert login.status_code == 200, login.text
    body = login.json()
    return email, body["access_token"], body["customer"]["id"]


def _verify(email):
    """Confirm the mailbox using the token from the notification outbox — the
    same path a real customer takes, without a real mailbox."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        row = db.query(models.Notification).filter(
            models.Notification.recipient == email,
            models.Notification.event_type == "account.verify_email",
        ).order_by(models.Notification.created_at.desc()).first()
        assert row, "no verification email was queued"
        token = row.body_text.split("verify-email?token=")[1].split()[0]
    finally:
        db.close()
    r = requests.post(f"{API}/account/verify-email", timeout=15, json={"token": token})
    assert r.status_code == 200, r.text


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _place_order(email, token=None):
    products = requests.get(f"{API}/products", timeout=10).json()
    for product in products:
        for variant in product.get("variants") or []:
            if (variant.get("stock") or 0) > 2:
                r = requests.post(f"{API}/orders", timeout=15, json={
                    "customer_name": "API Shopper", "customer_email": email,
                    "shipping_address": "12 MG Road",
                    "items": [{"product_id": product["id"],
                               "variant_id": variant["id"], "quantity": 1}],
                })
                assert r.status_code == 201, r.text
                return r.json()
    pytest.skip("no variant in the catalog has stock")


@live
def test_registration_answers_identically_for_a_taken_address():
    email = f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com"
    first = requests.post(f"{API}/account/register", timeout=15, json={
        "email": email, "password": GOOD_PASSWORD, "name": "First"})
    second = requests.post(f"{API}/account/register", timeout=15, json={
        "email": email, "password": "different-password-99", "name": "Second"})
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()          # byte-identical: no oracle

    # And the second attempt did NOT take over the account.
    login = requests.post(f"{API}/account/login", timeout=15,
                          json={"email": email, "password": "different-password-99"})
    assert login.status_code == 401


@live
def test_a_weak_password_is_refused_with_a_useful_message():
    r = requests.post(f"{API}/account/register", timeout=15, json={
        "email": f"{MARKER}-{uuid.uuid4().hex[:6]}@example.com",
        "password": "password123", "name": "Weak"})
    assert r.status_code == 400
    assert "common" in r.json()["detail"].lower()


@live
def test_login_returns_a_token_and_never_a_password_hash():
    email, token, _ = _new_account()
    body = requests.post(f"{API}/account/login", timeout=15,
                         json={"email": email, "password": GOOD_PASSWORD}).json()
    serialised = str(body)
    assert "hashed_password" not in serialised
    assert "$2b$" not in serialised               # no bcrypt hash anywhere
    assert body["customer"]["email"] == email

    profile = requests.get(f"{API}/account/me", headers=_auth(token), timeout=10).json()
    assert "hashed_password" not in str(profile)
    assert "token_version" not in str(profile)
    assert "password_reset_hash" not in str(profile)


@live
def test_account_endpoints_reject_an_admin_token():
    """A staff token must not act as a customer, whatever it is used on."""
    admin_token = requests.post(f"{API}/auth/login", json=ADMIN, timeout=10).json()["access_token"]
    for path in ("/account/me", "/account/orders", "/account/loyalty", "/account/wishlist"):
        r = requests.get(f"{API}{path}", headers=_auth(admin_token), timeout=10)
        assert r.status_code == 401, f"{path} accepted an admin token"


@live
def test_admin_endpoints_reject_a_customer_token():
    _email, token, _ = _new_account()
    for path in ("/orders", "/customers", "/notifications", "/reviews"):
        r = requests.get(f"{API}{path}", headers=_auth(token), timeout=10)
        assert r.status_code in (401, 403), f"{path} accepted a customer token"


@live
def test_order_history_is_hidden_until_the_mailbox_is_confirmed():
    """Rows in `customers` come from checkout, so registering with someone
    else's address must not hand over their orders."""
    email = f"{MARKER}-{uuid.uuid4().hex[:8]}@example.com"
    requests.post(f"{API}/account/register", timeout=15, json={
        "email": email, "password": GOOD_PASSWORD, "name": "Unverified"})
    token = requests.post(f"{API}/account/login", timeout=15, json={
        "email": email, "password": GOOD_PASSWORD}).json()["access_token"]

    for path in ("/account/orders", "/account/loyalty"):
        r = requests.get(f"{API}{path}", headers=_auth(token), timeout=10)
        assert r.status_code == 403, path
        assert "confirm your email" in r.json()["detail"].lower()

    # Profile and wishlist still work: they expose nothing that predates the account.
    assert requests.get(f"{API}/account/me", headers=_auth(token), timeout=10).status_code == 200

    _verify(email)
    token = requests.post(f"{API}/account/login", timeout=15, json={
        "email": email, "password": GOOD_PASSWORD}).json()["access_token"]
    assert requests.get(f"{API}/account/orders", headers=_auth(token),
                        timeout=10).status_code == 200


@live
def test_a_customer_sees_only_their_own_orders():
    email_a, token_a, _ = _new_account()
    email_b, token_b, _ = _new_account()
    order_a = _place_order(email_a)
    order_b = _place_order(email_b)

    mine = requests.get(f"{API}/account/orders", headers=_auth(token_a), timeout=15).json()
    ids = {o["id"] for o in mine}
    assert order_a["id"] in ids
    assert order_b["id"] not in ids


@live
def test_idor_changing_the_order_id_does_not_expose_another_customer():
    """The headline requirement: customer A must never reach customer B's order."""
    email_a, token_a, _ = _new_account()
    email_b, token_b, _ = _new_account()
    order_b = _place_order(email_b)

    # B can see it.
    assert requests.get(f"{API}/account/orders/{order_b['id']}",
                        headers=_auth(token_b), timeout=15).status_code == 200

    # A cannot — and gets 404, not 403, so the response does not confirm it exists.
    r = requests.get(f"{API}/account/orders/{order_b['id']}",
                     headers=_auth(token_a), timeout=15)
    assert r.status_code == 404
    assert order_b["order_number"] not in r.text

    # Nor without a token at all.
    assert requests.get(f"{API}/account/orders/{order_b['id']}",
                        timeout=15).status_code == 401


@live
def test_addresses_and_wishlist_are_scoped_to_the_owner():
    _email_a, token_a, _ = _new_account()
    _email_b, token_b, _ = _new_account()

    created = requests.post(f"{API}/account/addresses", headers=_auth(token_a), timeout=15,
                            json={"full_name": "A Shopper", "line1": "12 MG Road",
                                  "city": "Bengaluru", "country": "India"})
    assert created.status_code == 201
    address_id = created.json()["id"]

    assert requests.get(f"{API}/account/addresses", headers=_auth(token_b),
                        timeout=10).json() == []
    for method in (requests.put, requests.delete):
        kwargs = {"json": {"full_name": "B", "line1": "x", "city": "y"}} \
            if method is requests.put else {}
        r = method(f"{API}/account/addresses/{address_id}", headers=_auth(token_b),
                   timeout=10, **kwargs)
        assert r.status_code == 404

    # A's address is untouched.
    assert requests.get(f"{API}/account/addresses", headers=_auth(token_a),
                        timeout=10).json()[0]["full_name"] == "A Shopper"


@live
def test_changing_the_password_ends_other_sessions():
    email, token, _ = _new_account()
    assert requests.get(f"{API}/account/me", headers=_auth(token), timeout=10).status_code == 200

    changed = requests.post(f"{API}/account/password", headers=_auth(token), timeout=15,
                            json={"current_password": GOOD_PASSWORD,
                                  "new_password": "a-completely-new-one-55"})
    assert changed.status_code == 200

    # The token that made the change is now invalid too — that is the point.
    stale = requests.get(f"{API}/account/me", headers=_auth(token), timeout=10)
    assert stale.status_code == 401

    fresh = requests.post(f"{API}/account/login", timeout=15, json={
        "email": email, "password": "a-completely-new-one-55"})
    assert fresh.status_code == 200


@live
def test_logout_everywhere_invalidates_the_current_token():
    _email, token, _ = _new_account()
    assert requests.post(f"{API}/account/logout-all", headers=_auth(token),
                         timeout=10).status_code == 200
    assert requests.get(f"{API}/account/me", headers=_auth(token),
                        timeout=10).status_code == 401


@live
def test_password_reset_request_is_uniform_for_unknown_addresses():
    known, _token, _ = _new_account()
    unknown = f"{MARKER}-nobody-{uuid.uuid4().hex[:6]}@example.com"
    a = requests.post(f"{API}/account/password-reset/request", timeout=15,
                      json={"email": known})
    b = requests.post(f"{API}/account/password-reset/request", timeout=15,
                      json={"email": unknown})
    assert a.status_code == b.status_code == 202
    assert a.json() == b.json()


@live
def test_loyalty_history_is_the_customers_own_ledger():
    email, token, _ = _new_account()
    body = requests.get(f"{API}/account/loyalty", headers=_auth(token), timeout=15).json()
    assert body["balance"] == 0
    assert body["history"] == []
    assert body["max_redeem_pct"] >= 0
    # The programme terms are public; the balance is not.
    assert requests.get(f"{API}/loyalty/programme", timeout=10).status_code == 200
    assert requests.get(f"{API}/account/loyalty", timeout=10).status_code == 401
