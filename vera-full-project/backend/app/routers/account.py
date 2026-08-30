"""Customer account endpoints.

Every read below is scoped by `current.id`, taken from the JWT. No endpoint
here accepts an email, a customer id or an order number as a way of choosing
whose data to return — that is what makes a changed id in the URL a 404 rather
than someone else's order.

Responses are deliberately uniform where they would otherwise leak account
existence: register, login and password-reset all answer the same way for a
known and an unknown address.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app import accounts, loyalty as loyalty_service, models, notifications as notify, schemas
from app.database import get_db
from app.deps import get_current_customer, get_verified_customer

router = APIRouter(prefix="/api/account", tags=["account"])

#: Sent for both a successful registration and an attempt on an address that
#: already has an account, so the response cannot be used to test addresses.
REGISTER_MESSAGE = (
    "Check your inbox — if that address can be registered, we have sent a "
    "confirmation link. Your order history stays hidden until you confirm."
)
RESET_MESSAGE = (
    "If that address has an account, we have sent a password reset link. "
    "It expires in an hour."
)


def _profile(customer: models.Customer) -> schemas.CustomerProfileOut:
    return schemas.CustomerProfileOut(
        id=customer.id, name=customer.name, email=customer.email,
        phone=customer.phone, email_verified=customer.email_verified,
        loyalty_points=customer.loyalty_points or 0,
        preferred_currency=customer.preferred_currency,
        created_at=customer.created_at, last_login_at=customer.last_login_at,
    )


def _address_out(a: models.CustomerAddress) -> schemas.AddressOut:
    return schemas.AddressOut(
        id=a.id, label=a.label, full_name=a.full_name, phone=a.phone,
        line1=a.line1, line2=a.line2, city=a.city, state=a.state,
        postal_code=a.postal_code, country=a.country, is_default=a.is_default,
    )


# ---------------------------------------------------------------- registration


@router.post("/register", response_model=schemas.AccountMessage, status_code=202)
def register(payload: schemas.RegisterRequest, background: BackgroundTasks,
             db: Session = Depends(get_db)):
    """Create an account and send a verification link.

    Answers identically whether or not the address already has an account.
    """
    try:
        customer, _created = accounts.register(
            db, email=str(payload.email), password=payload.password,
            name=payload.name, phone=payload.phone,
        )
    except accounts.AccountError as exc:
        # Only problems with the SUBMITTED data reach here (weak password,
        # missing name) — never "that address is taken".
        raise HTTPException(status_code=400, detail=str(exc))

    accounts.send_verification(db, customer)
    db.commit()
    notify.schedule_dispatch(background)
    return schemas.AccountMessage(message=REGISTER_MESSAGE)


@router.post("/verify-email", response_model=schemas.AccountMessage)
def verify_email(payload: schemas.TokenRequest, db: Session = Depends(get_db)):
    try:
        customer = accounts.verify_email(db, payload.token)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return schemas.AccountMessage(
        message=f"Thanks {customer.name} — your email is confirmed.")


@router.post("/resend-verification", response_model=schemas.AccountMessage, status_code=202)
def resend_verification(background: BackgroundTasks,
                        current: models.Customer = Depends(get_current_customer),
                        db: Session = Depends(get_db)):
    if current.email_verified:
        return schemas.AccountMessage(message="That address is already confirmed.")
    accounts.send_verification(db, current)
    db.commit()
    notify.schedule_dispatch(background)
    return schemas.AccountMessage(message="Sent — check your inbox.")


# ---------------------------------------------------------------- session


@router.post("/login", response_model=schemas.CustomerToken)
def login(payload: schemas.CustomerLoginRequest, db: Session = Depends(get_db)):
    try:
        customer = accounts.authenticate(db, str(payload.email), payload.password)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    token = accounts.issue_customer_token(customer)
    db.commit()
    return schemas.CustomerToken(access_token=token, customer=_profile(customer))


@router.post("/logout", response_model=schemas.AccountMessage)
def logout(current: models.Customer = Depends(get_current_customer)):
    """Ends the session on this device.

    Honest about what it does: the token is stateless, so this endpoint asks the
    client to discard it. To invalidate a token that has already been stolen,
    use `/logout-all`, which bumps the token version server-side.
    """
    return schemas.AccountMessage(
        message="Signed out. Discard the token on this device.")


@router.post("/logout-all", response_model=schemas.AccountMessage)
def logout_all(current: models.Customer = Depends(get_current_customer),
               db: Session = Depends(get_db)):
    """Invalidate every token issued so far, on every device."""
    accounts.logout_everywhere(db, current)
    db.commit()
    return schemas.AccountMessage(
        message="Signed out everywhere. Existing sessions will stop working.")


@router.post("/password-reset/request", response_model=schemas.AccountMessage,
             status_code=202)
def request_password_reset(payload: schemas.PasswordResetRequest,
                           background: BackgroundTasks, db: Session = Depends(get_db)):
    """Always answers the same way — a different answer confirms the address."""
    accounts.request_password_reset(db, str(payload.email))
    db.commit()
    notify.schedule_dispatch(background)
    return schemas.AccountMessage(message=RESET_MESSAGE)


@router.post("/password-reset/confirm", response_model=schemas.AccountMessage)
def confirm_password_reset(payload: schemas.PasswordResetConfirm,
                           background: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        accounts.confirm_password_reset(db, payload.token, payload.new_password)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    notify.schedule_dispatch(background)
    return schemas.AccountMessage(
        message="Your password has been changed. Please sign in again.")


@router.post("/password", response_model=schemas.AccountMessage)
def change_password(payload: schemas.PasswordChangeRequest, background: BackgroundTasks,
                    current: models.Customer = Depends(get_current_customer),
                    db: Session = Depends(get_db)):
    try:
        accounts.change_password(db, current, payload.current_password,
                                 payload.new_password)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    notify.schedule_dispatch(background)
    return schemas.AccountMessage(
        message="Password changed. Other sessions have been signed out.")


# ---------------------------------------------------------------- profile


@router.get("/me", response_model=schemas.CustomerProfileOut)
def me(current: models.Customer = Depends(get_current_customer)):
    return _profile(current)


@router.put("/me", response_model=schemas.CustomerProfileOut)
def update_me(payload: schemas.ProfileUpdate,
              current: models.Customer = Depends(get_current_customer),
              db: Session = Depends(get_db)):
    """Name, phone and display-currency preference.

    The email address is deliberately not editable here: changing it would move
    an account onto a mailbox nobody has verified, which is the same hole
    `email_verified` exists to close.
    """
    if payload.name is not None:
        if not payload.name.strip():
            raise HTTPException(status_code=400, detail="Name cannot be empty.")
        current.name = payload.name.strip()
    if payload.phone is not None:
        current.phone = payload.phone.strip() or None
    if payload.preferred_currency is not None:
        from app import currency as currency_service
        code = currency_service.resolve_currency(payload.preferred_currency)
        current.preferred_currency = code
    db.commit()
    db.refresh(current)
    return _profile(current)


# ---------------------------------------------------------------- orders


@router.get("/orders", response_model=List[schemas.OrderOut])
def my_orders(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
              current: models.Customer = Depends(get_verified_customer),
              db: Session = Depends(get_db)):
    """This customer's orders. Scoped by the token, not by anything sent."""
    return (
        db.query(models.Order)
        .options(joinedload(models.Order.items))
        .filter(models.Order.customer_id == current.id)
        .order_by(models.Order.created_at.desc())
        .offset(offset).limit(limit).all()
    )


