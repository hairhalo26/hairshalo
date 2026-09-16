"""Shipping address validation — the single place an address is judged valid.

The rule this file exists to enforce: **an address is validated on the server,
against the country it claims to be in.**

Frontend validation is a convenience for the person typing; it is not a
control. Anything can POST to `/api/orders`, so the checks that decide whether
an order is accepted have to live here. Both callers use this module — checkout
and the saved-address endpoints — so an address cannot be smuggled in through
the account area and then used at checkout without ever being checked.

Why per-country postal rules rather than one regex: "PIN code" is an Indian
term for a six-digit number, and applying that shape to a UK or Canadian
address rejects real addresses. A country we have no rule for is accepted with
a loose sanity check rather than refused, because inventing a format for a
country nobody here has verified would block real customers to no benefit.

What this module deliberately does NOT do:

* Look up whether an address exists. That needs a postal API; guessing would
  reject valid new-build addresses.
* Normalise or "correct" what the customer typed beyond trimming whitespace.
  A courier delivers to what the customer wrote, and silently rewriting it is
  how parcels go to the wrong place.
"""
import re
from typing import Dict, List, Optional

# Countries we ship to, with the local name for the postal field and its shape.
#
# `label` is what the storefront shows above the input — an Indian customer
# should see "PIN Code", an American "ZIP Code". `pattern` is checked
# server-side; `example` is only ever shown in an error message, to make the
# expected shape obvious instead of making the customer guess.
COUNTRY_RULES: Dict[str, dict] = {
    "India": {
        "code": "IN",
        # Six digits, and the first may not be 0 — no Indian PIN starts with 0.
        "pattern": r"^[1-9][0-9]{5}$",
        "label": "PIN Code",
        "example": "500001",
        "requires_state": True,
        "postal_required": True,
    },
    "United States": {
        "code": "US",
        "pattern": r"^[0-9]{5}(?:-[0-9]{4})?$",
        "label": "ZIP Code",
        "example": "94103 or 94103-1234",
        "requires_state": True,
        "postal_required": True,
    },
    "United Kingdom": {
        "code": "GB",
        "pattern": r"^[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2}$",
        "label": "Postcode",
        "example": "SW1A 1AA",
        "requires_state": False,
        "postal_required": True,
    },
    "Canada": {
        "code": "CA",
        # No D, F, I, O, Q or U in a Canadian postal code, and the first
        # character additionally excludes W and Z.
        "pattern": r"^[ABCEGHJ-NPRSTVXY][0-9][ABCEGHJ-NPRSTV-Z]\s*[0-9][ABCEGHJ-NPRSTV-Z][0-9]$",
        "label": "Postal Code",
        "example": "K1A 0B1",
        "requires_state": True,
        "postal_required": True,
    },
    "Australia": {
        "code": "AU",
        "pattern": r"^[0-9]{4}$",
        "label": "Postcode",
        "example": "2000",
        "requires_state": True,
        "postal_required": True,
    },
    "United Arab Emirates": {
        "code": "AE",
        # The UAE has no postal code system at all. Demanding one would be
        # asking for something that does not exist.
        "pattern": None,
        "label": "Postal Code (optional)",
        "example": "",
        "requires_state": False,
        "postal_required": False,
    },
    "Singapore": {
        "code": "SG",
        "pattern": r"^[0-9]{6}$",
        "label": "Postal Code",
        "example": "238823",
        "requires_state": False,
        "postal_required": True,
    },
    "Nigeria": {
        "code": "NG",
        "pattern": r"^[0-9]{6}$",
        "label": "Postal Code",
        "example": "100001",
        "requires_state": True,
        "postal_required": False,
    },
}

DEFAULT_COUNTRY = "India"

# Used for a country with no rule of its own: letters, digits, spaces and
# hyphens, 3-12 characters. Loose on purpose — see the module docstring.
GENERIC_POSTAL = r"^[A-Za-z0-9][A-Za-z0-9 \-]{1,10}[A-Za-z0-9]$"

GENERIC_RULE = {
    "code": None,
    "pattern": GENERIC_POSTAL,
    "label": "Postal Code",
    "example": "",
    "requires_state": False,
    "postal_required": False,
}

# A name must contain at least one letter. Digits-only or punctuation-only
# values are rejected; letters from any script are accepted, because a name is
# not required to be ASCII.
NAME_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

