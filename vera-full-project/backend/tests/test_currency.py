"""Currency service tests.

Unit tests (no server) plus API tests for the display-currency contract:
INR stays canonical, the client can never supply a rate, and historical
orders keep the rate they were placed under.
"""
import os
import uuid
from decimal import Decimal

import pytest
import requests

from app import currency as cur

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@verahair.co", "password": "ChangeMe123!"}


# ---------------- unit: registry + conversion ----------------

def test_all_required_currencies_are_supported():
    required = {"INR", "GBP", "USD", "EUR", "AED", "SGD", "MYR", "LKR",
                "AUD", "CAD", "NZD", "JPY", "SAR", "QAR", "KWD", "CHF"}
    assert required.issubset(set(cur.CURRENCIES))


@pytest.mark.parametrize("code,decimals", [
    ("INR", 2), ("USD", 2), ("JPY", 0), ("KWD", 3),
])
def test_currency_decimal_precision(code, decimals):
    assert cur.CURRENCIES[code].decimals == decimals


def test_jpy_has_no_minor_units():
    assert cur.convert(20000, "JPY") == cur.convert(20000, "JPY").to_integral_value()
    assert "." not in cur.format_amount(cur.convert(20000, "JPY"), "JPY")


def test_kwd_uses_three_decimals():
    text = cur.format_amount(cur.convert(20000, "KWD"), "KWD")
    assert len(text.split(".")[-1]) == 3


def test_inr_conversion_is_identity():
    assert cur.convert(Decimal("1234.56"), "INR") == Decimal("1234.56")
    assert cur.rate_for("INR")[0] == Decimal("1")


def test_conversion_uses_decimal_not_float():
    assert isinstance(cur.convert(20000, "USD"), Decimal)


def test_unknown_currency_falls_back_to_inr():
    assert cur.resolve_currency("ZZZ") == "INR"
    assert cur.resolve_currency(None) == "INR"
    assert cur.resolve_currency("") == "INR"
    rate, _ = cur.rate_for("ZZZ")
    assert rate == Decimal("1")


def test_rate_is_never_zero_or_missing():
    """Guards the 'never show NaN / ₹0 / broken symbol' requirement."""
    for code in cur.CURRENCIES:
        rate, _src = cur.rate_for(code)
        assert rate > 0
        formatted = cur.format_amount(cur.convert(1000, code), code)
        assert formatted and "NaN" not in formatted and "undefined" not in formatted
        assert cur.CURRENCIES[code].symbol in formatted


def test_zero_amount_formats_cleanly():
    assert cur.format_amount(0, "USD") == "$0.00"
    assert cur.format_amount(0, "JPY") == "¥0"


# ---------------- unit: country mapping ----------------

@pytest.mark.parametrize("country,expected", [
    ("IN", "INR"), ("GB", "GBP"), ("US", "USD"), ("AE", "AED"),
    ("SG", "SGD"), ("MY", "MYR"), ("LK", "LKR"), ("JP", "JPY"),
    ("DE", "EUR"), ("FR", "EUR"), ("KW", "KWD"), ("CH", "CHF"),
])
def test_country_to_currency(country, expected):
    assert cur.currency_for_country(country) == expected


def test_unknown_country_returns_nothing():
    assert cur.currency_for_country("ZZ") is None
    assert cur.currency_for_country(None) is None


# ---------------- unit: rate caching ----------------

def test_rate_table_is_cached():
    """One table serves the whole app — not one lookup per product."""
    first = cur.get_rate_table()
    second = cur.get_rate_table()
    assert first is second


def test_rate_table_reports_indicative_source():
    table = cur.get_rate_table()
    assert table.source in ("static", "http")
    if table.source == "static":
        assert table.is_indicative is True


# ---------------- API ----------------

def _alive():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


api_only = pytest.mark.skipif(
    os.getenv("VERA_SKIP_API_TESTS") == "1", reason="API tests disabled"
)


@pytest.fixture(scope="module")
def live():
    if not _alive():
        pytest.skip(f"backend not reachable at {API}")
    return True


@api_only
def test_currencies_endpoint(live):
    d = requests.get(f"{API}/currency/currencies", timeout=10).json()
    assert len(d) >= 16
    assert {c["code"] for c in d} >= {"INR", "USD", "GBP", "JPY", "KWD"}


@api_only
def test_rates_endpoint_is_single_table(live):
    d = requests.get(f"{API}/currency/rates", timeout=10).json()
    assert d["base"] == "INR"
    assert d["rates"]["INR"] == 1
    assert d["cache_ttl"] > 0
    assert all(v > 0 for v in d["rates"].values())


@api_only
def test_detect_uses_edge_header(live):
    d = requests.get(f"{API}/currency/detect", headers={"CF-IPCountry": "GB"}, timeout=10).json()
    assert d["country"] == "GB" and d["currency"] == "GBP"


@api_only
def test_detect_without_header_falls_back(live):
    d = requests.get(f"{API}/currency/detect", timeout=10).json()
    assert d["fallback"] == "INR"
    assert d["currency"] is None or d["currency"] in cur.CURRENCIES


@api_only
def test_client_cannot_supply_an_exchange_rate(live):
    """The headline security requirement for this phase."""
    products = requests.get(f"{API}/products", timeout=10).json()
    p = next(x for x in products if x["variants"] and x["variants"][0]["stock"] > 0)
    v = p["variants"][0]
    r = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "FX", "customer_email": f"fx{uuid.uuid4().hex[:6]}@example.com",
        "display_currency": "USD",
        # all of these are noise the server must ignore
        "exchange_rate": 0.00001, "currency": "USD", "total": 1, "display_total": 1,
        "items": [{"product_id": p["id"], "variant_id": v["id"], "quantity": 1, "price": 1}],
    })
    assert r.status_code == 201
    order = r.json()
    assert order["currency"] == "INR"                       # settled in INR
    assert float(order["total"]) == float(v["price"])       # real price charged
    assert float(order["display_rate"]) != 0.00001          # our rate, not theirs
    assert order["display_currency"] == "USD"


@api_only
def test_unknown_display_currency_does_not_break_checkout(live):
    products = requests.get(f"{API}/products", timeout=10).json()
    p = next(x for x in products if x["variants"] and x["variants"][0]["stock"] > 0)
    r = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "FX", "customer_email": f"fx{uuid.uuid4().hex[:6]}@example.com",
        "display_currency": "ZZZ",
        "items": [{"product_id": p["id"], "variant_id": p["variants"][0]["id"], "quantity": 1}],
    })
    assert r.status_code == 201
    assert r.json()["currency"] == "INR"


@api_only
def test_historical_order_keeps_its_own_rate(live):
    """Old orders must not be re-converted at today's rate."""
    products = requests.get(f"{API}/products", timeout=10).json()
    p = next(x for x in products if x["variants"] and x["variants"][0]["stock"] > 0)
    order = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "FX", "customer_email": f"fx{uuid.uuid4().hex[:6]}@example.com",
        "display_currency": "GBP",
        "items": [{"product_id": p["id"], "variant_id": p["variants"][0]["id"], "quantity": 1}],
    }).json()
    assert order["display_currency"] == "GBP"
    stored_rate = order["display_rate"]
    assert stored_rate is not None and float(stored_rate) > 0

    tok = requests.post(f"{API}/auth/login", json=ADMIN, timeout=10).json()["access_token"]
    again = requests.get(f"{API}/orders", headers={"Authorization": f"Bearer {tok}"},
                         timeout=15).json()
    stored = next(o for o in again if o["order_number"] == order["order_number"])
    assert stored["display_rate"] == stored_rate
    assert stored["total"] == order["total"]
