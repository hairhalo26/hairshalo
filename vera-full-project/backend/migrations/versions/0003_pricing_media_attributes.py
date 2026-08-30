"""Discount pricing, Decimal money, product attributes, media metadata.

Money columns move from double precision to NUMERIC(12,2) so currency
arithmetic is exact. Existing values are cast in place.

Pricing is expressed with the columns that already exist — `price` (selling)
and `compare_at_price` (original) — plus how the markdown was derived
(`discount_type`, `discount_value`). No `selling_price`/`final_price` column is
introduced, to avoid duplicate representations of the same number.

Revision ID: 0003_pricing_media_attributes
Revises: 0002_product_architecture
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_pricing_media_attributes"
down_revision = "0002_product_architecture"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(12, 2)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("CREATE TYPE discountkind AS ENUM ('none','percentage','fixed_amount')")
        discount_kind = postgresql.ENUM(
            "none", "percentage", "fixed_amount", name="discountkind", create_type=False
        )
    else:
        discount_kind = sa.Enum("none", "percentage", "fixed_amount", name="discountkind")

    # ---------- Money columns -> NUMERIC(12,2) ----------
    for table, col in [
        ("products", "price"),
        ("products", "compare_at_price"),
        ("product_variants", "price"),
        ("orders", "total"),
        ("order_items", "price"),
        ("coupons", "discount_value"),
    ]:
        op.alter_column(
            table, col,
            existing_type=sa.Float(),
            type_=MONEY,
            existing_nullable=True,
            postgresql_using=f"{col}::numeric(12,2)" if is_pg else None,
        )

    # ---------- Product: discount + attributes ----------
    op.add_column("products", sa.Column("discount_type", discount_kind,
                                        nullable=False, server_default="none"))
    op.add_column("products", sa.Column("discount_value", MONEY,
                                        nullable=False, server_default="0"))
    op.add_column("products", sa.Column("short_description", sa.String(), server_default=""))
    op.add_column("products", sa.Column("brand", sa.String(), nullable=True))
    op.add_column("products", sa.Column("hair_type", sa.String(), nullable=True))
    op.add_column("products", sa.Column("construction", sa.String(), nullable=True))

    # ---------- Variant: own pricing ----------
    op.add_column("product_variants", sa.Column("compare_at_price", MONEY, nullable=True))
    op.add_column("product_variants", sa.Column("discount_type", discount_kind,
                                                nullable=False, server_default="none"))
    op.add_column("product_variants", sa.Column("discount_value", MONEY,
                                                nullable=False, server_default="0"))

    # ---------- Media: upload metadata ----------
    op.add_column("product_media", sa.Column("storage_key", sa.String(), nullable=True))
    op.add_column("product_media", sa.Column("content_type", sa.String(), nullable=True))
    op.add_column("product_media", sa.Column("file_size", sa.Integer(), nullable=True))

    # ---------- Orders: subtotal / discount breakdown ----------
    op.add_column("orders", sa.Column("subtotal", MONEY, nullable=True))
    op.add_column("orders", sa.Column("discount_total", MONEY, nullable=True))
    op.add_column("order_items", sa.Column("compare_at_price", MONEY, nullable=True))

    conn = op.get_bind()
    # Backfill order breakdown from what is already known.
    conn.execute(sa.text(
        "UPDATE orders SET subtotal = COALESCE(subtotal, total), "
        "discount_total = COALESCE(discount_total, 0)"
    ))

    # Existing rows that already carried a compare_at_price were discounted by
    # an implicit fixed amount; record that explicitly so the pricing engine and
    # the stored data agree from here on.
    conn.execute(sa.text(
        "UPDATE products SET discount_type = 'fixed_amount', "
        "discount_value = (compare_at_price - price) "
        "WHERE compare_at_price IS NOT NULL AND compare_at_price > price"
    ))
    # A compare_at_price that is not a genuine markdown is meaningless — clear
    # it so the storefront cannot render a fake sale.
    conn.execute(sa.text(
        "UPDATE products SET compare_at_price = NULL "
        "WHERE compare_at_price IS NOT NULL AND compare_at_price <= price"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.drop_column("order_items", "compare_at_price")
    op.drop_column("orders", "discount_total")
    op.drop_column("orders", "subtotal")

    op.drop_column("product_media", "file_size")
    op.drop_column("product_media", "content_type")
    op.drop_column("product_media", "storage_key")

    op.drop_column("product_variants", "discount_value")
    op.drop_column("product_variants", "discount_type")
    op.drop_column("product_variants", "compare_at_price")

    op.drop_column("products", "construction")
    op.drop_column("products", "hair_type")
    op.drop_column("products", "brand")
    op.drop_column("products", "short_description")
    op.drop_column("products", "discount_value")
    op.drop_column("products", "discount_type")

    for table, col in [
        ("coupons", "discount_value"),
        ("order_items", "price"),
        ("orders", "total"),
        ("product_variants", "price"),
        ("products", "compare_at_price"),
        ("products", "price"),
    ]:
        op.alter_column(
            table, col,
            existing_type=MONEY,
            type_=sa.Float(),
            existing_nullable=True,
            postgresql_using=f"{col}::double precision" if is_pg else None,
        )

    if is_pg:
        op.execute("DROP TYPE IF EXISTS discountkind")