# Phone: optional +, then 7-15 digits once separators are stripped. The range
# is ITU-T E.164 — shorter cannot be dialled, longer is not a phone number.
PHONE_DIGITS = re.compile(r"[0-9]")
PHONE_ALLOWED = re.compile(r"^[+()\-.\s0-9]+$")

MAX_LEN = {
    "full_name": 120,
    "phone": 32,
    "line1": 200,
    "line2": 200,
    "city": 80,
    "state": 80,
    "postal_code": 16,
    "country": 60,
    "label": 40,
}


class AddressError(ValueError):
    """One or more fields were rejected.

    Carries `errors` as {field: message} so the storefront can mark the
    offending input rather than showing one generic failure for a form with
    eight fields in it.
    """

    def __init__(self, errors: Dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))


def known_countries() -> List[dict]:
    """The shipping destinations offered, for the storefront's country select.

    Returned rather than hardcoded in the frontend so the field label switches
    to "PIN Code" or "ZIP Code" from one source of truth.
    """
    return [
        {
            "name": name,
            "code": rule["code"],
            "postal_label": rule["label"],
            "postal_example": rule["example"],
            "postal_required": rule["postal_required"],
            "requires_state": rule["requires_state"],
        }
        for name, rule in COUNTRY_RULES.items()
    ]


def rule_for(country: Optional[str]) -> dict:
    if not country:
        return COUNTRY_RULES[DEFAULT_COUNTRY]
    country = country.strip()
    for name, rule in COUNTRY_RULES.items():
        if name.lower() == country.lower() or (rule["code"] or "").lower() == country.lower():
            return rule
    return GENERIC_RULE


def postal_label_for(country: Optional[str]) -> str:
    return rule_for(country)["label"]


def _clean(value: Optional[str]) -> str:
    """Trim, and collapse internal runs of whitespace to one space.

    Collapsing matters for `line1`: a pasted address often carries a newline or
    a double space, and that is not a difference a courier cares about.
    """
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalise_country(country: Optional[str]) -> str:
    """Resolve a country name or ISO code to the canonical stored name."""
    if not country:
        return DEFAULT_COUNTRY
    country = country.strip()
    for name, rule in COUNTRY_RULES.items():
        if name.lower() == country.lower() or (rule["code"] or "").lower() == country.lower():
            return name
    return country


