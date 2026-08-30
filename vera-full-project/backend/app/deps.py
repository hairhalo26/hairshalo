"""Authentication dependencies.

Two populations share one JWT scheme and must never be confused for each other:

* **Staff** (`users` table) run the dashboard. Their tokens have no `typ`, or
  `typ="admin"`.
* **Customers** (`customers` table) use the storefront. Their tokens carry
  `typ="customer"` and a `tv` (token version).

Each dependency below checks the token type explicitly rather than relying on
"the id will not be found in the other table". Id collision is unlikely, not
impossible, and "unlikely" is not an access-control decision.
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.security import decode_access_token
from app import models

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

CREDENTIALS_HEADER = {"WWW-Authenticate": "Bearer"}


def _unauthorized(detail: str = "Could not validate credentials") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail,
                         headers=CREDENTIALS_HEADER)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    """The signed-in staff member. Rejects customer tokens outright."""
    if not token:
        raise _unauthorized()
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise _unauthorized()
    # A customer token must never authenticate a staff endpoint, whatever id it
    # happens to carry.
    if payload.get("typ") == "customer":
        raise _unauthorized("This is a customer token, not a staff token.")
    user = db.query(models.User).filter(models.User.id == payload["sub"]).first()
    if not user:
        raise _unauthorized()
    return user


def get_current_admin(user: models.User = Depends(get_current_user)) -> models.User:
    if user.role not in ("admin", "staff"):
        raise HTTPException(status_code=403, detail="Not authorized")
    return user


def get_current_customer(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> models.Customer:
    """The signed-in customer.

    This is the ONLY way a customer endpoint learns who is calling. Nothing
    reads an email, a customer id or an order number out of the request to
    decide whose data to return.
    """
    if not token:
        raise _unauthorized()
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise _unauthorized()
    if payload.get("typ") != "customer":
        raise _unauthorized("This is not a customer token.")

    customer = db.query(models.Customer).filter(
        models.Customer.id == payload["sub"]).first()
    if not customer or not customer.hashed_password:
        raise _unauthorized()
    if not customer.is_active:
        raise HTTPException(status_code=403, detail="This account has been disabled.")
    # Token version: a password change or "log out everywhere" bumps the
    # customer's version, and every token minted before that stops working.
    if int(payload.get("tv", -1)) != int(customer.token_version or 0):
        raise _unauthorized("This session has expired. Please sign in again.")
    return customer


def get_verified_customer(
    customer: models.Customer = Depends(get_current_customer),
) -> models.Customer:
    """A customer who has proved they own the mailbox.

    Required for order history and loyalty. Most `customers` rows were created
    by checkout from an email somebody typed, so without this gate anyone could
    register with a stranger's address and read the orders already under it.
    """
    if not customer.email_verified:
        raise HTTPException(
            status_code=403,
            detail=("Please confirm your email address first — we sent you a link. "
                    "Your order history stays hidden until then."),
        )
    return customer


def get_optional_customer(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """The signed-in customer, or None.

    For endpoints that must keep working for guests (checkout, payment) while
    still enforcing ownership for anyone who IS identified. Returns None rather
    than raising for a missing or unusable token — including a staff token,
    which is simply "not a customer" here.
    """
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload or payload.get("typ") != "customer" or "sub" not in payload:
        return None
    customer = db.query(models.Customer).filter(
        models.Customer.id == payload["sub"]).first()
    if not customer or not customer.is_active:
        return None
    if int(payload.get("tv", -1)) != int(customer.token_version or 0):
        return None
    return customer
