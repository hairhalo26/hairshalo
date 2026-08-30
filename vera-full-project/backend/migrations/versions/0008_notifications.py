"""Notification outbox and suppression list.

Adds `notifications` (one row per outbound message, queued inside the business
transaction that caused it) and `notification_suppressions` (addresses we must
not mail).

The UNIQUE index on `notifications.event_key` is the schema-level guarantee
behind "a notification is never sent twice" — the same argument as
`payments.provider_payment_id`. Application checks can race; this cannot.

Revision ID: 0008_notifications
Revises: 0007_payment_reference
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_notifications"
down_revision = "0007_payment_reference"
branch_labels = None
depends_on = None

# Enum members are stored by NAME (SQLAlchemy's default), matching 0006.
NOTIFICATION_STATUS = ("queued", "sent", "failed", "suppressed", "cancelled")
NOTIFICATION_CATEGORY = ("transactional", "operational", "marketing")
NOTIFICATION_CHANNEL = ("email", "sms")
SUPPRESSION_SCOPE = ("marketing", "all")


def _enum(is_pg: bool, values, name: str):
    if is_pg:
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        for name, values in (
            ("notificationstatus", NOTIFICATION_STATUS),
            ("notificationcategory", NOTIFICATION_CATEGORY),
            ("notificationchannel", NOTIFICATION_CHANNEL),
            ("suppressionscope", SUPPRESSION_SCOPE),
        ):
            joined = ",".join(f"'{v}'" for v in values)
            op.execute(
                f"DO $$ BEGIN "
                f"CREATE TYPE {name} AS ENUM ({joined}); "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
            )

    status = _enum(is_pg, NOTIFICATION_STATUS, "notificationstatus")
    category = _enum(is_pg, NOTIFICATION_CATEGORY, "notificationcategory")
    channel = _enum(is_pg, NOTIFICATION_CHANNEL, "notificationchannel")
    scope = _enum(is_pg, SUPPRESSION_SCOPE, "suppressionscope")

    op.create_table(
        "notifications",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("event_key", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("channel", channel, nullable=False, server_default="email"),
        sa.Column("category", category, nullable=False, server_default="transactional"),
        sa.Column("status", status, nullable=False, server_default="queued"),
        sa.Column("recipient", sa.String(), nullable=False),
        sa.Column("recipient_name", sa.String(), nullable=True),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("reference_type", sa.String(), nullable=True),
        sa.Column("reference_id", sa.String(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    # UNIQUE: the real idempotency guarantee.
    op.create_index("ix_notifications_event_key", "notifications", ["event_key"], unique=True)
    op.create_index("ix_notifications_event_type", "notifications", ["event_type"])
    op.create_index("ix_notifications_status", "notifications", ["status"])
    op.create_index("ix_notifications_category", "notifications", ["category"])
    op.create_index("ix_notifications_recipient", "notifications", ["recipient"])
    op.create_index("ix_notifications_reference_id", "notifications", ["reference_id"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
    op.create_index("ix_notifications_next_attempt_at", "notifications", ["next_attempt_at"])
    # The dispatcher's query: due, queued, oldest first.
    op.create_index("ix_notifications_due", "notifications", ["status", "next_attempt_at"])

    op.create_table(
        "notification_suppressions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("scope", scope, nullable=False, server_default="marketing"),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_notification_suppressions_email", "notification_suppressions",
                    ["email"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.drop_index("ix_notification_suppressions_email", table_name="notification_suppressions")
    op.drop_table("notification_suppressions")

    for name in (
        "ix_notifications_due", "ix_notifications_next_attempt_at",
        "ix_notifications_created_at", "ix_notifications_reference_id",
        "ix_notifications_recipient", "ix_notifications_category",
        "ix_notifications_status", "ix_notifications_event_type",
        "ix_notifications_event_key",
    ):
        op.drop_index(name, table_name="notifications")
    op.drop_table("notifications")

    if is_pg:
        # Unlike 0006's order-status change, these types are used by nothing
        # else, so dropping them is safe and keeps a downgrade clean.
        for name in ("suppressionscope", "notificationchannel",
                     "notificationcategory", "notificationstatus"):
            op.execute(f"DROP TYPE IF EXISTS {name}")
