"""RAGFlow version mapping and released run refs (SDD 0043 block 9, 0047 s2.4).

- user_data_version adds ragflow_dataset_id, ragflow_document_id, ragflow_job_id, ragflow_status
- user_data_released_ref records released run refs to reject new grants (ref_released)

Revision ID: 20261101_0021
Revises: 20261029_0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261101_0021"
down_revision = "20261029_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_data_version", sa.Column("ragflow_dataset_id", sa.String(64), nullable=True))
    op.add_column("user_data_version", sa.Column("ragflow_document_id", sa.String(64), nullable=True))
    op.add_column("user_data_version", sa.Column("ragflow_job_id", sa.String(64), nullable=True))
    op.add_column("user_data_version", sa.Column("ragflow_status", sa.String(32), nullable=True))

    op.create_table(
        "user_data_released_ref",
        sa.Column("executor_app", sa.String(16), primary_key=True),
        sa.Column("ref", sa.String(255), primary_key=True),
        sa.Column("beneficiary", sa.String(255), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_user_data_released_ref_beneficiary",
        "user_data_released_ref",
        ["beneficiary"],
        postgresql_where=sa.text("beneficiary IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_user_data_released_ref_beneficiary", table_name="user_data_released_ref")
    op.drop_table("user_data_released_ref")
    op.drop_column("user_data_version", "ragflow_status")
    op.drop_column("user_data_version", "ragflow_job_id")
    op.drop_column("user_data_version", "ragflow_document_id")
    op.drop_column("user_data_version", "ragflow_dataset_id")
