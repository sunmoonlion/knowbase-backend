"""Consent for a single pinned material and method invocation."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261119_0022"
down_revision = "20261101_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_data_run_grant",
        sa.Column(
            "method_only", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.create_table(
        "user_data_method_read",
        sa.Column("beneficiary", sa.String(255), primary_key=True),
        sa.Column("call_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "grant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_run_grant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("binding", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM user_data_method_read) "
            "OR EXISTS (SELECT 1 FROM user_data_run_grant WHERE method_only)"
        )
    ):
        raise RuntimeError("Cannot discard recorded method read consents")
    op.drop_table("user_data_method_read")
    op.drop_column("user_data_run_grant", "method_only")
