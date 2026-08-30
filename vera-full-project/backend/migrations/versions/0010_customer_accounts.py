"""Customer accounts: credentials, addresses and wishlists.

Adds password credentials to `customers`, plus `customer_addresses` and
`wishlist_items`.

Two notes on the columns added to `customers`:

* `hashed_password` is NULLABLE on purpose. Most rows in this table were created
  by checkout, from an email typed into an order form — nobody has proved they
  own those mailboxes. Those customers have no account until they register.
* `email_verified` defaults to FALSE for the same reason. Order history is
  gated on it, so registering with a stranger's address cannot expose the
  orders already sitting under it.

Revision ID: 0010_customer_accounts
Revises: 0009_growth
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_customer_accounts"
down_revision = "0009_growth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("hashed_password", sa.String(), nullable=True))
    op.add_column("customers", sa.Column("email_verified", sa.Boolean(), nullable=False,
                                         server_default=sa.false()))
    op.add_column("customers", sa.Column("is_active", sa.Boolean(), nullable=False,
                                         server_default=sa.true()))
    op.add_column("customers", sa.Column("token_version", sa.Integer(), nullable=False,
                                         server_default="0"))
    op.add_column("customers", sa.Column("password_reset_hash", sa.String(), nullable=True))
    op.add_column("customers", sa.Column("password_reset_expires_at", sa.DateTime(),
                                         nullable=True))
    op.add_column("customers", sa.Column("password_changed_at", sa.DateTime(), nullable=True))
    op.add_column("customers", sa.Column("last_login_at", sa.DateTime(), nullable=True))
    op.add_column("customers", sa.Column("registered_at", sa.DateTime(), nullable=True))
    op.add_column("customers", sa.Column("preferred_currency", sa.String(), nullable=True))

    op.create_table(
        "customer_addresses",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("customer_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("line1", sa.String(), nullable=False),
        sa.Column("line2", sa.String(), nullable=True),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("postal_code", sa.String(), nullable=True),
        sa.Column("country", sa.String(), nullable=False, server_default="India"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_customer_addresses_customer_id", "customer_addresses",
                    ["customer_id"])

    op.create_table(
        "wishlist_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("customer_id", sa.String(), nullable=False),
        sa.Column("product_id", sa.String(), nullable=False),
        sa.Column("variant_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="CASCADE"),
        # UNIQUE so a double-tapped heart cannot create two rows.
        sa.UniqueConstraint("customer_id", "product_id", "variant_id",
                            name="uq_wishlist_customer_product_variant"),
    )
    op.create_index("ix_wishlist_items_customer_id", "wishlist_items", ["customer_id"])
    op.create_index("ix_wishlist_items_product_id", "wishlist_items", ["product_id"])


def downgrade() -> None:
    op.drop_index("ix_wishlist_items_product_id", table_name="wishlist_items")
    op.drop_index("ix_wishlist_items_customer_id", table_name="wishlist_items")
    op.drop_table("wishlist_items")

    op.drop_index("ix_customer_addresses_customer_id", table_name="customer_addresses")
    op.drop_table("customer_addresses")

    for column in ("preferred_currency", "registered_at", "last_login_at",
                   "password_changed_at", "password_reset_expires_at",
                   "password_reset_hash", "token_version", "is_active",
                   "email_verified", "hashed_password"):
        op.drop_column("customers", column)
