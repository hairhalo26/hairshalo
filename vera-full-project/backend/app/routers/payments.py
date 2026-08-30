"""Payment endpoints.

Flow enforced here:

    checkout -> POST /api/payments/intent        (server creates the intent)
             -> customer pays at the gateway
             -> POST /api/payments/confirm       (signature verified server-side)
                and/or
                POST /api/payments/webhook/{p}   (signature verified server-side)
             -> order becomes Paid, stock finalised

Nothing in this file trusts a client-declared status. `confirm` verifies the
gateway's HMAC over ids the gateway itself issued; the webhook verifies the
gateway's HMAC over the raw body.
"""
from decimal import Decimal
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_admin, get_optional_customer
from app import (
    models, schemas, payments as gateway, inventory,
    notifications as notify, loyalty,
)
from app.pricing import money

router = APIRouter(prefix="/api/payments", tags=["payments"])


def _payment_out(p: models.Payment) -> schemas.PaymentOut:
    return schemas.PaymentOut(
        id=p.id, order_id=p.order_id, provider=p.provider,
        provider_order_id=p.provider_order_id, provider_payment_id=p.provider_payment_id,
        status=p.status.value, amount=p.amount, currency=p.currency,
        amount_refunded=p.amount_refunded or Decimal("0.00"),
        method=p.method, reference=p.reference, note=p.note,
        error_code=p.error_code, error_message=p.error_message,
        created_at=p.created_at,
    )


def _apply_event(db: Session, payment: models.Payment, event: gateway.PaymentEvent) -> bool:
    """Apply a gateway event to a payment + its order. Idempotent.

    Returns True when state actually changed, False when the event was a
    duplicate (replayed webhook) and was safely ignored.
    """
    # Idempotency guard 1: the same event id already applied.
    if event.event_id and payment.last_event_id == event.event_id:
        return False
    # Idempotency guard 2: already in the target terminal state.
    target = {
        "paid": models.PaymentStatus.paid,
        "authorized": models.PaymentStatus.authorized,
        "failed": models.PaymentStatus.failed,
        "refunded": models.PaymentStatus.refunded,
        "cancelled": models.PaymentStatus.cancelled,
    }.get(event.status)
    if not target:
        raise HTTPException(status_code=400, detail=f"Unknown payment status '{event.status}'")
    if payment.status == target:
        payment.last_event_id = event.event_id
        return False

    # A captured payment must not be silently downgraded by a late event.
    if payment.status == models.PaymentStatus.paid and target in (
        models.PaymentStatus.pending, models.PaymentStatus.authorized,
    ):
        return False

    order = payment.order

    # Verify the gateway charged what we asked for.
    if target == models.PaymentStatus.paid and event.amount is not None:
        if money(event.amount) != money(payment.amount):
            payment.status = models.PaymentStatus.failed
            payment.error_code = "amount_mismatch"
            payment.error_message = (
                f"Gateway reported {event.amount} but the order total is {payment.amount}."
            )
            payment.last_event_id = event.event_id
            notify.notify_payment_failed(db, payment)
            db.commit()
            raise HTTPException(status_code=400, detail="Payment amount did not match the order.")

    payment.status = target
    payment.last_event_id = event.event_id
    if event.provider_payment_id:
        payment.provider_payment_id = event.provider_payment_id
    if event.method:
        payment.method = event.method
    if event.error_code:
        payment.error_code = event.error_code
    if event.error_message:
        payment.error_message = event.error_message

    # ---- order-side consequences ----
    if target == models.PaymentStatus.paid:
        # Stock was reserved when the order was created; payment finalises it.
        if order.status == models.OrderStatus.pending_payment:
            order.status = models.OrderStatus.paid
        # Money has arrived, so points are earned now. Idempotent: a replayed
        # webhook or a second confirmation awards once.
        loyalty.earn_for_order(db, order, actor="payment-gateway")
        notify.notify_payment_paid(db, payment)

    elif target in (models.PaymentStatus.failed, models.PaymentStatus.cancelled):
        # Release the reserved stock so it can be sold again.
        if order.status == models.OrderStatus.pending_payment:
            for line in order.items:
                if not line.variant_id:
                    continue
                variant = inventory.lock_variant(db, line.variant_id)
                if variant:
                    inventory.release_for_order(
                        db, variant, line.quantity, order.id,
                        models.MovementReason.cancellation,
                        actor="payment-gateway",
                    )
            order.status = models.OrderStatus.cancelled
            loyalty.reverse_for_order(db, order, actor="payment-gateway")
            notify.notify_order_event(db, order, "order.cancelled")
        notify.notify_payment_failed(db, payment)

    elif target == models.PaymentStatus.refunded:
        payment.amount_refunded = event.amount if event.amount is not None else payment.amount
        if order.status != models.OrderStatus.refunded:
            for line in order.items:
                if not line.variant_id:
                    continue
                variant = inventory.lock_variant(db, line.variant_id)
                if variant:
                    inventory.release_for_order(
                        db, variant, line.quantity, order.id,
                        models.MovementReason.refund, actor="payment-gateway",
                    )
            order.status = models.OrderStatus.refunded
            loyalty.reverse_for_order(db, order, actor="payment-gateway")
        notify.notify_payment_refunded(db, payment)

    db.commit()
    return True


