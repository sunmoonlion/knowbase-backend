"""User data v4 sessions: a session registered once by the user (SDD 0048 s5).

The user registers a session through the app with their own token; the model
then reads inside it and knowbase signs one small grant per first read, as
long as the item is in the session's scope (shown to the app, or a public
original while public access is open).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261027_0013"
down_revision = "20261027_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_data_session",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("executor_app", sa.String(16), nullable=False),
        sa.Column("scope_use", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_user_data_session_subject", "user_data_session", ["subject", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_user_data_session_subject", table_name="user_data_session")
    op.drop_table("user_data_session")
