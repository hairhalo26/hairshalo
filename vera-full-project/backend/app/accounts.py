"""Customer accounts: registration, login, verification, password reset.

Non-negotiable rule this module exists to enforce:

    Who you are comes from the token, never from the request body.

The corollaries are what most of this file is about:

* **Registering with an address does not grant its history.** Almost every row
  in `customers` was created by checkout, from an email typed into an order
  form; nobody proved they owned that mailbox. So order history and loyalty are
  gated on `email_verified`, which only a click in a mailbox can set. Without
  that gate, "register as someone@example.com" would read their orders.
* **Nothing here reveals whether an address is registered.** Login, password
  reset and registration all answer the same way for a known and an unknown
  address; a different answer turns any of them into an account oracle.
* **Customer tokens are not admin tokens.** They carry `typ="customer"` and are
  rejected by the admin dependency, and vice versa.
* **Changing a password invalidates existing tokens** through `token_version`,
  so a stolen JWT does not outlive the reset that was meant to stop it.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app import models, notifications as notify
from app.config import settings
from app.security import create_access_token, hash_password, verify_password

logger = logging.getLogger("vera.accounts")

MIN_PASSWORD_LENGTH = 10
# bcrypt hashes only the first 72 BYTES; anything beyond is silently ignored,
# which would make two different long passwords interchangeable. Refuse rather
# than truncate, so nobody believes a 200-character passphrase is being used.
MAX_PASSWORD_BYTES = 72

PASSWORD_RESET_TTL = timedelta(hours=1)
EMAIL_VERIFY_TTL = timedelta(days=7)

#: Rejected outright. Not a substitute for a breach-corpus check (which needs a
#: dataset this project does not ship), just the handful that would otherwise
#: turn up in a real customer list.
WEAK_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwertyuiop", "letmein123", "iloveyou1", "admin12345", "welcome123",
    "changeme123", "verahair123", "hairhalo123", "hairshalo123",
}


class AccountError(Exception):
    """Rejected account operation. The message is shown to the customer."""


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_password(password: str, email: str = "") -> None:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(
            f"Please use at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise AccountError(
            "That password is too long to be stored safely — please use "
            f"{MAX_PASSWORD_BYTES} bytes or fewer (about {MAX_PASSWORD_BYTES} "
            "characters)."
        )
    if password.lower() in WEAK_PASSWORDS:
        raise AccountError("That password is too common. Please choose another.")
    local_part = normalise_email(email).split("@")[0]
    if local_part and local_part in password.lower():
        raise AccountError("Please choose a password that is not part of your email.")


# ---------------------------------------------------------------- tokens


def _token_pair() -> Tuple[str, str]:
    """(raw token for the email, SHA-256 of it for the database).

    The database never holds anything that can be used to reset an account, so
    read access to `customers` is not enough to take one over.
    """
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def issue_customer_token(customer: models.Customer) -> str:
    """Mint a customer JWT. `typ` and `tv` are what make it a customer token
    and what makes it revocable."""
    return create_access_token({
        "sub": customer.id,
        "typ": "customer",
        "tv": customer.token_version or 0,
    })


def verification_token(customer: models.Customer) -> str:
    """Signed, stateless email-verification token.

    Stateless is fine here precisely because verifying twice is harmless — the
    second attempt finds the address already verified and does nothing.
    """
    return notify.unsubscribe_token(f"verify:{customer.id}:{customer.email}")


def verify_verification_token(db: Session, token: str) -> Optional[models.Customer]:
    payload = notify.verify_unsubscribe_token(token)
    if not payload or not payload.startswith("verify:"):
        return None
    try:
        _, customer_id, email = payload.split(":", 2)
    except ValueError:
        return None
    customer = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    # The email is inside the signed payload, so a token stops working if the
    # address it was issued for has since changed.
    if not customer or normalise_email(customer.email) != normalise_email(email):
        return None
    return customer


# ---------------------------------------------------------------- registration


def register(db: Session, *, email: str, password: str, name: str,
             phone: str = None) -> Tuple[models.Customer, bool]:
    """Create an account, or attach one to an existing checkout-created row.

    Returns (customer, created_new_row). Raises AccountError only for problems
    with the SUBMITTED data — never to say "that address already has an
    account", which would confirm the address is registered.
    """
    address = normalise_email(email)
    if not address or "@" not in address:
        raise AccountError("Please enter a valid email address.")
    validate_password(password, address)
    if not (name or "").strip():
        raise AccountError("Please tell us your name.")

    customer = db.query(models.Customer).filter(
        models.Customer.email == address).first()
    created = False

    if customer and customer.has_account:
        # Deliberately not an error. The caller sends the same "check your
        # inbox" response either way, and the mailbox owner receives either a
        # verification link or a "someone tried to register" note — so an
        # attacker learns nothing and the real owner learns something.
        logger.info("Registration attempted for an address that already has an account")
        return (customer, False)

    if not customer:
        customer = models.Customer(name=name.strip(), email=address, phone=phone)
        db.add(customer)
        db.flush()
        created = True
    else:
        # An existing checkout customer is claiming their account. Their orders
        # stay invisible until the mailbox is verified.
        customer.name = name.strip() or customer.name
        customer.phone = phone or customer.phone

    customer.hashed_password = hash_password(password)
    customer.email_verified = False
    customer.is_active = True
    customer.registered_at = datetime.utcnow()
    customer.password_changed_at = datetime.utcnow()
    return (customer, created)


def send_verification(db: Session, customer: models.Customer) -> None:
    token = verification_token(customer)
    url = f"{settings.STOREFRONT_URL.rstrip('/')}/verify-email?token={token}"
    notify.enqueue(
        db, "account.verify_email", customer.email,
        {"customer_name": customer.name, "verify_url": url,
         "expires_days": EMAIL_VERIFY_TTL.days},
        # Timestamped so a resend is a new message rather than a silent no-op.
        event_key=f"account.verify_email:{customer.id}:{datetime.utcnow().isoformat()}",
        recipient_name=customer.name,
        reference_type="customer", reference_id=customer.id,
    )


def verify_email(db: Session, token: str) -> models.Customer:
    customer = verify_verification_token(db, token)
    if not customer:
        raise AccountError("That verification link is not valid or has expired.")
    customer.email_verified = True
    return customer


# ---------------------------------------------------------------- login


def authenticate(db: Session, email: str, password: str) -> models.Customer:
    """Check credentials. One error message for every failure mode."""
    generic = AccountError("Incorrect email or password.")
    address = normalise_email(email)

    customer = db.query(models.Customer).filter(
        models.Customer.email == address).first()

    if not customer or not customer.hashed_password:
        # Spend the same time as a real check would, so response timing does not
        # separate "no such account" from "wrong password".
        hash_password(password or "x")
        raise generic
    if not verify_password(password or "", customer.hashed_password):
        raise generic
    if not customer.is_active:
        raise AccountError(
            "This account has been disabled. Please contact us if that is unexpected.")

    customer.last_login_at = datetime.utcnow()
    return customer


def change_password(db: Session, customer: models.Customer,
                    current_password: str, new_password: str) -> models.Customer:
    if not customer.hashed_password or not verify_password(
            current_password or "", customer.hashed_password):
        raise AccountError("Your current password is incorrect.")
    validate_password(new_password, customer.email)
    if verify_password(new_password, customer.hashed_password):
        raise AccountError("Please choose a password you have not used here before.")

    customer.hashed_password = hash_password(new_password)
    customer.password_changed_at = datetime.utcnow()
    # Everything minted before this moment stops working.
    customer.token_version = (customer.token_version or 0) + 1
    _notify_password_changed(db, customer)
    return customer


def logout_everywhere(db: Session, customer: models.Customer) -> models.Customer:
    customer.token_version = (customer.token_version or 0) + 1
    return customer


# ---------------------------------------------------------------- password reset


def request_password_reset(db: Session, email: str) -> Optional[models.Customer]:
    """Start a reset. Returns None for unknown addresses — the CALLER must
    respond identically either way."""
    address = normalise_email(email)
    customer = db.query(models.Customer).filter(
        models.Customer.email == address).first()
    if not customer or not customer.hashed_password:
        return None

    raw, hashed = _token_pair()
    customer.password_reset_hash = hashed
    customer.password_reset_expires_at = datetime.utcnow() + PASSWORD_RESET_TTL
    url = f"{settings.STOREFRONT_URL.rstrip('/')}/reset-password?token={raw}"
    notify.enqueue(
        db, "account.password_reset", customer.email,
        {"customer_name": customer.name, "reset_url": url,
         "expires_minutes": int(PASSWORD_RESET_TTL.total_seconds() // 60)},
        event_key=f"account.password_reset:{customer.id}:{datetime.utcnow().isoformat()}",
        recipient_name=customer.name,
        reference_type="customer", reference_id=customer.id,
    )
    return customer


def confirm_password_reset(db: Session, token: str, new_password: str) -> models.Customer:
    """Complete a reset. The token is single-use and time-limited."""
    if not token:
        raise AccountError("That reset link is not valid or has expired.")
    hashed = hashlib.sha256(token.encode()).hexdigest()
    customer = db.query(models.Customer).filter(
        models.Customer.password_reset_hash == hashed).first()
    if not customer or not customer.password_reset_expires_at \
            or customer.password_reset_expires_at < datetime.utcnow():
        raise AccountError("That reset link is not valid or has expired.")

    validate_password(new_password, customer.email)
    customer.hashed_password = hash_password(new_password)
    customer.password_changed_at = datetime.utcnow()
    customer.token_version = (customer.token_version or 0) + 1
    # Consumed: a reset link works exactly once.
    customer.password_reset_hash = None
    customer.password_reset_expires_at = None
    # Completing a reset proves control of the mailbox, which is the same thing
    # verification proves.
    customer.email_verified = True
    _notify_password_changed(db, customer)
    return customer


def _notify_password_changed(db: Session, customer: models.Customer) -> None:
    """Tell the mailbox owner. This is how a customer finds out about a takeover
    they did not perform, so it is not optional."""
    notify.enqueue(
        db, "account.password_changed", customer.email,
        {"customer_name": customer.name,
         "changed_at": datetime.utcnow()},
        event_key=f"account.password_changed:{customer.id}:{datetime.utcnow().isoformat()}",
        recipient_name=customer.name,
        reference_type="customer", reference_id=customer.id,
    )