@router.get("/config", response_model=schemas.PaymentConfigOut)
def payment_config():
    """What the checkout UI needs to know before it renders."""
    provider = gateway.get_provider()
    return schemas.PaymentConfigOut(
        provider=provider.name,
        enabled=provider.name != "none",
        holds_order=provider.holds_order,
        public_key=getattr(provider, "key_id", None) if provider.name == "razorpay" else None,
    )


def _authorised_for_order(order: models.Order, customer) -> bool:
    """A signed-in customer may only pay for their own orders.

    Guest checkout still works without a token — the order id is a UUIDv4 that
    was just handed to that browser, and nothing here reveals anything the
    payer did not already submit. But once a caller IS identified, using
    somebody else's order id is unambiguous abuse and is refused.
    """
    if customer is None:
        return True
    return order.customer_id == customer.id


@router.post("/intent", response_model=schemas.PaymentIntentOut, status_code=201)
def create_intent(payload: schemas.PaymentIntentRequest, db: Session = Depends(get_db),
                  customer=Depends(get_optional_customer)):
    """Create a gateway intent for an existing order. Amount comes from the DB."""
    order = db.query(models.Order).filter(models.Order.id == payload.order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not _authorised_for_order(order, customer):
        # 404, not 403: a signed-in attacker probing ids learns nothing.
        raise HTTPException(status_code=404, detail="Order not found")
    if order.is_paid:
        raise HTTPException(status_code=400, detail="This order has already been paid.")
    if order.status in (models.OrderStatus.cancelled, models.OrderStatus.refunded):
        raise HTTPException(status_code=400, detail=f"This order is {order.status.value.lower()}.")

    provider = gateway.get_provider()
    try:
        intent = provider.create_intent(order)
    except gateway.PaymentError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    payment = models.Payment(
        order_id=order.id, provider=intent.provider,
        provider_order_id=intent.provider_order_id,
        status=models.PaymentStatus.pending,
        amount=money(order.total), currency=order.currency or "INR",
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return schemas.PaymentIntentOut(
        payment_id=payment.id, provider=intent.provider,
        provider_order_id=intent.provider_order_id,
        amount=intent.amount, currency=intent.currency,
        public_key=intent.public_key, instructions=intent.instructions,
        extra=intent.extra,
    )


@router.post("/confirm", response_model=schemas.PaymentOut)
def confirm_payment(payload: schemas.PaymentConfirmRequest, background: BackgroundTasks,
                    db: Session = Depends(get_db),
                    customer=Depends(get_optional_customer)):
    """Confirm from the browser's gateway return — signature verified here.

    The client sends only ids the gateway issued plus its signature. A forged
    or replayed payload fails the HMAC check and nothing is marked paid.
    """
    payment = db.query(models.Payment).filter(models.Payment.id == payload.payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if not _authorised_for_order(payment.order, customer):
        raise HTTPException(status_code=404, detail="Payment not found")

    provider = gateway.get_provider()
    try:
        event = provider.verify_return(payload.gateway_response or {})
    except gateway.PaymentError as exc:
        payment.error_message = str(exc)
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc))

    if event.provider_order_id and payment.provider_order_id \
            and event.provider_order_id != payment.provider_order_id:
        raise HTTPException(status_code=400, detail="Payment does not belong to this order.")

    _apply_event(db, payment, event)
    notify.schedule_dispatch(background)
    db.refresh(payment)
    return _payment_out(payment)


@router.post("/webhook/{provider_name}")
async def webhook(provider_name: str, request: Request, background: BackgroundTasks,
                  db: Session = Depends(get_db)):
    """Gateway webhook. Signature-verified and idempotent."""
    provider = gateway.get_provider()
    if provider.name != provider_name:
        raise HTTPException(status_code=404, detail="Unknown payment provider")

    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        event = provider.verify_webhook(raw, headers)
    except gateway.PaymentError as exc:
        # 400 (not 500) so the gateway does not retry a bad signature forever.
        raise HTTPException(status_code=400, detail=str(exc))

    payment = None
    if event.provider_payment_id:
        payment = db.query(models.Payment).filter(
            models.Payment.provider_payment_id == event.provider_payment_id
        ).first()
    if not payment and event.provider_order_id:
        payment = db.query(models.Payment).filter(
            models.Payment.provider_order_id == event.provider_order_id
        ).first()
    if not payment:
        # Acknowledge unknown events so the gateway stops retrying.
        return {"received": True, "applied": False, "reason": "no matching payment"}

    changed = _apply_event(db, payment, event)
    notify.schedule_dispatch(background)
    return {"received": True, "applied": changed}


@router.get("/status/{payment_id}", response_model=schemas.PaymentStatusOut)
def payment_status(payment_id: str, db: Session = Depends(get_db),
                   customer=Depends(get_optional_customer)):
    """Where a payment got to — the ONLY thing the storefront may believe.

    The browser never decides that a payment succeeded; it asks this. That
    matters for the cases where the browser was not present at the decision:
    the customer refreshed, closed the tab, or the gateway confirmed by webhook
    minutes later.

    Deliberately narrow: status, the order's status and its number. No
    addresses, no line items, no customer details — the payment id is a UUID
    handed to one browser, not an authenticated identity.
    """
    payment = db.query(models.Payment).filter(models.Payment.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if not _authorised_for_order(payment.order, customer):
        raise HTTPException(status_code=404, detail="Payment not found")

    order = payment.order
    return schemas.PaymentStatusOut(
        payment_id=payment.id,
        payment_status=payment.status.value,
        order_id=order.id,
        order_number=order.order_number,
        order_status=order.status.value if order.status else None,
        is_paid=order.is_paid,
        amount=payment.amount,
        currency=payment.currency,
        error_message=payment.error_message,
    )


@router.get("/order/{order_id}", response_model=List[schemas.PaymentOut])
def payments_for_order(order_id: str, db: Session = Depends(get_db),
                       _admin=Depends(get_current_admin)):
    rows = db.query(models.Payment).filter(models.Payment.order_id == order_id).all()
    return [_payment_out(p) for p in rows]


@router.post("/{payment_id}/mark-paid", response_model=schemas.PaymentOut)
def mark_paid(payment_id: str, payload: schemas.ManualSettleRequest,
              background: BackgroundTasks, db: Session = Depends(get_db),
              admin=Depends(get_current_admin)):
    """Admin-only confirmation of an OFFLINE payment (bank transfer / COD).

    Restricted to the `manual` provider — an admin must never be able to
    hand-wave a real gateway transaction into a paid state.
    """
    payment = db.query(models.Payment).filter(models.Payment.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment.provider != "manual":
        raise HTTPException(
            status_code=400,
            detail="Only offline (manual) payments can be settled by hand; "
                   "gateway payments are confirmed by the gateway.",
        )
    # provider_payment_id must stay unique and gateway-issued; the human bank
    # reference lives in its own column.
    event = gateway.PaymentEvent(
        event_id=f"manual_{payment.id}_settled",
        provider_payment_id=f"manual_{payment.id}",
        provider_order_id=payment.provider_order_id,
        status="paid", amount=payment.amount, currency=payment.currency,
        method="offline",
    )
    _apply_event(db, payment, event)
    payment.reference = payload.reference
    payment.note = f"Confirmed by {admin.email}" + (f" — {payload.note}" if payload.note else "")
    db.commit()
    notify.schedule_dispatch(background)
    db.refresh(payment)
    return _payment_out(payment)


@router.post("/{payment_id}/refund", response_model=schemas.PaymentOut)
def refund_payment(payment_id: str, payload: schemas.RefundRequest,
                   background: BackgroundTasks, db: Session = Depends(get_db),
                   admin=Depends(get_current_admin)):
    payment = db.query(models.Payment).filter(models.Payment.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment.status != models.PaymentStatus.paid:
        raise HTTPException(status_code=400, detail="Only a paid payment can be refunded.")

    amount = money(payload.amount) if payload.amount is not None else money(payment.amount)
    if amount <= 0 or amount > money(payment.amount):
        raise HTTPException(status_code=400, detail="Refund amount must be between 0 and the amount paid.")

    provider = gateway.get_provider()
    refund_id = f"manual_refund_{payment.id}"
    if payment.provider != "manual":
        try:
            refund_id, amount = provider.refund(payment, amount)
        except gateway.PaymentError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

    payment.provider_refund_id = refund_id
    event = gateway.PaymentEvent(
        event_id=f"refund_{refund_id}", provider_payment_id=payment.provider_payment_id,
        provider_order_id=payment.provider_order_id, status="refunded",
        amount=amount, currency=payment.currency,
    )
    _apply_event(db, payment, event)
    notify.schedule_dispatch(background)
    db.refresh(payment)
    return _payment_out(payment)
