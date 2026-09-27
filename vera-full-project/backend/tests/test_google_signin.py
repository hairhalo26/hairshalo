"""Sign in with Google.

Verification is tested against a locally generated RSA key standing in for
Google's published keys, so these run offline. The account rules are the
ones that matter for security: a Google sign-in may prove a mailbox, but it
must never let someone share an account they pre-registered with that address.
"""
import time
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from app import accounts, google_auth, models
from test_accounts import GOOD_PASSWORD, MARKER, _register, db  # noqa: F401 (fixture)

CLIENT_ID = "test-client.apps.googleusercontent.com"


@pytest.fixture(scope="module")
def signer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    public = jwk.construct(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo), "RS256").to_dict()
    public.update(kid="test-kid", use="sig", alg="RS256")
    return pem, public


@pytest.fixture(autouse=True)
def google_keys(monkeypatch, signer):
    monkeypatch.setattr(google_auth, "_keys", lambda force=False: [signer[1]])


def token(signer, **overrides):
    claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "1234567890",
              "email": f"{MARKER}-g{uuid.uuid4().hex[:8]}@example.com", "email_verified": True,
              "name": "Google Shopper", "iat": int(time.time()), "exp": int(time.time()) + 600}
    claims.update(overrides)
    return jwt.encode(claims, signer[0], algorithm="RS256", headers={"kid": "test-kid"})


# ---------------- verification ----------------

def test_a_genuine_credential_is_accepted(signer):
    claims = google_auth.verify_credential(token(signer), CLIENT_ID)
    assert claims["email_verified"] is True


@pytest.mark.parametrize("overrides", [
    {"aud": "someone-elses-client"},                      # issued for another site
    {"iss": "https://evil.example.com"},                  # not Google
    {"exp": int(time.time()) - 10},                       # expired
])
def test_a_credential_for_another_site_or_issuer_or_time_is_refused(signer, overrides):
    with pytest.raises(google_auth.GoogleAuthError):
        google_auth.verify_credential(token(signer, **overrides), CLIENT_ID)


def test_a_credential_signed_by_another_key_is_refused(signer):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    forged = jwt.encode({"iss": "accounts.google.com", "aud": CLIENT_ID, "email": "x@example.com",
                         "exp": int(time.time()) + 600}, other, algorithm="RS256", headers={"kid": "test-kid"})
    with pytest.raises(google_auth.GoogleAuthError):
        google_auth.verify_credential(forged, CLIENT_ID)


def test_nothing_is_accepted_when_google_sign_in_is_not_configured(signer):
    with pytest.raises(google_auth.GoogleAuthError):
        google_auth.verify_credential(token(signer), "")


# ---------------- account rules ----------------

def claims_for(email, verified=True):
    return {"email": email, "email_verified": verified, "name": "Google Shopper"}


def test_a_new_google_customer_gets_a_confirmed_account(db):
    email = f"{MARKER}-g{uuid.uuid4().hex[:8]}@example.com"
    customer = accounts.google_sign_in(db, claims_for(email))
    db.flush()
    assert customer.email == email
    assert customer.has_account and customer.email_verified
    assert customer.can_see_order_history
    # No password was chosen, so none can be guessed: password login fails.
    with pytest.raises(accounts.AccountError):
        accounts.authenticate(db, email, GOOD_PASSWORD)


def test_an_unverified_google_email_is_refused(db):
    with pytest.raises(accounts.AccountError):
        accounts.google_sign_in(db, claims_for(f"{MARKER}-g{uuid.uuid4().hex[:8]}@example.com", verified=False))


def test_a_confirmed_account_keeps_its_password(db):
    customer = _register(db)
    customer.email_verified = True
    db.flush()
    version = customer.token_version or 0
    same = accounts.google_sign_in(db, claims_for(customer.email))
    assert same.id == customer.id
    assert accounts.authenticate(db, customer.email, GOOD_PASSWORD).id == customer.id
    assert (same.token_version or 0) == version          # nobody was signed out


def test_an_unconfirmed_pre_registration_cannot_be_shared(db):
    """Someone registers the victim's address with their own password but never
    confirms it. When the real owner signs in with Google, that password must
    stop working and any session it opened must end."""
    squatter = _register(db)
    assert squatter.email_verified is False
    version = squatter.token_version or 0
    owner = accounts.google_sign_in(db, claims_for(squatter.email))
    assert owner.id == squatter.id and owner.email_verified
    with pytest.raises(accounts.AccountError):
        accounts.authenticate(db, squatter.email, GOOD_PASSWORD)
    assert owner.token_version == version + 1


def test_a_checkout_customer_claims_their_orders_with_google(db):
    walk_in = models.Customer(name="Walk In", email=f"{MARKER}-g{uuid.uuid4().hex[:6]}@example.com")
    db.add(walk_in)
    db.flush()
    assert not walk_in.has_account
    customer = accounts.google_sign_in(db, claims_for(walk_in.email))
    assert customer.id == walk_in.id and customer.can_see_order_history


def test_a_disabled_account_stays_disabled(db):
    customer = _register(db)
    customer.is_active = False
    db.flush()
    with pytest.raises(accounts.AccountError):
        accounts.google_sign_in(db, claims_for(customer.email))
