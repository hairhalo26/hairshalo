"""Order fulfilment details, order timeline, coupon rules, subcategories.

Every change is additive: new nullable columns, one new table, no rewrite of
any existing row except a back-filled boolean. Nothing is dropped.

1. `orders` gains what fulfilment needs and did not have:
   * `tracking_number`, `carrier`, `tracking_url` — set by staff when a parcel
     ships; shown to the customer.
   * `internal_notes` — staff-only. Never serialised to a customer endpoint.
   * `idempotency_key` — a checkout attempt's own id, unique, so a
     double-clicked "Place Order" or a retried request returns the order that
     already exists instead of creating (and charging stock for) a second one.
     Nullable, because every existing order predates it.
   * `placed_signed_in` — whether the order was placed by a signed-in
     account. An unverified account may see THOSE orders (it placed them),
     never orders that merely share its email address. Existing orders are
     FALSE: nobody can now prove they were placed while signed in.

2. `order_events` — the order's timeline. One row per status change, with
   who made it. Existing orders have no rows; the API synthesises a minimal
   timeline for them from `created_at` and the current status rather than
   inventing history.

3. `coupons` gains `max_discount_amount` (cap on a percentage discount),
   `starts_at` (not valid before) and `per_customer_limit`. All nullable,
   and NULL means "no rule", so existing coupons behave exactly as before.

4. `categories` gains `parent_id`, so a category can be a subcategory.
   NULL means top level — which is what every existing category is.

5. `product_variants` gains `low_stock_threshold`. NULL means the shop-wide
   default (LOW_STOCK_ALERT_THRESHOLD), so nothing changes until staff set one.

Revision ID: 0013_orders_coupons_cats
Revises: 0012_shipping_and_content
"""
from alembic import op
import sqlalchemy as sa

# Kept under 32 characters: alembic_version.version_num is VARCHAR(32).
revision = "0013_orders_coupons_cats"
down_revision = "0012_shipping_and_content"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(12, 2)


def upgrade() -> None:
    # --- 1. orders ---------------------------------------------------------
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("tracking_number", sa.String(), nullable=True))
        batch.add_column(sa.Column("carrier", sa.String(), nullable=True))
        batch.add_column(sa.Column("tracking_url", sa.String(), nullable=True))
        batch.add_column(sa.Column("internal_notes", sa.Text(), nullable=True))
        batch.add_column(sa.Column("idempotency_key", sa.String(), nullable=True))
        batch.add_column(sa.Column("placed_signed_in", sa.Boolean(),
                                   server_default=sa.false(), nullable=False))
    # Unique among non-NULL values in PostgreSQL, so the many existing NULLs
    # never collide with each other.
    op.create_index("ix_orders_idempotency_key", "orders", ["idempotency_key"],
                    unique=True)

    # --- 2. order_events -------------------------------------------------
    op.create_table(
        "order_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("order_id", sa.String(),
                  sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_order_events_order_id", "order_events", ["order_id"])

    # --- 3. coupons ----------------------------------------------------------
    with op.batch_alter_table("coupons") as batch:
        batch.add_column(sa.Column("max_discount_amount", MONEY, nullable=True))
        batch.add_column(sa.Column("starts_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("per_customer_limit", sa.Integer(), nullable=True))

    # --- 4. categories -----------------------------------------------------
    with op.batch_alter_table("categories") as batch:
        batch.add_column(sa.Column("parent_id", sa.String(), nullable=True))
        batch.create_foreign_key("fk_categories_parent_id", "categories",
                                 ["parent_id"], ["id"], ondelete="SET NULL")

    # --- 5. per-variant low-stock threshold ---------------------------------
    # NULL = the shop default, which is what every existing variant gets.
    with op.batch_alter_table("product_variants") as batch:
        batch.add_column(sa.Column("low_stock_threshold", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("product_variants") as batch:
        batch.drop_column("low_stock_threshold")
    with op.batch_alter_table("categories") as batch:
        batch.drop_constraint("fk_categories_parent_id", type_="foreignkey")
        batch.drop_column("parent_id")
    with op.batch_alter_table("coupons") as batch:
        batch.drop_column("per_customer_limit")
        batch.drop_column("starts_at")
        batch.drop_column("max_discount_amount")
    op.drop_index("ix_order_events_order_id", table_name="order_events")
    op.drop_table("order_events")
    op.drop_index("ix_orders_idempotency_key", table_name="orders")
    with op.batch_alter_table("orders") as batch:
        for name in ("placed_signed_in", "idempotency_key", "internal_notes",
                     "tracking_url", "carrier", "tracking_number"):
            batch.drop_column(name)
