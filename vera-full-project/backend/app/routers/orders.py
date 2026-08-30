import random
import string
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_admin
from app import (
    models, schemas, inventory, coupons, currency, payments as gateway,
    notifications as notify, loyalty,
)
from app.pricing import money

router = APIRouter(prefix="/api/orders", tags=["orders"])

# Guards against a client sending an absurd quantity to probe or to lock rows.
MAX_LINE_QUANTITY = 100

# Valid order-status transitions. Uses only the statuses the model defines, so
# e.g. Delivered cannot be moved back to Processing.
ALLOWED_TRANSITIONS = {
    # Payment states. Pending Payment -> Paid happens via the gateway, not here.
    "Pending Payment": {"Cancelled"},
    "Paid": {"Processing", "Cancelled", "Refunded"},
    "Processing": {"Shipped", "Cancelled", "Refunded"},
    "Shipped": {"Out for Delivery", "Delivered", "Cancelled", "Refunded"},
    "Out for Delivery": {"Delivered", "Cancelled", "Refunded"},
    "Delivered": {"Refunded"},
    "Cancelled": set(),          # terminal
    "Refunded": set(),           # terminal
}

# Moving to these by hand returns reserved stock to the shelf.
RESTOCKING_STATUSES = {"Cancelled", "Refunded"}


def generate_order_number(db: Session) -> str:
    """Allocate an order number that is not already taken.

    Four random digits collide often enough to fail real checkouts (a birthday
    collision is likely within a few hundred orders), so we verify uniqueness
    against the database and widen the space if the short form is exhausted.
    """
    for _ in range(25):
        candidate = "VR-" + "".join(random.choices(string.digits, k=4))
        if not db.query(models.Order.id).filter(models.Order.order_number == candidate).first():
            return candidate
    for _ in range(25):
        candidate = "VR-" + "".join(random.choices(string.digits, k=8))
        if not db.query(models.Order.id).filter(models.Order.order_number == candidate).first():
            return candidate
    raise HTTPException(status_code=500, detail="Could not allocate an order number")


