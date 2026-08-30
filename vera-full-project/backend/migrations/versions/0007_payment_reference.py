"""Give offline payments their own reference field.

`provider_payment_id` is UNIQUE because it is the gateway's identifier and the
basis of webhook idempotency. A human bank reference ("NEFT-1") is neither
unique nor gateway-issued, so it needs its own column instead of borrowing
that one.

Revision ID: 0007_payment_reference
Revises: 0006_payments
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_payment_reference"
down_revision = "0006_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("reference", sa.String(), nullable=True))
    op.add_column("payments", sa.Column("note", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("payments", "note")
    op.drop_column("payments", "reference")
