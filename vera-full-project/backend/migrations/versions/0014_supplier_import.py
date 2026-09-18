"""Generic supplier catalogue import.

Additive only. Nothing existing is rewritten or dropped.

1. `products` gains the descriptive fields supplier catalogues carry and the
   product model did not: product_type, weight, heat_resistance, features,
   benefits, care_instructions. All nullable, all customer-facing, all editable
   in the Admin Panel like any other product field.

2. `suppliers` — one row per authorised supplier: its name and reference, its
   default currency, and what the last import used (field mapping, category
   mapping, pricing), so the next file from the same supplier starts from it.

3. `supplier_imports` — one row per uploaded file: the import log. It holds the
   parsed rows, the preview and, after commit, the per-row result, so an admin
   can open any past import and see exactly what happened to each product.

4. `supplier_products` / `supplier_variants` — the link between a supplier's
   item and a Hairshalo product/variant, with the supplier's own identifiers,
   price, currency and availability. Kept apart from `products` on purpose:
   supplier data is internal, and a supplier price must never be mistaken for
   (or overwrite) the Hairshalo selling price.

Revision ID: 0014_supplier_import
Revises: 0013_orders_coupons_cats
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_supplier_import"
down_revision = "0013_orders_coupons_cats"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(12, 2)


def upgrade() -> None:
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("product_type", sa.String(), nullable=True))
        batch.add_column(sa.Column("weight", sa.String(), nullable=True))
        batch.add_column(sa.Column("heat_resistance", sa.String(), nullable=True))
        batch.add_column(sa.Column("features", sa.Text(), nullable=True))
        batch.add_column(sa.Column("benefits", sa.Text(), nullable=True))
        batch.add_column(sa.Column("care_instructions", sa.Text(), nullable=True))

    op.create_table(
        "suppliers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("code", sa.String(), nullable=False, unique=True),
        sa.Column("reference", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=False, server_default="INR"),
        sa.Column("allowed_image_hosts", sa.JSON(), nullable=True),
        sa.Column("field_mapping", sa.JSON(), nullable=True),
        sa.Column("category_mapping", sa.JSON(), nullable=True),
        sa.Column("pricing", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "supplier_imports",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("supplier_id", sa.String(),
                  sa.ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_name", sa.String(), nullable=False),
        sa.Column("file_format", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="uploaded"),
        sa.Column("columns", sa.JSON(), nullable=True),
        sa.Column("raw_rows", sa.JSON(), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("preview", sa.JSON(), nullable=True),
        sa.Column("results", sa.JSON(), nullable=True),
        sa.Column("image_files", sa.JSON(), nullable=True),
        sa.Column("total_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("valid_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("imported", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duplicates", sa.Integer(), server_default="0", nullable=False),
        sa.Column("errors", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_images", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("committed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_supplier_imports_supplier_id", "supplier_imports", ["supplier_id"])

    op.create_table(
        "supplier_products",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("supplier_id", sa.String(),
                  sa.ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.String(),
                  sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_product_id", sa.String(), nullable=False),
        sa.Column("source_sku", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("source_name", sa.String(), nullable=True),
        sa.Column("source_price", MONEY, nullable=True),
        sa.Column("source_currency", sa.String(), nullable=True),
        sa.Column("source_availability", sa.String(), nullable=True),
        sa.Column("source_file", sa.String(), nullable=True),
        sa.Column("source_data", sa.JSON(), nullable=True),
        sa.Column("last_import_id", sa.String(), nullable=True),
        sa.Column("first_imported_at", sa.DateTime(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("supplier_id", "source_product_id",
                            name="uq_supplier_products_source"),
    )
    op.create_index("ix_supplier_products_product_id", "supplier_products", ["product_id"])

    op.create_table(
        "supplier_variants",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("supplier_product_id", sa.String(),
                  sa.ForeignKey("supplier_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("variant_id", sa.String(),
                  sa.ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_variant_id", sa.String(), nullable=True),
        sa.Column("source_sku", sa.String(), nullable=True),
        sa.Column("source_price", MONEY, nullable=True),
        sa.Column("source_availability", sa.String(), nullable=True),
        sa.Column("source_quantity", sa.Integer(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_supplier_variants_parent", "supplier_variants", ["supplier_product_id"])
    op.create_index("ix_supplier_variants_source_sku", "supplier_variants", ["source_sku"])


def downgrade() -> None:
    op.drop_index("ix_supplier_variants_source_sku", table_name="supplier_variants")
    op.drop_index("ix_supplier_variants_parent", table_name="supplier_variants")
    op.drop_table("supplier_variants")
    op.drop_index("ix_supplier_products_product_id", table_name="supplier_products")
    op.drop_table("supplier_products")
    op.drop_index("ix_supplier_imports_supplier_id", table_name="supplier_imports")
    op.drop_table("supplier_imports")
    op.drop_table("suppliers")
    with op.batch_alter_table("products") as batch:
        for name in ("care_instructions", "benefits", "features", "heat_resistance",
                     "weight", "product_type"):
            batch.drop_column(name)