@router.post("", response_model=schemas.OrderOut, status_code=201)
def create_order(payload: schemas.OrderCreate, background: BackgroundTasks,
                 db: Session = Depends(get_db)):
    if not payload.items:
        raise HTTPException(status_code=400, detail="Order must contain at least one item")

    # find or create the customer record
    customer = db.query(models.Customer).filter(
        models.Customer.email == payload.customer_email
    ).first()
    if not customer:
        customer = models.Customer(name=payload.customer_name, email=payload.customer_email)
        db.add(customer)
        db.flush()

    order_items = []
    pending_movements = []
    # (product, variant) pairs whose stock this order consumed — checked for
    # low-stock alerts once the order itself is safely written.
    stock_touched = []
    total = Decimal("0.00")
    for item in payload.items:
        if item.quantity is None or item.quantity < 1:
            raise HTTPException(status_code=400, detail="Quantity must be at least 1")

        product = db.query(models.Product).filter(models.Product.id == item.product_id).first()
        if not product:
            # Placeholders are not sellable. Reject them explicitly rather than
            # returning a generic 404, so the caller knows why it was refused.
            placeholder = db.query(models.ProductPlaceholder).filter(
                models.ProductPlaceholder.id == item.product_id
            ).first()
            if placeholder:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"'{placeholder.name}' is a product placeholder and cannot be ordered. "
                        "Convert it to a real product first."
                    ),
                )
            raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found")

        # Only published products may be purchased. Draft / review / archived /
        # out-of-stock products are rejected even if the id is valid.
        if product.status != models.ProductStatus.published:
            raise HTTPException(
                status_code=400,
                detail=f"'{product.name}' is not available for purchase (status: {product.status.value}).",
            )

        if item.quantity > MAX_LINE_QUANTITY:
            raise HTTPException(
                status_code=400,
                detail=f"Maximum {MAX_LINE_QUANTITY} units per item per order.",
            )

        variant = None
        if item.variant_id:
            # SELECT ... FOR UPDATE: serialises concurrent checkouts of the same
            # variant so two orders cannot both pass the stock check and oversell.
            # The lock is held until this transaction commits or rolls back.
            variant = inventory.lock_variant(db, item.variant_id)
            if not variant or variant.product_id != product.id:
                raise HTTPException(
                    status_code=400,
                    detail=f"Variant {item.variant_id} does not belong to '{product.name}'.",
                )
            if not variant.is_available:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{product.name} — {variant.label}' is currently unavailable.",
                )
        elif product.variants:
            # The product is sold by variant, so one must be chosen.
            available = [v for v in product.variants if v.is_available]
            if available:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{product.name}' requires a variant to be selected.",
                )
            raise HTTPException(
                status_code=400,
                detail=f"'{product.name}' has no available variants.",
            )

        # Server-side stock validation.
        stock = variant.stock if variant else product.total_stock
        if stock is None or stock < item.quantity:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Insufficient stock for '{product.name}"
                    f"{' — ' + variant.label if variant else ''}': "
                    f"{stock or 0} available, {item.quantity} requested."
                ),
            )

        # Price ALWAYS comes from the database, never from the request payload.
        # `OrderItemIn` has no price field, so there is nothing to trust.
        unit_price = money(variant.effective_price(product) if variant else product.price)
        compare_at = money(
            variant.effective_compare_at(product) if variant else product.compare_at_price
        )
        if unit_price is None:
            raise HTTPException(
                status_code=400,
                detail=f"'{product.name}' has no price set and cannot be ordered.",
            )
        if compare_at is not None and compare_at < unit_price:
            # Inconsistent stored pricing — refuse rather than charge something odd.
            raise HTTPException(
                status_code=400,
                detail=f"'{product.name}' has inconsistent pricing and cannot be ordered.",
            )

        total += unit_price * item.quantity

        if variant:
            # Deduct through the inventory service so the movement log and the
            # running total are written together, under the lock taken above.
            try:
                pending_movements.append((variant, item.quantity))
                inventory.consume_for_order(db, variant, item.quantity)
                stock_touched.append((product, variant))
            except inventory.InsufficientStock as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        # Descriptive fields are snapshots, so later product edits never
        # rewrite what this customer actually bought.
        order_items.append(models.OrderItem(
            product_id=product.id,
            variant_id=variant.id if variant else None,
            product_name=product.name,
            variant_label=variant.label if variant else None,
            variant_sku=variant.sku if variant else None,
            quantity=item.quantity,
            price=unit_price,
            compare_at_price=compare_at,
        ))

    subtotal = money(total)
    shipping_fee = coupons.shipping_fee_for(subtotal)
    discount_total = Decimal("0.00")
    applied_code = None

    # Coupon is re-validated here at final checkout, never trusted from the
    # client. A coupon that cannot actually reduce the total is rejected.
    if payload.coupon_code:
        try:
            coupon, goods_off, ship_off, _msg = coupons.evaluate(
                db, payload.coupon_code, subtotal, shipping_fee
            )
        except coupons.CouponError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        discount_total = money(goods_off)
        shipping_fee = money(shipping_fee - ship_off)
        coupon.usage_count = (coupon.usage_count or 0) + 1
        applied_code = coupon.code

    # --- loyalty redemption ------------------------------------------
    # The client says how many POINTS to spend, never what they are worth. The
    # value, the balance and the ceiling are all decided here, against the
    # goods total after any coupon — so points can never discount an order
    # twice or take it below zero.
    loyalty_points_redeemed = 0
    loyalty_discount = Decimal("0.00")
    locked_customer = None
    if payload.redeem_loyalty_points:
        payable_goods = money(subtotal - discount_total)
        # Row lock: two simultaneous checkouts by the same customer must not
        # both spend the same balance.
        locked_customer = loyalty.lock_customer(db, customer.id)
        try:
            loyalty_points_redeemed, loyalty_discount = loyalty.redeem_for_order(
                db, locked_customer, payload.redeem_loyalty_points, payable_goods
            )
        except loyalty.LoyaltyError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    total = money(subtotal - discount_total - loyalty_discount + shipping_fee)
    total = max(total, Decimal("0.00"))

    # Resolve the display currency ourselves. An unknown code degrades to INR
    # rather than failing the order.
    display_code = currency.resolve_currency(payload.display_currency)
    display_rate, _rate_source = currency.rate_for(display_code)

    order = models.Order(
        order_number=generate_order_number(db),
        customer_id=customer.id,
        customer_name=payload.customer_name,
        customer_email=payload.customer_email,
        shipping_address=payload.shipping_address,
        subtotal=subtotal,
        discount_total=discount_total,
        shipping_fee=shipping_fee,
        coupon_code=applied_code,
        loyalty_points_redeemed=loyalty_points_redeemed,
        loyalty_discount=money(loyalty_discount),
        total=money(total),
        # Settlement is always INR. The display currency is a record of what
        # the customer saw; the rate is looked up server-side, never accepted
        # from the request.
        # With a gateway configured the order waits at Pending Payment until
        # the gateway confirms. With payments disabled it goes straight to
        # Processing, exactly as before.
        status=(models.OrderStatus.pending_payment if gateway.payments_enabled()
                else models.OrderStatus.processing),
        currency=currency.BASE,
        display_currency=display_code if display_code != currency.BASE else None,
        display_rate=display_rate if display_code != currency.BASE else None,
        display_total=currency.convert(total, display_code) if display_code != currency.BASE else None,
        items=order_items,
    )
    db.add(order)
    db.flush()
    # Back-fill the movement rows with the order they belong to, now that the
    # order has an id. Same transaction, so the audit trail is atomic with it.
    for mv in db.new:
        if isinstance(mv, models.InventoryMovement) and mv.reference_type == "order" \
                and mv.reference_id is None:
            mv.reference_id = order.id
    # --- loyalty bookkeeping -----------------------------------------
    # Spending is recorded now, against the order that spent it.
    if loyalty_points_redeemed and locked_customer is not None:
        loyalty.apply(
            db, locked_customer, -loyalty_points_redeemed,
            models.LoyaltyReason.redeemed, reference_type="order",
            reference_id=order.id,
            note=f"Redeemed against {order.order_number}",
        )
    # Earning waits for payment — an abandoned checkout must not mint points.
    # With no gateway configured the order is already Processing, which is the
    # same moment: it is no longer waiting for money.
    if not gateway.payments_enabled():
        loyalty.earn_for_order(db, order)

    # Queue the emails INSIDE this transaction, so they cannot describe an
    # order that never committed — and send them afterwards, so a mail server
    # can never fail a checkout. Done after the movement back-fill above
    # because queueing flushes the session.
    notify.notify_order_placed(
        db, order,
        payment_instructions=gateway.get_provider().checkout_instructions,
    )
    for touched_product, touched_variant in stock_touched:
        notify.check_low_stock(db, touched_variant, touched_product)

    # Single commit: stock decrements, the order, its items and its queued
    # notifications all land together, or none of them do. Any raise above
    # rolls the whole thing back and releases the row locks.
    db.commit()
    db.refresh(order)
    notify.schedule_dispatch(background)
    return order


