"""Loyalty points — earning, spending, and the ledger that explains both.

Non-negotiable rule this module exists to enforce:

    Points are currency. They are minted only when money actually arrives, they
    are spent only against a real order, and every movement is written down.

Before this module existed, `Customer.loyalty_points` was incremented the
moment an order was created — so an abandoned, never-paid checkout minted
points — and nothing could ever spend them. Both halves are fixed here:

* **Earning happens on payment**, not on order creation. With payments disabled
  the order is Processing immediately and earning happens then, which is the
  same rule ("when the order is no longer waiting for money").
* **Spending happens at checkout**, server-side. The client says how many
  points to use; the server decides what that is worth, clamps it to the
  balance and to a cap, and writes the deduction in the same transaction as
  the order.
* Cancellations and refunds reverse both directions: earned points are clawed
  back, redeemed points are returned.

`Customer.loyalty_points` is the authoritative balance; `loyalty_transactions`
explains it. Nothing outside this module may assign to the balance — the same
contract `app/inventory.py` has with stock.
"""
import logging
from decimal import Decimal
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.pricing import money

logger = logging.getLogger("vera.loyalty")


class LoyaltyError(Exception):
    """Rejected redemption. The message is shown to the customer."""


def points_value(points: int) -> Decimal:
    """What `points` are worth in rupees."""
    return money(Decimal(str(max(0, int(points or 0)))) * Decimal(str(settings.LOYALTY_POINT_VALUE)))


