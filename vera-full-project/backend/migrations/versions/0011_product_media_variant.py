"""Let a product image belong to one variant.

The supplied product photography is organised by colour — one folder per colour
code (1, 1B, 2, 4, 27, 30, 613) — so a customer who picks 1B should see the 1B
photographs, not the whole product gallery. `product_media` had no way to say
which variant a file belongs to.

Deliberately additive and nullable:

* Existing rows keep `variant_id = NULL`, which means "product-level media" —
  exactly the behaviour every current row already has. Nothing that works today
  changes, and no data is rewritten.
* ON DELETE SET NULL, not CASCADE. Retiring a colour must not delete the
  photographs of it; the media falls back to the product gallery instead.
  CASCADE here would make "discontinue this colour" silently destructive.

Written with batch_alter_table so it applies on SQLite as well as PostgreSQL —
SQLite cannot ALTER a constraint in place and needs the copy-and-move strategy.

Revision ID: 0011_product_media_variant
Revises: 0010_customer_accounts
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_product_media_variant"
down_revision = "0010_customer_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("product_media") as batch:
        batch.add_column(sa.Column("variant_id", sa.String(), nullable=True))
        batch.create_foreign_key(
            "fk_product_media_variant_id",
            "product_variants",
            ["variant_id"], ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_product_media_variant_id", "product_media", ["variant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_product_media_variant_id", table_name="product_media")
    with op.batch_alter_table("product_media") as batch:
        batch.drop_constraint("fk_product_media_variant_id", type_="foreignkey")
        batch.drop_column("variant_id")
