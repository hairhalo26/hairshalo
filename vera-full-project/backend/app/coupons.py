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


def find(db: Session, code: str) -> Optional[models.Coupon]:
    if not code:
        return None
    return db.query(models.Coupon).filter(
        models.Coupon.code == code.strip().upper()
    ).first()


def evaluate(db: Session, code: str, subtotal, shipping) -> Tuple[models.Coupon, Decimal, Decimal, str]:
    """Validate a coupon against a real basket.

    Returns (coupon, goods_discount, shipping_discount, human_message).
    Raises CouponError with a customer-readable reason when it cannot apply.
    """
    coupon = find(db, code)
    if not coupon:
        raise CouponError("That coupon code was not recognised.")
    if not coupon.active:
        raise CouponError(f"{coupon.code} is no longer active.")
    if coupon.is_expired:
        raise CouponError(f"{coupon.code} expired on {coupon.expires_at:%d %b %Y}.")
    if coupon.is_exhausted:
        raise CouponError(f"{coupon.code} has reached its usage limit.")

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