def points_for_spend(amount) -> int:
    """How many points an order of `amount` earns.

    Earning is floored, deliberately: rounding up would let a customer mint a
    fraction of a point of value out of nothing on every order.
    """
    per = Decimal(str(settings.LOYALTY_EARN_PER))
    if per <= 0:
        return 0
    return int(money(amount or 0) // per)


def max_redeemable(db: Session, customer: models.Customer, subtotal) -> int:
    """The most points this customer may spend on this basket.

    Capped as a percentage of the goods total, so points discount an order
    rather than replacing payment for it — otherwise a large balance turns into
    free product and the shop ships stock for nothing.
    """
    if not customer:
        return 0
    balance = max(0, customer.loyalty_points or 0)
    if balance <= 0 or settings.LOYALTY_POINT_VALUE <= 0:
        return 0
    cap_fraction = Decimal(str(settings.LOYALTY_MAX_REDEEM_PCT)) / Decimal("100")
    cap_amount = money(money(subtotal or 0) * cap_fraction)
    cap_points = int(cap_amount / Decimal(str(settings.LOYALTY_POINT_VALUE)))
    return max(0, min(balance, cap_points))


def lock_customer(db: Session, customer_id: str) -> Optional[models.Customer]:
    """Fetch a customer with a row lock held until the transaction ends.

    Two concurrent checkouts by the same customer would otherwise both read the
    same balance and each spend it — the same double-spend the stock code takes
    a lock to prevent.
    """
    return (
        db.query(models.Customer)
        .filter(models.Customer.id == customer_id)
        .with_for_update()
        .first()
    )


def apply(db: Session, customer: models.Customer, delta: int,
          reason: models.LoyaltyReason, *, reference_type: str = None,
          reference_id: str = None, note: str = None, actor: str = None,
          allow_negative: bool = False) -> Optional[models.LoyaltyTransaction]:
    """Change a balance by `delta` and record why. Caller must hold the lock.

    Raises LoyaltyError when the result would go below zero, unless
    `allow_negative` (used by admin corrections after a chargeback).
    """
    if not customer or not delta:
        return None

    current = customer.loyalty_points or 0
    new_balance = current + int(delta)
    if new_balance < 0 and not allow_negative:
        raise LoyaltyError(
            f"That would leave a negative balance: {current} points available, "
            f"{abs(int(delta))} requested."
        )

    customer.loyalty_points = new_balance
    entry = models.LoyaltyTransaction(
        customer_id=customer.id,
        delta=int(delta),
        balance_after=new_balance,
        reason=reason,
        reference_type=reference_type,
        reference_id=reference_id,
        note=note,
        actor=actor,
    )
    db.add(entry)
    return entry


def redeem_for_order(db: Session, customer: models.Customer, requested_points: int,
                     subtotal) -> Tuple[int, Decimal]:
    """Spend points against a basket. Returns (points_spent, discount).

    The client asks for a number of points; it never states what they are
    worth. The value comes from settings, the balance from the database, and
    the ceiling from `max_redeemable` — so the worst a manipulated request can
    do is ask for more than it gets.
    """
    if not requested_points or requested_points <= 0:
        return (0, Decimal("0.00"))
    if not customer:
        raise LoyaltyError("Points can only be redeemed against a customer account.")
    if settings.LOYALTY_POINT_VALUE <= 0:
        raise LoyaltyError("Points redemption is not enabled.")

    balance = max(0, customer.loyalty_points or 0)
    if balance <= 0:
        raise LoyaltyError("There are no points on this account yet.")

    ceiling = max_redeemable(db, customer, subtotal)
    if ceiling <= 0:
        raise LoyaltyError(
            f"Points cover up to {settings.LOYALTY_MAX_REDEEM_PCT}% of an order, "
            "and this basket is too small to apply any."
        )
    if requested_points > balance:
        raise LoyaltyError(
            f"Only {balance} points are available on this account."
        )

    spent = min(int(requested_points), ceiling)
    return (spent, points_value(spent))


def earn_for_order(db: Session, order: models.Order, actor: str = None) -> Optional[models.LoyaltyTransaction]:
    """Award points for an order that has been paid for. Idempotent.

    Keyed on the order: a replayed payment webhook, a manual re-confirmation and
    a status change that lands on the same order all award once.
    """
    if not order.customer_id:
        return None
    already = db.query(models.LoyaltyTransaction).filter(
        models.LoyaltyTransaction.reference_id == order.id,
        models.LoyaltyTransaction.reason == models.LoyaltyReason.earned,
    ).first()
    if already:
        return None

    # Earn on what was actually paid — points redeemed and coupon discounts
    # reduce the spend, so they must not earn points back.
    points = points_for_spend(order.total)
    if points <= 0:
        return None

    customer = lock_customer(db, order.customer_id)
    if not customer:
        return None
    return apply(
        db, customer, points, models.LoyaltyReason.earned,
        reference_type="order", reference_id=order.id,
        note=f"Order {order.order_number} paid", actor=actor,
    )


def reverse_for_order(db: Session, order: models.Order, actor: str = None) -> list:
    """Undo an order's loyalty effects: claw back what it earned, return what it
    spent. Idempotent in both directions.
    """
    if not order.customer_id:
        return []
    entries = db.query(models.LoyaltyTransaction).filter(
        models.LoyaltyTransaction.reference_id == order.id
    ).all()
    by_reason = {e.reason: e for e in entries}
    written = []

    customer = lock_customer(db, order.customer_id)
    if not customer:
        return []

    earned = by_reason.get(models.LoyaltyReason.earned)
    if earned and models.LoyaltyReason.reversed not in by_reason:
        # allow_negative: the customer may already have spent the points
        # elsewhere. A negative balance is a fact to be corrected, not a reason
        # to leave phantom points in circulation.
        written.append(apply(
            db, customer, -earned.delta, models.LoyaltyReason.reversed,
            reference_type="order", reference_id=order.id,
            note=f"Order {order.order_number} {order.status.value.lower() if order.status else 'reversed'}",
            actor=actor, allow_negative=True,
        ))

    redeemed = by_reason.get(models.LoyaltyReason.redeemed)
    if redeemed and models.LoyaltyReason.returned not in by_reason:
        written.append(apply(
            db, customer, abs(redeemed.delta), models.LoyaltyReason.returned,
            reference_type="order", reference_id=order.id,
            note=f"Points returned from order {order.order_number}", actor=actor,
        ))
    return [w for w in written if w]


def adjust(db: Session, customer_id: str, delta: int, note: str,
           actor: str) -> models.LoyaltyTransaction:
    """Admin correction. Takes its own lock."""
    customer = lock_customer(db, customer_id)
    if not customer:
        raise LoyaltyError("Customer not found.")
    return apply(
        db, customer, delta, models.LoyaltyReason.adjustment,
        note=note, actor=actor, allow_negative=True,
    )


def balance_report(db: Session, customer: models.Customer) -> dict:
    """Balance plus what it is worth, for the admin view."""
    balance = max(0, customer.loyalty_points or 0)
    return {
        "customer_id": customer.id,
        "email": customer.email,
        "balance": balance,
        "value": points_value(balance),
        "point_value": Decimal(str(settings.LOYALTY_POINT_VALUE)),
        "earn_per": Decimal(str(settings.LOYALTY_EARN_PER)),
        "max_redeem_pct": settings.LOYALTY_MAX_REDEEM_PCT,
    }
