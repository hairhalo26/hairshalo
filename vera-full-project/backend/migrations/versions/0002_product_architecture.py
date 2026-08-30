"""Product architecture hardening: categories, variants, media, workflow.

Data is migrated, not dropped:
  * products.category (text)  -> categories table + products.category_id
  * products.image_url        -> product_media rows (is_primary = true)
  * status 'active'           -> 'published' (enum value renamed in place)

Revision ID: 0002_product_architecture
Revises: 0001_baseline
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_product_architecture"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

PLACEHOLDER_SCHEMA = "vera_product_placeholders"


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # ---------- 1. Extend the product status enum ----------
    if is_pg:
        # 'active' becomes 'published'; renaming keeps existing rows valid
        # without any UPDATE and without dropping the type.
        op.execute("ALTER TYPE productstatus RENAME VALUE 'active' TO 'published'")
        op.execute("ALTER TYPE productstatus ADD VALUE IF NOT EXISTS 'review'")
        op.execute("ALTER TYPE productstatus ADD VALUE IF NOT EXISTS 'out_of_stock'")
        op.execute("CREATE TYPE productbadge AS ENUM ('new','bestseller','featured','limited','sale')")
        op.execute("CREATE TYPE mediatype AS ENUM ('image','video')")

    # The types are created explicitly above, so the column definitions must
    # NOT try to create them again. Only postgresql.ENUM honours create_type;
    # generic sa.Enum silently ignores it and would emit a second CREATE TYPE.
    if is_pg:
        product_badge = postgresql.ENUM(
            "new", "bestseller", "featured", "limited", "sale",
            name="productbadge", create_type=False,
        )
        media_type = postgresql.ENUM("image", "video", name="mediatype", create_type=False)
    else:
        product_badge = sa.Enum("new", "bestseller", "featured", "limited", "sale",
                                name="productbadge")
        media_type = sa.Enum("image", "video", name="mediatype")

    # ---------- 2. Categories ----------
    op.create_table(
        "categories",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("description", sa.Text()),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_categories_slug", "categories", ["slug"])

    # ---------- 3. New product columns ----------
    op.add_column("products", sa.Column("category_id", sa.String(), nullable=True))
    op.add_column("products", sa.Column("texture", sa.String(), nullable=True))
    op.add_column("products", sa.Column("badge", product_badge, nullable=True))
    op.add_column("products", sa.Column("featured", sa.Boolean(), server_default=sa.false()))
    op.add_column("products", sa.Column("bestseller", sa.Boolean(), server_default=sa.false()))
    op.add_column("products", sa.Column("new_arrival", sa.Boolean(), server_default=sa.false()))
    op.add_column("products", sa.Column("sort_order", sa.Integer(), server_default="0"))
    op.add_column("products", sa.Column("is_demo", sa.Boolean(), server_default=sa.false()))
    op.add_column("products", sa.Column("published_at", sa.DateTime(), nullable=True))
    op.add_column("products", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.create_foreign_key("fk_products_category", "products", "categories", ["category_id"], ["id"])
    op.create_index("ix_products_category_id", "products", ["category_id"])
    op.create_index("ix_products_status", "products", ["status"])
    op.create_index("ix_products_featured", "products", ["featured"])
    op.create_index("ix_products_bestseller", "products", ["bestseller"])
    op.create_index("ix_products_new_arrival", "products", ["new_arrival"])
    op.create_index("ix_products_sort_order", "products", ["sort_order"])
    op.create_index("ix_products_is_demo", "products", ["is_demo"])

    # Base price becomes optional (variants may carry the price instead)
    op.alter_column("products", "price", existing_type=sa.Float(), nullable=True)

    # ---------- 4. Media ----------
    op.create_table(
        "product_media",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("product_id", sa.String(),
                  sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("media_type", media_type, nullable=False, server_default="image"),
        sa.Column("alt_text", sa.String()),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.false()),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_product_media_product_id", "product_media", ["product_id"])

    # ---------- 5. Variants ----------
    op.create_table(
        "product_variants",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("product_id", sa.String(),
                  sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sku", sa.String(), nullable=False, unique=True),
        sa.Column("length", sa.String()),
        sa.Column("density", sa.String()),
        sa.Column("color", sa.String()),
        sa.Column("lace_type", sa.String()),
        sa.Column("cap_size", sa.String()),
        sa.Column("price", sa.Float()),
        sa.Column("stock", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_available", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_product_variants_sku", "product_variants", ["sku"])
    op.create_index("ix_product_variants_product_id", "product_variants", ["product_id"])
    op.create_index("ix_product_variants_product_available", "product_variants",
                    ["product_id", "is_available"])

    # ---------- 6. Inventory + order item links ----------
    op.add_column("inventory_items", sa.Column("variant_id", sa.String(), nullable=True))
    op.create_foreign_key("fk_inventory_variant", "inventory_items", "product_variants",
                          ["variant_id"], ["id"])
    op.create_index("ix_inventory_items_variant_id", "inventory_items", ["variant_id"])

    op.add_column("order_items", sa.Column("variant_id", sa.String(), nullable=True))
    op.add_column("order_items", sa.Column("variant_label", sa.String(), nullable=True))
    op.add_column("order_items", sa.Column("variant_sku", sa.String(), nullable=True))
    op.create_foreign_key("fk_order_items_variant", "order_items", "product_variants",
                          ["variant_id"], ["id"])

    # ---------- 7. Placeholder demo flag ----------
    op.add_column(
        "product_placeholders",
        sa.Column("is_demo", sa.Boolean(), server_default=sa.false()),
        schema=PLACEHOLDER_SCHEMA if is_pg else None,
    )

    # ---------- 8. Backfill: category text -> categories rows ----------
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category <> ''"
    )).fetchall()

    def slugify(text_value: str) -> str:
        import re
        return re.sub(r"[^a-z0-9]+", "-", text_value.lower()).strip("-")

    import uuid as _uuid
    for idx, (name,) in enumerate(rows):
        cat_id = str(_uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO categories (id, name, slug, description, sort_order, is_active, created_at) "
                "VALUES (:id, :name, :slug, '', :sort, true, NOW())"
            ),
            {"id": cat_id, "name": name, "slug": slugify(name), "sort": (idx + 1) * 10},
        )
        conn.execute(
            sa.text("UPDATE products SET category_id = :cid WHERE category = :name"),
            {"cid": cat_id, "name": name},
        )

    # ---------- 9. Backfill: image_url -> product_media ----------
    imgs = conn.execute(sa.text(
        "SELECT id, image_url FROM products WHERE image_url IS NOT NULL AND image_url <> ''"
    )).fetchall()
    for pid, url in imgs:
        conn.execute(
            sa.text(
                "INSERT INTO product_media (id, product_id, url, media_type, alt_text, sort_order, is_primary, created_at) "
                "VALUES (:id, :pid, :url, 'image', '', 0, true, NOW())"
            ),
            {"id": str(_uuid.uuid4()), "pid": pid, "url": url},
        )

    # published_at for already-published products
    conn.execute(sa.text(
        "UPDATE products SET published_at = COALESCE(published_at, created_at, NOW()) "
        "WHERE status = 'published'"
    ))
    conn.execute(sa.text("UPDATE products SET updated_at = COALESCE(updated_at, created_at, NOW())"))

    # ---------- 10. Drop the replaced columns ----------
    op.drop_column("products", "category")
    op.drop_column("products", "image_url")


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    conn = op.get_bind()

    op.add_column("products", sa.Column("category", sa.String(), nullable=True))
    op.add_column("products", sa.Column("image_url", sa.String(), nullable=True))
    conn.execute(sa.text(
        "UPDATE products p SET category = c.name FROM categories c WHERE p.category_id = c.id"
    ))
    conn.execute(sa.text(
        "UPDATE products p SET image_url = m.url FROM product_media m "
        "WHERE m.product_id = p.id AND m.is_primary = true"
    ))
    conn.execute(sa.text("UPDATE products SET category = 'Uncategorized' WHERE category IS NULL"))
    op.alter_column("products", "category", existing_type=sa.String(), nullable=False)

    op.drop_column("product_placeholders", "is_demo",
                   schema=PLACEHOLDER_SCHEMA if is_pg else None)

    op.drop_constraint("fk_order_items_variant", "order_items", type_="foreignkey")
    op.drop_column("order_items", "variant_sku")
    op.drop_column("order_items", "variant_label")
    op.drop_column("order_items", "variant_id")

    op.drop_constraint("fk_inventory_variant", "inventory_items", type_="foreignkey")
    op.drop_index("ix_inventory_items_variant_id", table_name="inventory_items")
    op.drop_column("inventory_items", "variant_id")

    op.drop_table("product_variants")
    op.drop_table("product_media")

    op.drop_constraint("fk_products_category", "products", type_="foreignkey")
    for idx in ("ix_products_is_demo", "ix_products_sort_order", "ix_products_new_arrival",
                "ix_products_bestseller", "ix_products_featured", "ix_products_status",
                "ix_products_category_id"):
        op.drop_index(idx, table_name="products")
    for col in ("updated_at", "published_at", "is_demo", "sort_order", "new_arrival",
                "bestseller", "featured", "badge", "texture", "category_id"):
        op.drop_column("products", col)
    op.alter_column("products", "price", existing_type=sa.Float(), nullable=False)

    op.drop_table("categories")

    if is_pg:
        op.execute("DROP TYPE IF EXISTS mediatype")
        op.execute("DROP TYPE IF EXISTS productbadge")
        # Reverse the rename; 'review'/'out_of_stock' cannot be removed from a
        # PostgreSQL enum, so any rows using them are moved to 'draft' first.
        conn.execute(sa.text(
            "UPDATE products SET status = 'draft' WHERE status IN ('review','out_of_stock')"
        ))
        op.execute("ALTER TYPE productstatus RENAME VALUE 'published' TO 'active'")
