"""Canonical pricing logic — the single source of truth for money.

Design note (why there is no `selling_price` column):

The project already stores `price` (what the customer actually pays) and
`compare_at_price` (the struck-through original). Adding `original_price` /
`selling_price` / `final_price` columns would duplicate those concepts and
create four ways to be wrong. Instead the canonical relationship is:

    compare_at_price  = original price, and is NULL when there is no discount
    price             = the actual selling price (this is what orders charge)
    discount_type     = none | percentage | fixed_amount
    discount_value    = the admin's input (20 for 20%, or 5000 for ₹5,000)
    discount_amount   = DERIVED (compare_at_price - price), never stored

`price` keeps its existing meaning, so orders, analytics and every existing
query keep working untouched.

All money is Decimal quantised to 2dp with ROUND_HALF_UP. Floats are never used
for currency arithmetic.
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple

TWO_PLACES = Decimal("0.01")


class PricingError(ValueError):
    """Raised when a pricing combination is invalid or inconsistent."""


def money(value) -> Decimal:
    """Coerce to a 2dp Decimal without going through binary float."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        d = value
    else:
        d = Decimal(str(value))
    return d.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def compute_pricing(
    original_price,
    discount_type: Optional[str],
    discount_value,
) -> Tuple[Decimal, Optional[Decimal], Decimal, str, Decimal]:
    """Validate and compute a price set.

    Returns (price, compare_at_price, discount_amount, discount_type, discount_value)
    where `price` is the selling price and `compare_at_price` is NULL when the
    product is not discounted (so the storefront never renders a fake sale).

    Raises PricingError with a customer-readable message on invalid input.
    """
    dtype = (discount_type or "none").strip().lower()
    if dtype not in ("none", "percentage", "fixed_amount"):
        raise PricingError(
            "Discount type must be one of: none, percentage, fixed_amount."
        )

    if original_price is None:
        raise PricingError("Original price is required.")

    original = money(original_price)
    if original < 0:
        raise PricingError("Original price cannot be negative.")

    value = money(discount_value or 0)
    if value < 0:
        raise PricingError("Discount value cannot be negative.")

    if dtype == "none" or value == 0:
        # No discount: price IS the original, and there is no struck-through price.
        return original, None, Decimal("0.00"), "none", Decimal("0.00")

    if dtype == "percentage":
        if value > 100:
            raise PricingError("Discount percentage must be between 0 and 100.")
        amount = money(original * value / Decimal("100"))
    else:  # fixed_amount
        if value > original:
            raise PricingError("Fixed discount cannot exceed the original price.")
        amount = value

    selling = money(original - amount)
    if selling < 0:
        raise PricingError("Final selling price cannot be negative.")

    # Guard against an inconsistent set slipping through (e.g. rounding drift).
    if selling > original:
        raise PricingError("Selling price cannot be greater than the original price.")

    return selling, original, amount, dtype, value


def discount_amount_for(price, compare_at_price) -> Decimal:
    """Derived discount amount. Zero unless there is a genuine markdown."""
    if price is None or compare_at_price is None:
        return Decimal("0.00")
    p, c = money(price), money(compare_at_price)
    return money(c - p) if c > p else Decimal("0.00")


def discount_percent_for(price, compare_at_price) -> int:
    """Whole-number discount percentage, 0 when not discounted.

    Rounded for display only; the amount charged always comes from `price`.
    """
    if price is None or compare_at_price is None:
        return 0
    p, c = money(price), money(compare_at_price)
    if c <= 0 or c <= p:
        return 0
    pct = (c - p) / c * Decimal("100")
    return int(pct.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def is_on_sale(price, compare_at_price) -> bool:
    """True only when the stored pricing genuinely supports a sale claim."""
    if price is None or compare_at_price is None:
        return False
    return money(compare_at_price) > money(price)
