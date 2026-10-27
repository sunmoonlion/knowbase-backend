"""User data v4 resolution: resolutions, confirmations, preferences (SDD 0043 s3).

Expand only. A resolution keeps every revision (new needs found while running
make a new revision); a confirmation is valid for seven days and only for the
items of the revision it names; preferences are ordering hints keyed by user,
consumer, contract version, need key and normalized task parameters.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261027_0012"
down_revision = "20261027_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_user_data_item_description",
        "user_data_item",
        ["subject_code", "doc_type", "period_end"],
        postgresql_where=sa.text("subject_code IS NOT NULL"),
    )
    op.create_table(
        "user_data_resolution",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("app", sa.String(16), nullable=False),
        sa.Column("consumer", sa.String(160), nullable=False),
        sa.Column("contract_version", sa.String(40), nullable=False),
        sa.Column("vocab_version", sa.Integer(), nullable=False),
        sa.Column("task_ref", sa.String(255)),
        sa.Column("needs", postgresql.JSONB(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_user_data_resolution_revision"),
    )
    op.create_index(
        "ix_user_data_resolution_subject",
        "user_data_resolution",
        ["subject", "created_at"],
    )
    op.create_table(
        "user_data_confirmation",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("app", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("task_ref", sa.String(255)),
        sa.Column("resolution_id", postgresql.UUID(as_uuid=True)),
        sa.Column("resolution_revision", sa.Integer()),
        sa.Column(
            "needs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("supply", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('description', 'url', 'topic')",
            name="ck_user_data_confirmation_kind",
        ),
    )
    op.create_index(
        "ix_user_data_confirmation_subject",
        "user_data_confirmation",
        ["subject", "expires_at"],
    )
    op.create_table(
        "user_data_preference",
        sa.Column("subject", sa.String(255), primary_key=True),
        sa.Column("consumer", sa.String(160), primary_key=True),
        sa.Column("contract_version", sa.String(40), primary_key=True),
        sa.Column("need_key", sa.String(40), primary_key=True),
        sa.Column("params_hash", sa.String(64), primary_key=True),
        sa.Column("material_ids", postgresql.JSONB(), nullable=False),
        sa.Column("vocab_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_data_preference")
    op.drop_index(
        "ix_user_data_confirmation_subject", table_name="user_data_confirmation"
    )
    op.drop_table("user_data_confirmation")
    op.drop_index("ix_user_data_resolution_subject", table_name="user_data_resolution")
    op.drop_table("user_data_resolution")
    op.drop_index("ix_user_data_item_description", table_name="user_data_item")
