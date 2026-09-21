"""Managed colours for the hair catalogue, and an index for hair-type filters.

Additive only. No existing row is rewritten and nothing is inserted:

1. `colours` — the Back Office's reusable colour list (name, slug, optional
   hex swatch, sort order, active flag). Created empty; staff add colours.

2. `product_variants.colour_id` — nullable FK to `colours`. Every existing
   variant keeps colour_id NULL and its free-text `color` exactly as it is:
   linking "1B" to a managed colour is a decision for staff, not a guess made
   by a migration. ON DELETE RESTRICT, so a colour in use cannot vanish from
   under a variant (the API archives it instead).

3. An index on `categories.parent_id`. The hair hierarchy reuses the existing
   one-level category tree (Hair Category -> Hair Type), and the shop now
   filters a category together with its subcategories.

Revision ID: 0016_hair_colours
Revises: 0015_staff_token_version
"""
from alembic import op
import sqlalchemy as sa

# Kept under 32 characters: alembic_version.version_num is VARCHAR(32).
revision = "0016_hair_colours"
down_revision = "0015_staff_token_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "colours",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("hex", sa.String(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("name", name="uq_colours_name"),
    )
    op.create_index("ix_colours_slug", "colours", ["slug"], unique=True)

    with op.batch_alter_table("product_variants") as batch:
        batch.add_column(sa.Column("colour_id", sa.String(), nullable=True))
        batch.create_foreign_key("fk_product_variants_colour_id", "colours",
                                 ["colour_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_product_variants_colour_id", "product_variants", ["colour_id"])

    op.create_index("ix_categories_parent_id", "categories", ["parent_id"])


def downgrade() -> None:
    op.drop_index("ix_categories_parent_id", table_name="categories")
    op.drop_index("ix_product_variants_colour_id", table_name="product_variants")
    with op.batch_alter_table("product_variants") as batch:
        batch.drop_constraint("fk_product_variants_colour_id", type_="foreignkey")
        batch.drop_column("colour_id")
    op.drop_index("ix_colours_slug", table_name="colours")
    op.drop_table("colours")