def validate(data: dict, *, require_phone: bool = True) -> dict:
    """Validate and clean one address. Returns the cleaned fields.

    Raises `AddressError` with a per-field map when anything is wrong. Every
    check here is the authoritative one — nothing calls this "after" the
    frontend has already approved, because the frontend's approval is not
    evidence of anything.

    `require_phone` is False for a saved address book entry, where a customer
    may reasonably keep an address they have not attached a number to yet. It
    is True at checkout, because a courier that cannot call cannot deliver.
    """
    errors: Dict[str, str] = {}

    country = normalise_country(_clean(data.get("country")) or DEFAULT_COUNTRY)
    rule = rule_for(country)

    full_name = _clean(data.get("full_name"))
    if not full_name:
        errors["full_name"] = "Enter the recipient's full name."
    elif len(full_name) < 2:
        errors["full_name"] = "That name is too short."
    elif not NAME_HAS_LETTER.search(full_name):
        errors["full_name"] = "A name must contain at least one letter."
    elif len(full_name) > MAX_LEN["full_name"]:
        errors["full_name"] = f"Keep the name under {MAX_LEN['full_name']} characters."

    # Phone keeps the customer's own formatting; only the digit COUNT is
    # checked. Rewriting "+91 98765 43210" into a canonical form would lose
    # information the customer may have meant (an extension, a country prefix
    # they rely on).
    phone = _clean(data.get("phone"))
    if not phone:
        if require_phone:
            errors["phone"] = "Enter a phone number the courier can call."
    elif len(phone) > MAX_LEN["phone"]:
        errors["phone"] = "That phone number is too long."
    elif not PHONE_ALLOWED.match(phone):
        errors["phone"] = "A phone number may contain only digits, spaces and + ( ) - ."
    else:
        digits = PHONE_DIGITS.findall(phone)
        if len(digits) < 7:
            errors["phone"] = "That phone number has too few digits."
        elif len(digits) > 15:
            errors["phone"] = "That phone number has too many digits."
        elif rule["code"] == "IN":
            # Indian mobile numbers are 10 digits starting 6-9, optionally
            # with a 91 country prefix. Landlines are also 10 digits with an
            # STD code, so only the clearly-wrong lengths are refused.
            local = "".join(digits)
            if local.startswith("91") and len(local) == 12:
                local = local[2:]
            elif local.startswith("0") and len(local) == 11:
                local = local[1:]
            if len(local) != 10:
                errors["phone"] = "An Indian phone number should be 10 digits."

    line1 = _clean(data.get("line1"))
    if not line1:
        errors["line1"] = "Enter the street address."
    elif len(line1) < 4:
        errors["line1"] = "That address line is too short."
    elif len(line1) > MAX_LEN["line1"]:
        errors["line1"] = f"Keep this line under {MAX_LEN['line1']} characters."

    line2 = _clean(data.get("line2"))
    if len(line2) > MAX_LEN["line2"]:
        errors["line2"] = f"Keep this line under {MAX_LEN['line2']} characters."

    city = _clean(data.get("city"))
    if not city:
        errors["city"] = "Enter the town or city."
    elif not NAME_HAS_LETTER.search(city):
        errors["city"] = "That city name is not valid."
    elif len(city) > MAX_LEN["city"]:
        errors["city"] = f"Keep the city under {MAX_LEN['city']} characters."

    state = _clean(data.get("state"))
    if rule["requires_state"] and not state:
        errors["state"] = "Enter the state or province."
    elif len(state) > MAX_LEN["state"]:
        errors["state"] = f"Keep the state under {MAX_LEN['state']} characters."

    postal = _clean(data.get("postal_code")).upper()
    label = rule["label"]
    if not postal:
        if rule["postal_required"]:
            errors["postal_code"] = f"Enter the {label.replace(' (optional)', '')}."
    elif len(postal) > MAX_LEN["postal_code"]:
        errors["postal_code"] = f"That {label} is too long."
    elif rule["pattern"] and not re.match(rule["pattern"], postal):
        example = f" Example: {rule['example']}." if rule["example"] else ""
        errors["postal_code"] = f"That is not a valid {label}.{example}"

    if len(country) > MAX_LEN["country"]:
        errors["country"] = "That country name is too long."

    if errors:
        raise AddressError(errors)

    return {
        "full_name": full_name,
        "phone": phone or None,
        "line1": line1,
        "line2": line2 or None,
        "city": city,
        "state": state or None,
        # Stored upper-cased: postal codes are case-insensitive, and a courier
        # label reads better in caps. This is the one normalisation applied,
        # and it cannot change which address is meant.
        "postal_code": postal or None,
        "country": country,
    }


def as_text(address: dict) -> str:
    """Render a validated address as the multi-line blob `Order.shipping_address` holds.

    Both forms are written for a new order: the structured columns for label
    printing and filtering, and this text for everything that already reads the
    old column (the admin table, the order-confirmation email template).
    """
    middle = ", ".join(
        p for p in [address.get("city"), address.get("state"), address.get("postal_code")] if p
    )
    parts = [
        address.get("full_name"),
        address.get("phone"),
        address.get("line1"),
        address.get("line2"),
        middle,
        address.get("country"),
    ]
    return "\n".join(p for p in parts if p)


def from_saved(row) -> dict:
    """Copy a `CustomerAddress` row into the plain dict shape used above."""
    return {
        "full_name": row.full_name,
        "phone": row.phone,
        "line1": row.line1,
        "line2": row.line2,
        "city": row.city,
        "state": row.state,
        "postal_code": row.postal_code,
        "country": row.country,
    }


def snapshot_onto_order(order, address: dict) -> None:
    """Write a validated address onto an order as an immutable snapshot.

    Called once, at creation. Nothing updates these columns afterwards: the
    address an order was placed with is a historical fact, and the point of a
    snapshot is that later edits to the customer's address book cannot reach
    back and change it.
    """
    order.shipping_name = address.get("full_name")
    order.shipping_phone = address.get("phone")
    order.shipping_line1 = address.get("line1")
    order.shipping_line2 = address.get("line2")
    order.shipping_city = address.get("city")
    order.shipping_state = address.get("state")
    order.shipping_postal_code = address.get("postal_code")
    order.shipping_country = address.get("country")
    order.shipping_address = as_text(address)
