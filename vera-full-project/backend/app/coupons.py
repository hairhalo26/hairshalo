"""Coupon redemption rules — the single place a discount is decided.

Core principle: a coupon is only "applied" when it actually reduces what the
customer pays. A coupon that validates but changes nothing is rejected with a
readable reason instead of reporting false success.
"""
from decimal import Decimal
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.pricing import money


class CouponError(Exception):
    """Rejected coupon. The message is shown to the customer."""


def shipping_fee_for(subtotal) -> Decimal:
    """Flat shipping, waived above a threshold. Server-side only."""
    sub = money(subtotal or 0)
    if sub >= free_shipping_threshold():
        return Decimal("0.00")
    return money(settings.SHIPPING_FLAT_FEE)


def free_shipping_threshold() -> Decimal:
    """Subtotal at which shipping stops being charged.

    Published to the storefront so the basket can show progress towards free
    delivery using this number rather than a copy of it hardcoded in the page.
    """
    return money(settings.FREE_SHIPPING_THRESHOLD)


def find(db: Session, code: str, *, lock: bool = False) -> Optional[models.Coupon]:
    if not code:
        return None
    q = db.query(models.Coupon).filter(models.Coupon.code == code.strip().upper())
    if lock:
        # Checkout takes the row lock, so two orders racing for a coupon's
        # last use are serialised: the second sees the updated usage_count.
        q = q.with_for_update()
    return q.first()


# Orders in these states no longer count as having used a coupon.
_RELEASED = (models.OrderStatus.cancelled, models.OrderStatus.refunded)


def uses_by_customer(db: Session, coupon: models.Coupon, customer_id: str) -> int:
    """How many live orders this customer has placed with this coupon."""
    if not customer_id:
        return 0
    return db.query(models.Order).filter(
        models.Order.customer_id == customer_id,
        models.Order.coupon_code == coupon.code,
        ~models.Order.status.in_(_RELEASED),
    ).count()


def release(db: Session, order: models.Order) -> None:
    """Give a coupon use back when its order is cancelled or refunded."""
    if not order.coupon_code:
        return
    coupon = find(db, order.coupon_code, lock=True)
    if coupon and (coupon.usage_count or 0) > 0:
        coupon.usage_count = coupon.usage_count - 1


def evaluate(db: Session, code: str, subtotal, shipping, *, customer_id: str = None,
             lock: bool = False) -> Tuple[models.Coupon, Decimal, Decimal, str]:
    """Validate a coupon against a real basket.

    Returns (coupon, goods_discount, shipping_discount, human_message).
    Raises CouponError with a customer-readable reason when it cannot apply.

    `customer_id` enables the per-customer limit; without it (an anonymous
    preview) that rule cannot be checked here, and checkout checks it again.
    `lock` is for checkout only — see `find`.
    """
    coupon = find(db, code, lock=lock)
    if not coupon:
        raise CouponError("That coupon code was not recognised.")
    if not coupon.active:
        raise CouponError(f"{coupon.code} is no longer active.")
    if coupon.not_started:
        raise CouponError(f"{coupon.code} is not valid until {coupon.starts_at:%d %b %Y}.")
    if coupon.is_expired:
        raise CouponError(f"{coupon.code} expired on {coupon.expires_at:%d %b %Y}.")
    if coupon.is_exhausted:
        raise CouponError(f"{coupon.code} has reached its usage limit.")
    if coupon.per_customer_limit is not None and             uses_by_customer(db, coupon, customer_id) >= coupon.per_customer_limit:
        times = "once" if coupon.per_customer_limit == 1 else f"{coupon.per_customer_limit} times"
        raise CouponError(f"{coupon.code} can be used {times} per customer, and you already have.")

    sub = money(subtotal or 0)
    ship = money(shipping or 0)

    if coupon.min_order_amount is not None and sub < money(coupon.min_order_amount):
        raise CouponError(
            f"{coupon.code} needs a minimum order of ₹{money(coupon.min_order_amount):,.0f}."
        )

    goods_discount = Decimal("0.00")
    shipping_discount = Decimal("0.00")

    if coupon.discount_type == models.DiscountType.percent:
        pct = money(coupon.discount_value or 0)
        if pct <= 0:
            raise CouponError(f"{coupon.code} is not configured with a discount and cannot be applied.")
        if pct > 100:
            raise CouponError(f"{coupon.code} is misconfigured and cannot be applied.")
        goods_discount = money(sub * pct / Decimal("100"))
        # format(..., 'f') avoids Decimal.normalize() rendering 10 as "1E+1"
        message = f"{coupon.code} applied — {format(pct.normalize(), 'f')}% off"
        if coupon.max_discount_amount is not None and                 goods_discount > money(coupon.max_discount_amount):
            goods_discount = money(coupon.max_discount_amount)
            message += f" (up to ₹{goods_discount:,.0f})"

    elif coupon.discount_type == models.DiscountType.flat:
        amount = money(coupon.discount_value or 0)
        if amount <= 0:
            raise CouponError(f"{coupon.code} is not configured with a discount and cannot be applied.")
        goods_discount = min(amount, sub)
        message = f"{coupon.code} applied — ₹{goods_discount:,.0f} off"

    elif coupon.discount_type == models.DiscountType.free_shipping:
        if ship <= 0:
            # Nothing to waive — say so rather than reporting a phantom saving.
            raise CouponError(
                f"{coupon.code} waives delivery, but this order already ships free."
            )
        shipping_discount = ship
        message = f"{coupon.code} applied — free delivery (₹{ship:,.0f} off)"
    else:
        raise CouponError(f"{coupon.code} is misconfigured and cannot be applied.")

    total_saving = money(goods_discount + shipping_discount)
    if total_saving <= 0:
        raise CouponError(f"{coupon.code} would not reduce this order's total.")

    return coupon, goods_discount, shipping_discount, message
