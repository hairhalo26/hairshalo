"""Baseline: schema as it existed before the product-architecture hardening.

An existing database that predates Alembic can be marked as being at this
revision without re-running it:

    alembic stamp 0001_baseline

Revision ID: 0001_baseline
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

PLACEHOLDER_SCHEMA = "vera_product_placeholders"


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    product_status = sa.Enum("active", "draft", "archived", name="productstatus")
    order_status = sa.Enum(
        "processing", "shipped", "out_for_delivery", "delivered", "cancelled",
        name="orderstatus",
    )
    appointment_status = sa.Enum(
        "pending", "confirmed", "completed", "cancelled", name="appointmentstatus"
    )
    discount_type = sa.Enum("percent", "flat", "free_shipping", name="discounttype")

    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(), nullable=False),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("role", sa.String()),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "customers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False, unique=True),
        sa.Column("phone", sa.String()),
        sa.Column("loyalty_points", sa.Integer()),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_customers_email", "customers", ["email"])

    op.create_table(
        "products",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("compare_at_price", sa.Float()),
        sa.Column("image_url", sa.String()),
        sa.Column("rating", sa.Float()),
        sa.Column("review_count", sa.Integer()),
        sa.Column("status", product_status),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_products_slug", "products", ["slug"])

    op.create_table(
        "inventory_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("product_id", sa.String(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("sku", sa.String(), nullable=False, unique=True),
        sa.Column("variant", sa.String(), nullable=False),
        sa.Column("warehouse", sa.String()),
        sa.Column("units", sa.Integer()),
        sa.Column("low_stock_threshold", sa.Integer()),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("order_number", sa.String(), nullable=False, unique=True),
        sa.Column("customer_id", sa.String(), sa.ForeignKey("customers.id")),
        sa.Column("customer_name", sa.String(), nullable=False),
        sa.Column("customer_email", sa.String(), nullable=False),
        sa.Column("shipping_address", sa.Text()),
        sa.Column("total", sa.Float(), nullable=False),
        sa.Column("status", order_status),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "order_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("order_id", sa.String(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("product_id", sa.String(), sa.ForeignKey("products.id")),
        sa.Column("product_name", sa.String(), nullable=False),
        sa.Column("quantity", sa.Integer()),
        sa.Column("price", sa.Float(), nullable=False),
    )

    op.create_table(
        "appointments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("customer_name", sa.String(), nullable=False),
        sa.Column("customer_email", sa.String(), nullable=False),
        sa.Column("customer_phone", sa.String()),
        sa.Column("appointment_type", sa.String(), nullable=False),
        sa.Column("stylist", sa.String()),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("status", appointment_status),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "coupons",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("code", sa.String(), nullable=False, unique=True),
        sa.Column("description", sa.String()),
        sa.Column("discount_type", discount_type),
        sa.Column("discount_value", sa.Float()),
        sa.Column("usage_count", sa.Integer()),
        sa.Column("active", sa.Boolean()),
    )
    op.create_index("ix_coupons_code", "coupons", ["code"])

    # Placeholders live in their own schema, isolated from the real catalog.
    if is_pg:
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{PLACEHOLDER_SCHEMA}"')

    op.create_table(
        "product_placeholders",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("short_description", sa.Text()),
        sa.Column("placeholder_image", sa.String()),
        sa.Column("placeholder_label", sa.String()),
        sa.Column("display_price", sa.String()),
        sa.Column("badge", sa.String()),
        sa.Column("sort_order", sa.Integer()),
        sa.Column("is_visible", sa.Boolean()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        schema=PLACEHOLDER_SCHEMA if is_pg else None,
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.drop_table("product_placeholders", schema=PLACEHOLDER_SCHEMA if is_pg else None)
    if is_pg:
        op.execute(f'DROP SCHEMA IF EXISTS "{PLACEHOLDER_SCHEMA}" CASCADE')
    op.drop_table("coupons")
    op.drop_table("appointments")
    op.drop_table("order_items")
    op.drop_table("orders")
    op.drop_table("inventory_items")
    op.drop_table("products")
    op.drop_table("customers")
    op.drop_table("users")

    for name in ("productstatus", "orderstatus", "appointmentstatus", "discounttype"):
        sa.Enum(name=name).drop(bind, checkfirst=True)
