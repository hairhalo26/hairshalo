"""Payment records and the payment-aware order lifecycle.

Adds `payments` (gateway transactions, kept separate from order business data)
and extends OrderStatus with pending_payment / paid / refunded.

No card data is stored anywhere — only gateway identifiers.

Revision ID: 0006_payments
Revises: 0005_order_currency_snapshot
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_payments"
down_revision = "0005_order_currency_snapshot"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(12, 2)
PAYMENT_STATES = ("pending", "authorized", "paid", "failed", "cancelled", "refunded")


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        # Extend the existing order status enum in place — existing rows keep
        # their values, so no data migration is needed.
        op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'pending_payment'")
        op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'paid'")
        op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'refunded'")
        op.execute(
            "CREATE TYPE paymentstatus AS ENUM "
            "('pending','authorized','paid','failed','cancelled','refunded')"
        )
        payment_status = postgresql.ENUM(*PAYMENT_STATES, name="paymentstatus",
                                         create_type=False)
    else:
        payment_status = sa.Enum(*PAYMENT_STATES, name="paymentstatus")

    op.create_table(
        "payments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("order_id", sa.String(),
                  sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_order_id", sa.String(), nullable=True),
        sa.Column("provider_payment_id", sa.String(), nullable=True, unique=True),
        sa.Column("provider_refund_id", sa.String(), nullable=True),
        sa.Column("status", payment_status, nullable=False, server_default="pending"),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default="INR"),
        sa.Column("amount_refunded", MONEY, server_default="0"),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("method", sa.String(), nullable=True),
        sa.Column("last_event_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_payments_order_id", "payments", ["order_id"])
    op.create_index("ix_payments_status", "payments", ["status"])
    op.create_index("ix_payments_provider_order_id", "payments", ["provider_order_id"])
    op.create_index("ix_payments_provider_payment_id", "payments", ["provider_payment_id"])


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.drop_index("ix_payments_provider_payment_id", table_name="payments")
    op.drop_index("ix_payments_provider_order_id", table_name="payments")
    op.drop_index("ix_payments_status", table_name="payments")
    op.drop_index("ix_payments_order_id", table_name="payments")
    op.drop_table("payments")

    if is_pg:
        op.execute("DROP TYPE IF EXISTS paymentstatus")
        # PostgreSQL cannot remove enum values; move any order using a new
        # state back to a pre-existing one so the schema stays consistent.
        op.get_bind().execute(sa.text(
            "UPDATE orders SET status = 'processing' "
            "WHERE status IN ('pending_payment','paid')"
        ))
        op.get_bind().execute(sa.text(
            "UPDATE orders SET status = 'cancelled' WHERE status = 'refunded'"
        ))
