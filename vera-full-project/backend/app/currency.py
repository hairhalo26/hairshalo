"""Currency service — the single place conversion happens.

Design rules this file exists to enforce:

  * INR is canonical. Every price in the database is INR; nothing else is
    ever stored as a product price.
  * Converted prices are NEVER written to product rows. They are computed on
    read, from one cached rate table — not one API call per product.
  * The client never supplies a currency rate. It may ask for a display
    currency by code; the server decides the rate.
  * Display currency and payment currency are different things. Conversion
    here is for presentation only; charging happens in INR.
  * Rate resolution falls back: live provider -> cached rates -> static table
    -> INR only. It never yields NaN, None or a zero rate.

Configuration (see app/config.py):
    BASE_CURRENCY, EXCHANGE_RATE_PROVIDER, EXCHANGE_RATE_API_KEY,
    EXCHANGE_RATE_CACHE_TTL
"""
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional, Tuple

from app.config import settings

BASE = "INR"


class Currency:
    __slots__ = ("code", "symbol", "name", "decimals", "symbol_first")

    def __init__(self, code, symbol, name, decimals=2, symbol_first=True):
        self.code, self.symbol, self.name = code, symbol, name
        self.decimals, self.symbol_first = decimals, symbol_first


# Supported display currencies. `decimals` matters: JPY has none, KWD has three.
CURRENCIES: Dict[str, Currency] = {
    "INR": Currency("INR", "₹", "Indian Rupee"),
    "USD": Currency("USD", "$", "US Dollar"),
    "GBP": Currency("GBP", "£", "Pound Sterling"),
    "EUR": Currency("EUR", "€", "Euro"),
    "AED": Currency("AED", "د.إ", "UAE Dirham"),
    "SGD": Currency("SGD", "S$", "Singapore Dollar"),
    "MYR": Currency("MYR", "RM", "Malaysian Ringgit"),
    "LKR": Currency("LKR", "Rs", "Sri Lankan Rupee"),
    "AUD": Currency("AUD", "A$", "Australian Dollar"),
    "CAD": Currency("CAD", "C$", "Canadian Dollar"),
    "NZD": Currency("NZD", "NZ$", "New Zealand Dollar"),
    "JPY": Currency("JPY", "¥", "Japanese Yen", decimals=0),
    "SAR": Currency("SAR", "SR", "Saudi Riyal"),
    "QAR": Currency("QAR", "QR", "Qatari Riyal"),
    "KWD": Currency("KWD", "KD", "Kuwaiti Dinar", decimals=3),
    "CHF": Currency("CHF", "CHF", "Swiss Franc"),
}

# ISO-3166 alpha-2 -> display currency.
COUNTRY_CURRENCY: Dict[str, str] = {
    "IN": "INR",
    "GB": "GBP", "UK": "GBP",
    "US": "USD",
    "AE": "AED", "SG": "SGD", "MY": "MYR", "LK": "LKR",
    "AU": "AUD", "CA": "CAD", "NZ": "NZD", "JP": "JPY",
    "SA": "SAR", "QA": "QAR", "KW": "KWD", "CH": "CHF",
    # Eurozone
    "AT": "EUR", "BE": "EUR", "CY": "EUR", "DE": "EUR", "EE": "EUR",
    "ES": "EUR", "FI": "EUR", "FR": "EUR", "GR": "EUR", "HR": "EUR",
    "IE": "EUR", "IT": "EUR", "LT": "EUR", "LU": "EUR", "LV": "EUR",
    "MT": "EUR", "NL": "EUR", "PT": "EUR", "SI": "EUR", "SK": "EUR",
}

# Offline fallback: units of each currency per 1 INR.
#
# These are INDICATIVE ONLY and are the last resort before INR-only display.
# Configure a live provider for real rates; the UI labels prices as indicative
# whenever these are in use.
STATIC_RATES: Dict[str, Decimal] = {
    "INR": Decimal("1"),
    "USD": Decimal("0.01200"), "GBP": Decimal("0.00940"), "EUR": Decimal("0.01110"),
    "AED": Decimal("0.04410"), "SGD": Decimal("0.01550"), "MYR": Decimal("0.05330"),
    "LKR": Decimal("3.55000"), "AUD": Decimal("0.01840"), "CAD": Decimal("0.01640"),
    "NZD": Decimal("0.02010"), "JPY": Decimal("1.78000"), "SAR": Decimal("0.04500"),
    "QAR": Decimal("0.04370"), "KWD": Decimal("0.00368"), "CHF": Decimal("0.00960"),
}
STATIC_RATES_AS_OF = "2026-08-01"


