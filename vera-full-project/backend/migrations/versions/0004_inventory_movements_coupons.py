"""Inventory movement audit trail, coupon redemption rules, order shipping.

ProductVariant.stock becomes the single authoritative quantity;
`inventory_movements` is an append-only log explaining every change to it.
`inventory_items` is retained as a per-warehouse breakdown but is no longer
consulted to decide whether something can be sold.

Existing stock is backfilled as a single `initial` movement per variant, so the
log reconciles with the running total from day one.

Revision ID: 0004_inventory_movements_coupons
Revises: 0003_pricing_media_attributes
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_inventory_movements_coupons"
down_revision = "0003_pricing_media_attributes"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(12, 2)
REASONS = ("initial", "order", "cancellation", "refund", "restock",
           "adjustment", "damaged", "correction")


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute(
            "CREATE TYPE movementreason AS ENUM "
            "('initial','order','cancellation','refund','restock','adjustment','damaged','correction')"
        )
        reason_type = postgresql.ENUM(*REASONS, name="movementreason", create_type=False)
    else:
        reason_type = sa.Enum(*REASONS, name="movementreason")

    # ---------- inventory movements ----------
    op.create_table(
        "inventory_movements",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("variant_id", sa.String(),
                  sa.ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("stock_after", sa.Integer(), nullable=False),
        sa.Column("reason", reason_type, nullable=False),
        sa.Column("reference_type", sa.String(), nullable=True),
        sa.Column("reference_id", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_inventory_movements_variant_id", "inventory_movements", ["variant_id"])
    op.create_index("ix_inventory_movements_reason", "inventory_movements", ["reason"])
    op.create_index("ix_inventory_movements_created_at", "inventory_movements", ["created_at"])

    # ---------- coupon redemption rules ----------
    op.add_column("coupons", sa.Column("min_order_amount", MONEY, nullable=True))
    op.add_column("coupons", sa.Column("expires_at", sa.DateTime(), nullable=True))
    op.add_column("coupons", sa.Column("usage_limit", sa.Integer(), nullable=True))

    # ---------- order shipping + coupon snapshot ----------
    op.add_column("orders", sa.Column("shipping_fee", MONEY, nullable=True))
    op.add_column("orders", sa.Column("coupon_code", sa.String(), nullable=True))

    conn = op.get_bind()
    conn.execute(sa.text("UPDATE orders SET shipping_fee = COALESCE(shipping_fee, 0)"))

    # ---------- backfill the movement log from current stock ----------
    conn.execute(sa.text(
        "INSERT INTO inventory_movements "
        "(id, variant_id, delta, stock_after, reason, note, created_at) "
        "SELECT md5(random()::text || clock_timestamp()::text), id, stock, stock, "
        "'initial', 'Backfilled from stock at migration 0004', NOW() "
        "FROM product_variants WHERE stock IS NOT NULL AND stock <> 0"
    ) if is_pg else sa.text("SELECT 1"))


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.drop_column("orders", "coupon_code")
    op.drop_column("orders", "shipping_fee")
    op.drop_column("coupons", "usage_limit")
    op.drop_column("coupons", "expires_at")
    op.drop_column("coupons", "min_order_amount")

    op.drop_index("ix_inventory_movements_created_at", table_name="inventory_movements")
    op.drop_index("ix_inventory_movements_reason", table_name="inventory_movements")
    op.drop_index("ix_inventory_movements_variant_id", table_name="inventory_movements")
    op.drop_table("inventory_movements")

    if is_pg:
        op.execute("DROP TYPE IF EXISTS movementreason")
