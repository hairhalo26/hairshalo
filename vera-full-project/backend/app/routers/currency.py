"""Currency endpoints — supported currencies, rates, and country detection.

Rates are served from one cached table, so a storefront rendering 50 products
makes zero extra rate calls.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request

from app import currency as cur, schemas
from app.deps import get_optional_customer

router = APIRouter(prefix="/api/currency", tags=["currency"])


@router.get("/currencies", response_model=List[schemas.CurrencyOut])
def list_currencies():
    return [
        schemas.CurrencyOut(
            code=c.code, symbol=c.symbol, name=c.name, decimals=c.decimals,
        )
        for c in cur.CURRENCIES.values()
    ]


@router.get("/rates", response_model=schemas.RatesOut)
def get_rates(base: str = Query(cur.BASE, description="Informational; INR is canonical")):
    """All display rates in one call, quoted against INR."""
    table = cur.get_rate_table()
    return schemas.RatesOut(
        base=cur.BASE,
        rates={code: float(table.rates.get(code, cur.STATIC_RATES.get(code, 1)))
               for code in cur.CURRENCIES},
        source=table.source,
        age_seconds=table.age_seconds,
        is_indicative=table.is_indicative,
        as_of=cur.STATIC_RATES_AS_OF if table.is_indicative else None,
        cache_ttl=int(cur.settings.EXCHANGE_RATE_CACHE_TTL or 3600),
    )


@router.get("/detect", response_model=schemas.CurrencyDetectOut)
def detect_currency(request: Request, customer=Depends(get_optional_customer)):
    """Which currency to show, in the documented order of priority.

    1. Manual selection — the storefront keeps that locally and never asks.
    2. **A signed-in customer's saved preference** (handled here).
    3. Browser locale — the storefront reads that itself.
    4. Edge-header country (this endpoint).
    5. INR.

    Best-effort country detection from edge headers.

    Deliberately does NOT call a third-party IP service: that would add a
    per-visitor network round trip and hand a visitor's IP to another vendor.
    When the app sits behind a CDN that supplies a country header
    (Cloudflare's CF-IPCountry, or X-Country-Code from your proxy) it is used;
    otherwise this returns nothing and the browser falls back to its locale.
    """
    # Priority 2: a signed-in customer who has chosen a currency gets it
    # everywhere, on every device — that is the point of saving it.
    if customer is not None and customer.preferred_currency:
        return schemas.CurrencyDetectOut(
            country=None,
            currency=cur.resolve_currency(customer.preferred_currency),
            source="saved-preference",
            fallback=cur.BASE,
        )

    header_country = (
        request.headers.get("cf-ipcountry")
        or request.headers.get("x-country-code")
        or request.headers.get("x-vercel-ip-country")
    )
    country = None
    if header_country and header_country.upper() not in ("XX", "T1"):
        country = header_country.upper()

    detected = cur.currency_for_country(country) if country else None
    return schemas.CurrencyDetectOut(
        country=country,
        currency=detected,
        source="edge-header" if detected else "unavailable",
        fallback=cur.BASE,
    )


@router.get("/convert", response_model=schemas.ConvertOut)
def convert_amount(amount: float, to: str):
    """Convert a canonical INR amount. Used for spot checks and receipts."""
    code = cur.resolve_currency(to)
    rate, source = cur.rate_for(code)
    value = cur.convert(amount, code)
    return schemas.ConvertOut(
        amount_inr=amount, currency=code, rate=float(rate),
        converted=float(value), formatted=cur.format_amount(value, code),
        source=source,
    )