class RateTable:
    """A resolved set of rates plus where they came from."""

    def __init__(self, rates: Dict[str, Decimal], source: str, fetched_at: float):
        self.rates, self.source, self.fetched_at = rates, source, fetched_at

    @property
    def age_seconds(self) -> int:
        return int(time.time() - self.fetched_at)

    @property
    def is_indicative(self) -> bool:
        return self.source == "static"


# ---------------- providers ----------------

class ExchangeRateProvider:
    """Implement fetch() to plug in a live rate feed."""
    name = "base"

    def fetch(self) -> Optional[Dict[str, Decimal]]:
        raise NotImplementedError


class StaticProvider(ExchangeRateProvider):
    name = "static"

    def fetch(self):
        return dict(STATIC_RATES)


class HttpRateProvider(ExchangeRateProvider):
    """Generic JSON provider, e.g. exchangerate-api / openexchangerates.

    Expects a payload with a `rates` object keyed by currency code, quoted
    against BASE_CURRENCY. Requires EXCHANGE_RATE_API_KEY to be set.
    """
    name = "http"

    def __init__(self, url_template: str, api_key: str):
        self.url_template, self.api_key = url_template, api_key

    def fetch(self):
        if not self.api_key:
            return None
        try:
            import json
            import urllib.request
            url = self.url_template.format(key=self.api_key, base=BASE)
            with urllib.request.urlopen(url, timeout=6) as resp:
                payload = json.loads(resp.read().decode())
            raw = payload.get("rates") or payload.get("conversion_rates") or {}
            rates = {}
            for code in CURRENCIES:
                if code in raw:
                    try:
                        rates[code] = Decimal(str(raw[code]))
                    except Exception:
                        continue
            rates[BASE] = Decimal("1")
            # A payload missing most currencies is not usable.
            return rates if len(rates) >= 4 else None
        except Exception:
            return None


def _build_provider() -> ExchangeRateProvider:
    name = (settings.EXCHANGE_RATE_PROVIDER or "static").lower()
    if name in ("static", "", "none"):
        return StaticProvider()
    return HttpRateProvider(settings.EXCHANGE_RATE_URL, settings.EXCHANGE_RATE_API_KEY)


_provider = None
_cache: Optional[RateTable] = None


def get_rate_table(force: bool = False) -> RateTable:
    """Cached rate table. One fetch per TTL for the whole application."""
    global _provider, _cache
    if _provider is None:
        _provider = _build_provider()

    ttl = int(settings.EXCHANGE_RATE_CACHE_TTL or 3600)
    if _cache and not force and _cache.age_seconds < ttl:
        return _cache

    fetched = None
    try:
        fetched = _provider.fetch()
    except Exception:
        fetched = None

    if fetched:
        _cache = RateTable(fetched, _provider.name, time.time())
    elif _cache:
        # Live fetch failed — keep serving the last good rates rather than
        # falling all the way back and changing prices under the customer.
        _cache.fetched_at = time.time() - int(ttl * 0.9)
    else:
        _cache = RateTable(dict(STATIC_RATES), "static", time.time())
    return _cache


def rate_for(code: str) -> Tuple[Decimal, str]:
    """(rate, source). Always returns a usable positive rate."""
    code = (code or BASE).upper()
    if code == BASE or code not in CURRENCIES:
        return Decimal("1"), BASE
    table = get_rate_table()
    rate = table.rates.get(code) or STATIC_RATES.get(code)
    if not rate or rate <= 0:
        return Decimal("1"), BASE     # never NaN, never zero
    return rate, table.source


def quantize_for(amount: Decimal, code: str) -> Decimal:
    cur = CURRENCIES.get((code or BASE).upper(), CURRENCIES[BASE])
    exp = Decimal(1).scaleb(-cur.decimals)
    return Decimal(amount).quantize(exp, rounding=ROUND_HALF_UP)


def convert(amount_inr, code: str) -> Decimal:
    """Convert a canonical INR amount into a display currency."""
    if amount_inr is None:
        return None
    code = (code or BASE).upper()
    rate, _src = rate_for(code)
    return quantize_for(Decimal(str(amount_inr)) * rate, code)


def format_amount(amount, code: str) -> str:
    """Server-side formatting, used for emails/receipts."""
    code = (code or BASE).upper()
    cur = CURRENCIES.get(code, CURRENCIES[BASE])
    value = quantize_for(Decimal(str(amount or 0)), code)
    text = f"{value:,.{cur.decimals}f}"
    return f"{cur.symbol}{text}" if cur.symbol_first else f"{text} {cur.symbol}"


def currency_for_country(country: str) -> Optional[str]:
    if not country:
        return None
    return COUNTRY_CURRENCY.get(country.strip().upper())


def resolve_currency(requested: Optional[str]) -> str:
    """Normalise a client-requested currency code. Unknown codes fall back to
    INR rather than erroring — a bad code must never break a price."""
    code = (requested or "").strip().upper()
    return code if code in CURRENCIES else BASE