@router.get("/orders/{order_id}", response_model=schemas.OrderOut)
def my_order(order_id: str,
             current: models.Customer = Depends(get_verified_customer),
             db: Session = Depends(get_db)):
    """One order — 404 when it is not this customer's.

    404, not 403: "you may not see this" confirms the order exists. The filter
    is part of the query, so someone else's id simply matches nothing.
    """
    order = (
        db.query(models.Order)
        .options(joinedload(models.Order.items))
        .filter(models.Order.id == order_id,
                models.Order.customer_id == current.id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


# ---------------------------------------------------------------- loyalty


@router.get("/loyalty", response_model=schemas.MyLoyaltyOut)
def my_loyalty(limit: int = Query(50, ge=1, le=200),
               current: models.Customer = Depends(get_verified_customer),
               db: Session = Depends(get_db)):
    """Balance and the ledger behind it — earned, redeemed, reversed, returned."""
    report = loyalty_service.balance_report(db, current)
    rows = (
        db.query(models.LoyaltyTransaction)
        .filter(models.LoyaltyTransaction.customer_id == current.id)
        .order_by(models.LoyaltyTransaction.created_at.desc())
        .limit(limit).all()
    )
    earned = sum(r.delta for r in rows if r.delta > 0)
    spent = sum(-r.delta for r in rows if r.delta < 0)
    return schemas.MyLoyaltyOut(
        balance=report["balance"], value=report["value"],
        point_value=report["point_value"], earn_per=report["earn_per"],
        max_redeem_pct=report["max_redeem_pct"],
        earned_total=earned, redeemed_total=spent,
        history=[
            schemas.LoyaltyEntryOut(
                delta=r.delta, balance_after=r.balance_after,
                reason=r.reason.value, note=r.note,
                reference_type=r.reference_type, reference_id=r.reference_id,
                created_at=r.created_at)
            for r in rows
        ],
    )


# ---------------------------------------------------------------- addresses


@router.get("/addresses", response_model=List[schemas.AddressOut])
def list_addresses(current: models.Customer = Depends(get_current_customer),
                   db: Session = Depends(get_db)):
    return [_address_out(a) for a in
            db.query(models.CustomerAddress).filter(
                models.CustomerAddress.customer_id == current.id).all()]


@router.post("/addresses", response_model=schemas.AddressOut, status_code=201)
def add_address(payload: schemas.AddressIn,
                current: models.Customer = Depends(get_current_customer),
                db: Session = Depends(get_db)):
    address = models.CustomerAddress(
        customer_id=current.id, **payload.model_dump(exclude={"is_default"}))
    if payload.is_default:
        _clear_default(db, current.id)
        address.is_default = True
    db.add(address)
    db.commit()
    db.refresh(address)
    return _address_out(address)


@router.put("/addresses/{address_id}", response_model=schemas.AddressOut)
def update_address(address_id: str, payload: schemas.AddressIn,
                   current: models.Customer = Depends(get_current_customer),
                   db: Session = Depends(get_db)):
    address = _owned_address(db, address_id, current.id)
    for field, value in payload.model_dump(exclude={"is_default"}).items():
        setattr(address, field, value)
    if payload.is_default:
        _clear_default(db, current.id)
        address.is_default = True
    db.commit()
    db.refresh(address)
    return _address_out(address)


@router.delete("/addresses/{address_id}", status_code=204)
def delete_address(address_id: str,
                   current: models.Customer = Depends(get_current_customer),
                   db: Session = Depends(get_db)):
    db.delete(_owned_address(db, address_id, current.id))
    db.commit()


def _owned_address(db: Session, address_id: str, customer_id: str) -> models.CustomerAddress:
    address = db.query(models.CustomerAddress).filter(
        models.CustomerAddress.id == address_id,
        models.CustomerAddress.customer_id == customer_id,
    ).first()
    if not address:
        raise HTTPException(status_code=404, detail="Address not found")
    return address


def _clear_default(db: Session, customer_id: str) -> None:
    db.query(models.CustomerAddress).filter(
        models.CustomerAddress.customer_id == customer_id,
        models.CustomerAddress.is_default.is_(True),
    ).update({"is_default": False}, synchronize_session=False)


# ---------------------------------------------------------------- wishlist


@router.get("/wishlist", response_model=List[schemas.WishlistItemOut])
def list_wishlist(current: models.Customer = Depends(get_current_customer),
                  db: Session = Depends(get_db)):
    rows = (
        db.query(models.WishlistItem)
        .options(joinedload(models.WishlistItem.product),
                 joinedload(models.WishlistItem.variant))
        .filter(models.WishlistItem.customer_id == current.id)
        .all()
    )
    out = []
    for row in rows:
        product = row.product
        out.append(schemas.WishlistItemOut(
            id=row.id, product_id=row.product_id, variant_id=row.variant_id,
            product_name=product.name if product else "(removed)",
            variant_label=row.variant.label if row.variant else None,
            # Live values, not a snapshot: a wishlist is a pointer at a product,
            # not a promise about its price.
            price=(row.variant.effective_price(product) if row.variant and product
                   else (product.price if product else None)),
            image_url=product.primary_image_url if product else None,
            in_stock=bool(row.variant.stock) if row.variant
                     else bool(product and product.total_stock),
            added_at=row.created_at,
        ))
    return out


@router.post("/wishlist", response_model=schemas.WishlistItemOut, status_code=201)
def add_to_wishlist(payload: schemas.WishlistAdd,
                    current: models.Customer = Depends(get_current_customer),
                    db: Session = Depends(get_db)):
    product = db.query(models.Product).filter(
        models.Product.id == payload.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if payload.variant_id:
        variant = db.query(models.ProductVariant).filter(
            models.ProductVariant.id == payload.variant_id,
            models.ProductVariant.product_id == product.id,
        ).first()
        if not variant:
            raise HTTPException(status_code=400,
                                detail="That variant does not belong to this product.")

    existing = db.query(models.WishlistItem).filter(
        models.WishlistItem.customer_id == current.id,
        models.WishlistItem.product_id == payload.product_id,
        models.WishlistItem.variant_id == payload.variant_id,
    ).first()
    if existing:
        # Idempotent: tapping the heart twice is not an error.
        row = existing
    else:
        row = models.WishlistItem(
            customer_id=current.id, product_id=payload.product_id,
            variant_id=payload.variant_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return list_wishlist_item(db, row)


def list_wishlist_item(db: Session, row: models.WishlistItem) -> schemas.WishlistItemOut:
    product = row.product
    return schemas.WishlistItemOut(
        id=row.id, product_id=row.product_id, variant_id=row.variant_id,
        product_name=product.name if product else "(removed)",
        variant_label=row.variant.label if row.variant else None,
        price=(row.variant.effective_price(product) if row.variant and product
               else (product.price if product else None)),
        image_url=product.primary_image_url if product else None,
        in_stock=bool(row.variant.stock) if row.variant
                 else bool(product and product.total_stock),
        added_at=row.created_at,
    )


@router.delete("/wishlist/{item_id}", status_code=204)
def remove_from_wishlist(item_id: str,
                         current: models.Customer = Depends(get_current_customer),
                         db: Session = Depends(get_db)):
    row = db.query(models.WishlistItem).filter(
        models.WishlistItem.id == item_id,
        models.WishlistItem.customer_id == current.id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not in your wishlist")
    db.delete(row)
    db.commit()
