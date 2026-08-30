"""Pricing engine unit tests — no database or server required.

    python -m pytest tests/ -q
"""
from decimal import Decimal

import pytest

from app.pricing import (
    compute_pricing, PricingError, discount_amount_for,
    discount_percent_for, is_on_sale, money,
)


def price_of(original, dtype, dvalue):
    price, compare_at, amount, _t, _v = compute_pricing(original, dtype, dvalue)
    return price, compare_at, amount


# ---------------- valid combinations ----------------

def test_no_discount_leaves_compare_at_null():
    price, compare_at, amount = price_of(25000, "none", 0)
    assert price == Decimal("25000.00")
    assert compare_at is None          # so the storefront cannot fake a sale
    assert amount == Decimal("0.00")


def test_zero_percent_is_treated_as_no_discount():
    price, compare_at, _ = price_of(25000, "percentage", 0)
    assert price == Decimal("25000.00")
    assert compare_at is None


def test_twenty_percent():
    price, compare_at, amount = price_of(25000, "percentage", 20)
    assert (price, compare_at, amount) == (
        Decimal("20000.00"), Decimal("25000.00"), Decimal("5000.00"))


def test_fixed_amount():
    price, compare_at, amount = price_of(25000, "fixed_amount", 5000)
    assert (price, compare_at, amount) == (
        Decimal("20000.00"), Decimal("25000.00"), Decimal("5000.00"))


def test_hundred_percent_is_allowed():
    price, compare_at, amount = price_of(25000, "percentage", 100)
    assert price == Decimal("0.00")
    assert amount == Decimal("25000.00")


@pytest.mark.parametrize("original,pct,expected", [
    (20000, 10, "18000.00"),
    (25000, 10, "22500.00"),
    (32000, 15, "27200.00"),
])
def test_variant_pricing_table(original, pct, expected):
    price, _c, _a = price_of(original, "percentage", pct)
    assert price == Decimal(expected)


def test_money_uses_decimal_not_float():
    # 0.1 + 0.2 must be exactly 0.30, which float arithmetic cannot guarantee
    assert money("0.1") + money("0.2") == Decimal("0.30")


def test_rounding_is_half_up_to_two_places():
    price, _c, _a = price_of("19.99", "percentage", "33.333")
    assert price == Decimal("13.33")


# ---------------- rejected combinations ----------------

@pytest.mark.parametrize("original,dtype,dvalue,fragment", [
    (25000, "percentage", 150, "between 0 and 100"),
    (25000, "percentage", 101, "between 0 and 100"),
    (25000, "percentage", -10, "cannot be negative"),
    (25000, "fixed_amount", 30000, "cannot exceed the original price"),
    (-100, "none", 0, "cannot be negative"),
    (25000, "bogus", 10, "must be one of"),
    (None, "none", 0, "Original price is required"),
])
def test_invalid_pricing_is_rejected(original, dtype, dvalue, fragment):
    with pytest.raises(PricingError) as exc:
        compute_pricing(original, dtype, dvalue)
    assert fragment in str(exc.value)


# ---------------- derived helpers ----------------

def test_no_fake_sale_when_compare_at_not_greater():
    assert is_on_sale(20000, 20000) is False
    assert is_on_sale(20000, 15000) is False     # compare_at below price
    assert discount_percent_for(20000, 20000) == 0
    assert discount_amount_for(20000, 15000) == Decimal("0.00")


def test_sale_detected_only_on_genuine_markdown():
    assert is_on_sale(20000, 25000) is True
    assert discount_percent_for(20000, 25000) == 20
    assert discount_amount_for(20000, 25000) == Decimal("5000.00")


def test_percent_is_rounded_for_display():
    # 18999 off 22999 is 17.39% -> displayed as 17
    assert discount_percent_for(18999, 22999) == 17
