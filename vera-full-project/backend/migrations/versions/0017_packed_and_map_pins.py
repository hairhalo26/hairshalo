"""A Packed order status, and optional map pins on addresses.

Additive only. No existing row is rewritten:

1. OrderStatus gains `packed` — boxed and labelled, not yet with the courier.
   Existing orders keep their status. Staff may record tracking from Packed
   onwards; the customer sees it from Shipped onwards (app/models.py).

2. `orders.shipping_latitude/longitude/place_id/formatted_address` — the pin
   the customer chose with the Google Maps picker at checkout, snapshotted
   with the rest of the delivery address. NULL for every existing order.

3. The same four columns on `customer_addresses`, so a saved address keeps
   its pin. NULL for every existing address.

Revision ID: 0017_packed_and_map_pins
Revises: 0016_hair_colours
"""
from alembic import op
import sqlalchemy as sa

# Kept under 32 characters: alembic_version.version_num is VARCHAR(32).
revision = "0017_packed_and_map_pins"
down_revision = "0016_hair_colours"
branch_labels = None
depends_on = None

PIN_COLUMNS = (
    ("latitude", sa.Numeric(10, 7)),
    ("longitude", sa.Numeric(10, 7)),
    ("place_id", sa.String()),
    ("formatted_address", sa.String()),
)


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # SQLAlchemy stores the enum NAME, so the new label is 'packed'.
        # Not used in this transaction, which Postgres 12+ allows.
        op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'packed' AFTER 'processing'")

    for name, type_ in PIN_COLUMNS:
        op.add_column("orders", sa.Column(f"shipping_{name}", type_, nullable=True))
        op.add_column("customer_addresses", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _type in PIN_COLUMNS:
        op.drop_column("customer_addresses", name)
        op.drop_column("orders", f"shipping_{name}")
    # Postgres cannot drop an enum value. 'packed' stays defined but unused;
    # move any Packed orders back to Processing before downgrading the code.
