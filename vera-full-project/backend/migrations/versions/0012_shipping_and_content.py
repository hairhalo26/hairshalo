"""Structured shipping snapshot, notification read state, and site content.

Three additive changes, none of which rewrites or removes existing data.

1. `orders` gains structured shipping columns. Until now the delivery address
   was one free-text blob, which is unusable for the two things an address is
   actually for: printing a label and answering "where did this go?". The blob
   column stays and is still written, so every historical order keeps exactly
   the text it was placed with and nothing that reads it breaks. New orders
   populate both.

   These columns are a SNAPSHOT, deliberately duplicated from
   `customer_addresses` rather than referenced by foreign key. An address a
   customer later edits or deletes must not rewrite where a past order was
   sent — a foreign key here would do precisely that.

2. `notifications` gains `read_at`. The outbox tracks delivery state (queued /
   sent / failed), which is a fact about the mail server, not about whether a
   human has looked at it. The admin panel needs the second thing, and reusing
   `status` for it would corrupt the first.

3. `site_content` is a new table holding the storefront's editable copy —
   announcement bar, promotional panels, FAQ entries, footer, business details.
   One row per block, with the payload as JSON, because these blocks have
   genuinely different shapes and a column per field would mean a migration
   every time the marketing copy grows a line. Content is seeded from the
   current hardcoded storefront copy by `app/site_content.py` on first read, so
   this migration creates an empty table and changes nothing a customer sees.

4. `categories` gains `tagline` and `image_url`, so the storefront's collection
   cards can come from the database instead of being hardcoded with external
   image URLs.

Revision ID: 0012_shipping_and_content
Revises: 0011_product_media_variant
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_shipping_and_content"
down_revision = "0011_product_media_variant"
branch_labels = None
depends_on = None


SHIPPING_COLUMNS = [
    ("shipping_name", sa.String()),
    ("shipping_phone", sa.String()),
    ("shipping_line1", sa.String()),
    ("shipping_line2", sa.String()),
    ("shipping_city", sa.String()),
    ("shipping_state", sa.String()),
    ("shipping_postal_code", sa.String()),
    ("shipping_country", sa.String()),
]


def upgrade() -> None:
    # --- 1. Order shipping snapshot ---------------------------------------
    # All nullable: 1,300+ existing orders have only the text blob, and
    # inventing structured fields for them by parsing that blob would be
    # guessing at data that shipping decisions depend on. They stay NULL, and
    # the API falls back to the blob for those orders.
    with op.batch_alter_table("orders") as batch:
        for name, type_ in SHIPPING_COLUMNS:
            batch.add_column(sa.Column(name, type_, nullable=True))

    # --- 2. Notification read state ---------------------------------------
    # NULL means unread. Existing rows are therefore all "unread", which would
    # put 2,696 on the admin badge on first load, so they are marked read on
    # the way in: they predate the feature and nobody needs to acknowledge
    # them one by one.
    with op.batch_alter_table("notifications") as batch:
        batch.add_column(sa.Column("read_at", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE notifications SET read_at = COALESCE(sent_at, created_at, now())"
    )
    op.create_index(
        "ix_notifications_read_at", "notifications", ["read_at"], unique=False
    )

    # --- 3. Site content --------------------------------------------------
    op.create_table(
        "site_content",
        sa.Column("id", sa.String(), primary_key=True),
        # The storefront asks for a block by key ("announcement", "faq"), so
        # the key is the stable contract between the two — not the row id.
        sa.Column("key", sa.String(), nullable=False, unique=True, index=True),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )

    # --- 4. Category presentation ----------------------------------------
    with op.batch_alter_table("categories") as batch:
        batch.add_column(sa.Column("tagline", sa.String(), nullable=True))
        batch.add_column(sa.Column("image_url", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("categories") as batch:
        batch.drop_column("image_url")
        batch.drop_column("tagline")

    op.drop_table("site_content")

    op.drop_index("ix_notifications_read_at", table_name="notifications")
    with op.batch_alter_table("notifications") as batch:
        batch.drop_column("read_at")

    with op.batch_alter_table("orders") as batch:
        for name, _type in reversed(SHIPPING_COLUMNS):
            batch.drop_column(name)
