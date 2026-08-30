"""Record the display currency and rate an order was placed under.

INR remains the settlement and accounting currency. These columns exist so a
receipt can be reproduced exactly as the customer saw it, without ever
re-converting a historical order at today's rate.

Revision ID: 0005_order_currency_snapshot
Revises: 0004_inventory_movements_coupons
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_order_currency_snapshot"
down_revision = "0004_inventory_movements_coupons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("currency", sa.String(), nullable=False,
                                      server_default="INR"))
    op.add_column("orders", sa.Column("display_currency", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("display_rate", sa.Numeric(18, 8), nullable=True))
    op.add_column("orders", sa.Column("display_total", sa.Numeric(18, 4), nullable=True))

    # Existing orders were placed in INR with no separate display currency.
    op.get_bind().execute(sa.text(
        "UPDATE orders SET currency = 'INR' WHERE currency IS NULL"
    ))


def downgrade() -> None:
    op.drop_column("orders", "display_total")
    op.drop_column("orders", "display_rate")
    op.drop_column("orders", "display_currency")
    op.drop_column("orders", "currency")
