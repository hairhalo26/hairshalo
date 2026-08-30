"""Reviews, the loyalty ledger, and the marketing list.

Adds `reviews`, `loyalty_transactions`, `marketing_subscribers` and `campaigns`.

It also **resets `products.rating` and `products.review_count` to zero**. Those
columns were seeded with invented numbers (4.9 stars from 312 reviews that did
not exist). From this revision on they are a cache maintained solely by
`app/reviews.py:recalculate()` from published reviews, so the only honest
starting value is nothing.

That reset is deliberately not reversible: `downgrade()` drops the new tables
but leaves the ratings at zero, because restoring them would mean inventing the
same numbers again. A product with no reviews shows "no reviews yet", which is
the truth.

Revision ID: 0009_growth
Revises: 0008_notifications
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_growth"
down_revision = "0008_notifications"
branch_labels = None
depends_on = None

REVIEW_STATUS = ("pending", "published", "rejected")
LOYALTY_REASON = ("earned", "redeemed", "reversed", "returned", "adjustment", "expired")
SUBSCRIBER_STATUS = ("pending", "confirmed", "unsubscribed")
CAMPAIGN_STATUS = ("draft", "sent", "cancelled")


def _enum(is_pg, values, name):
    if is_pg:
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        for name, values in (
            ("reviewstatus", REVIEW_STATUS),
            ("loyaltyreason", LOYALTY_REASON),
            ("subscriberstatus", SUBSCRIBER_STATUS),
            ("campaignstatus", CAMPAIGN_STATUS),
        ):
            joined = ",".join(f"'{v}'" for v in values)
            op.execute(
                f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({joined}); "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
            )

    review_status = _enum(is_pg, REVIEW_STATUS, "reviewstatus")
    loyalty_reason = _enum(is_pg, LOYALTY_REASON, "loyaltyreason")
    subscriber_status = _enum(is_pg, SUBSCRIBER_STATUS, "subscriberstatus")
    campaign_status = _enum(is_pg, CAMPAIGN_STATUS, "campaignstatus")

    # ---- reviews -------------------------------------------------------
    op.create_table(
        "reviews",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("product_id", sa.String(), nullable=False),
        sa.Column("order_id", sa.String(), nullable=True),
        sa.Column("order_item_id", sa.String(), nullable=True),
        sa.Column("customer_id", sa.String(), nullable=True),
        sa.Column("author_name", sa.String(), nullable=False),
        sa.Column("author_email", sa.String(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("status", review_status, nullable=False, server_default="pending"),
        sa.Column("is_verified_purchase", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("is_demo", sa.Boolean(), nullable=True, server_default=sa.false()),
        sa.Column("moderated_by", sa.String(), nullable=True),
        sa.Column("moderated_at", sa.DateTime(), nullable=True),
        sa.Column("moderation_note", sa.String(), nullable=True),
        sa.Column("reply_body", sa.Text(), nullable=True),
        sa.Column("reply_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        # UNIQUE: one review per purchased line. Buying the same wig twice earns
        # two reviews; buying it once does not.
        sa.UniqueConstraint("order_item_id", name="uq_reviews_order_item"),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_reviews_rating_range"),
    )
    op.create_index("ix_reviews_product_id", "reviews", ["product_id"])
    op.create_index("ix_reviews_order_id", "reviews", ["order_id"])
    op.create_index("ix_reviews_customer_id", "reviews", ["customer_id"])
    op.create_index("ix_reviews_author_email", "reviews", ["author_email"])
    op.create_index("ix_reviews_status", "reviews", ["status"])
    op.create_index("ix_reviews_is_demo", "reviews", ["is_demo"])
    op.create_index("ix_reviews_created_at", "reviews", ["created_at"])
    op.create_index("ix_reviews_product_status", "reviews", ["product_id", "status"])

    # ---- loyalty -------------------------------------------------------
    op.create_table(
        "loyalty_transactions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("customer_id", sa.String(), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("reason", loyalty_reason, nullable=False),
        sa.Column("reference_type", sa.String(), nullable=True),
        sa.Column("reference_id", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_loyalty_transactions_customer_id", "loyalty_transactions",
                    ["customer_id"])
    op.create_index("ix_loyalty_transactions_reason", "loyalty_transactions", ["reason"])
    op.create_index("ix_loyalty_transactions_reference_id", "loyalty_transactions",
                    ["reference_id"])
    op.create_index("ix_loyalty_transactions_created_at", "loyalty_transactions",
                    ["created_at"])

    # Existing balances have no ledger behind them, so give them one: a single
    # opening entry per customer. Without it the ledger would disagree with the
    # balance from the very first day, and the invariant the code relies on
    # ("the ledger explains the balance") would be false before anyone used it.
    op.execute(
        """
        INSERT INTO loyalty_transactions
            (id, customer_id, delta, balance_after, reason, reference_type,
             note, actor, created_at)
        SELECT
            md5(random()::text || clock_timestamp()::text)::uuid::text,
            c.id, c.loyalty_points, c.loyalty_points, 'adjustment', 'migration',
            'Opening balance carried over when the ledger was introduced',
            'migration:0009_growth', now()
        FROM customers c
        WHERE COALESCE(c.loyalty_points, 0) <> 0
        """
        if is_pg else
        """
        INSERT INTO loyalty_transactions
            (id, customer_id, delta, balance_after, reason, reference_type,
             note, actor, created_at)
        SELECT lower(hex(randomblob(16))), c.id, c.loyalty_points,
               c.loyalty_points, 'adjustment', 'migration',
               'Opening balance carried over when the ledger was introduced',
               'migration:0009_growth', CURRENT_TIMESTAMP
        FROM customers c
        WHERE COALESCE(c.loyalty_points, 0) <> 0
        """
    )

    # ---- marketing -----------------------------------------------------
    op.create_table(
        "marketing_subscribers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("status", subscriber_status, nullable=False, server_default="pending"),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("unsubscribed_at", sa.DateTime(), nullable=True),
        sa.Column("consent_ip", sa.String(), nullable=True),
        sa.Column("last_campaign_at", sa.DateTime(), nullable=True),
        sa.Column("is_demo", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.create_index("ix_marketing_subscribers_email", "marketing_subscribers",
                    ["email"], unique=True)
    op.create_index("ix_marketing_subscribers_status", "marketing_subscribers", ["status"])
    op.create_index("ix_marketing_subscribers_is_demo", "marketing_subscribers", ["is_demo"])

    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("preheader", sa.String(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("cta_label", sa.String(), nullable=True),
        sa.Column("cta_url", sa.String(), nullable=True),
        sa.Column("status", campaign_status, nullable=False, server_default="draft"),
        sa.Column("recipient_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_campaigns_status", "campaigns", ["status"])

    # ---- orders can now be part-paid with points ------------------------
    op.add_column("orders", sa.Column("loyalty_points_redeemed", sa.Integer(),
                                      nullable=False, server_default="0"))
    op.add_column("orders", sa.Column("loyalty_discount", sa.Numeric(12, 2),
                                      nullable=False, server_default="0"))

    # ---- retire the invented ratings ------------------------------------
    op.execute("UPDATE products SET rating = 0, review_count = 0")


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # Ratings are NOT restored: the previous values were invented, and this
    # migration has no way to tell them from real ones. They stay at zero.
    op.drop_column("orders", "loyalty_discount")
    op.drop_column("orders", "loyalty_points_redeemed")
    op.drop_index("ix_campaigns_status", table_name="campaigns")
    op.drop_table("campaigns")

    for name in ("ix_marketing_subscribers_is_demo", "ix_marketing_subscribers_status",
                 "ix_marketing_subscribers_email"):
        op.drop_index(name, table_name="marketing_subscribers")
    op.drop_table("marketing_subscribers")

    for name in ("ix_loyalty_transactions_created_at",
                 "ix_loyalty_transactions_reference_id",
                 "ix_loyalty_transactions_reason",
                 "ix_loyalty_transactions_customer_id"):
        op.drop_index(name, table_name="loyalty_transactions")
    op.drop_table("loyalty_transactions")

    for name in ("ix_reviews_product_status", "ix_reviews_created_at",
                 "ix_reviews_is_demo", "ix_reviews_status", "ix_reviews_author_email",
                 "ix_reviews_customer_id", "ix_reviews_order_id", "ix_reviews_product_id"):
        op.drop_index(name, table_name="reviews")
    op.drop_table("reviews")

    if is_pg:
        for name in ("campaignstatus", "subscriberstatus", "loyaltyreason", "reviewstatus"):
            op.execute(f"DROP TYPE IF EXISTS {name}")
