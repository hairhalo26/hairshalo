"""Staff token version, so Admin sign-out revokes tokens server-side.

One additive column. `users.token_version` defaults to 0 for every existing
staff account; tokens issued before this migration carry no version and are
read as 0, so nobody is signed out by the upgrade itself.

Revision ID: 0015_staff_token_version
Revises: 0014_supplier_import
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_staff_token_version"
down_revision = "0014_supplier_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("token_version", sa.Integer(),
                                   server_default="0", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("token_version")
