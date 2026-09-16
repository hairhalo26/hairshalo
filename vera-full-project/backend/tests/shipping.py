"""A valid delivery address, shared by every test that places an order.

`POST /api/orders` requires a structured, server-validated address: an order
with no address is an order nobody can ship, so accepting one would mean the
API cheerfully taking money for a parcel with nowhere to go.

Kept in one place so the shape of a valid address is stated once. Tests that
are ABOUT address validation build their own broken variants from
`valid_shipping(**overrides)` rather than copying this dict.
"""

SHIPPING = {
    "full_name": "Priya Sharma",
    "phone": "+91 98765 43210",
    "line1": "12 MG Road",
    "line2": "Flat 4B",
    "city": "Hyderabad",
    "state": "Telangana",
    "country": "India",
    "postal_code": "500001",
}


def valid_shipping(**overrides):
    """A valid address, with any field replaced. `None` removes the field."""
    out = dict(SHIPPING)
    out.update(overrides)
    return {k: v for k, v in out.items() if v is not None}