@router.get("", response_model=List[schemas.OrderOut])
def list_orders(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    q = db.query(models.Order).options(joinedload(models.Order.items))
    if status:
        q = q.filter(models.Order.status == status)
    return q.order_by(models.Order.created_at.desc()).all()


@router.get("/{order_id}", response_model=schemas.OrderOut)
def get_order(
    order_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    order = db.query(models.Order).options(joinedload(models.Order.items)).filter(
        models.Order.id == order_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.put("/{order_id}/status", response_model=schemas.OrderOut)
def update_order_status(
    order_id: str,
    payload: schemas.OrderStatusUpdate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if payload.status not in [s.value for s in models.OrderStatus]:
        raise HTTPException(status_code=400, detail="Invalid status")

    current = order.status.value if order.status else models.OrderStatus.processing.value
    target = payload.status
    if current == target:
        return order

    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot move an order from {current} to {target}. "
                f"Allowed from {current}: {', '.join(sorted(allowed)) or 'none'}."
            ),
        )

    # Cancelling or refunding also unwinds loyalty: points earned on this
    # order are clawed back, points spent on it are returned.
    if target in RESTOCKING_STATUSES:
        loyalty.reverse_for_order(db, order, actor=admin.email)

    # Cancelling or refunding returns stock to the shelf, through the audit trail.
    if target in RESTOCKING_STATUSES:
        reason = (models.MovementReason.refund if target == "Refunded"
                  else models.MovementReason.cancellation)
        for line in order.items:
            if not line.variant_id:
                continue
            variant = inventory.lock_variant(db, line.variant_id)
            if variant:
                inventory.release_for_order(
                    db, variant, line.quantity, order.id, reason, actor=admin.email,
                )

    order.status = target
    # Same transaction as the status change: the customer is only ever told
    # about a status that actually persisted.
    notify.notify_order_status_change(db, order, target)
    db.commit()
    db.refresh(order)
    notify.schedule_dispatch(background)
    return order
