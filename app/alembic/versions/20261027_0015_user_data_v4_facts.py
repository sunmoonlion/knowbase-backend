"""User data v4 facts: as-reported and standardized rows of a table reading.

SDD 0047 section 4. A `table` rendition version is a batch of fact rows that
never changes; the rendition digest is computed over the batch. Raw rows keep
what the statement printed (label, page, row, unit); standardized rows carry
a chart code, a value in yuan and the raw rows they come from.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261027_0015"
down_revision = "20261027_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_data_fact_raw",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "rendition_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_rendition.id"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_item.id"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("value", sa.Numeric(30, 6)),
        sa.Column("unit", sa.String(8), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("period_end", sa.Date()),
        sa.Column("duration", sa.String(8)),
        sa.Column("consolidation", sa.String(16)),
        sa.Column("statement_label", sa.String(100)),
        sa.Column("page", sa.Integer()),
        sa.Column("row_no", sa.Integer()),
        sa.Column("sheet", sa.String(100)),
        sa.Column("cell", sa.String(20)),
        sa.UniqueConstraint("rendition_id", "ordinal", name="uq_user_data_fact_raw"),
    )
    op.create_table(
        "user_data_fact",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "rendition_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_rendition.id"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_item.id"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("subject_code", sa.String(64)),
        sa.Column("chart_version", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("value", sa.Numeric(30, 6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("duration", sa.String(8), nullable=False),
        sa.Column("consolidation", sa.String(16), nullable=False),
        sa.Column("derived_from", postgresql.JSONB(), nullable=False),
        sa.Column("rule", sa.String(80), nullable=False),
        sa.Column("page", sa.Integer()),
    )
    op.create_index("ix_user_data_fact_rendition", "user_data_fact", ["rendition_id"])
    op.create_index(
        "ix_user_data_fact_subject_code",
        "user_data_fact",
        ["subject_code", "code", "period_end"],
    )
    op.create_index(
        "ix_user_data_fact_code_period", "user_data_fact", ["code", "period_end"]
    )


def downgrade() -> None:
    op.drop_index("ix_user_data_fact_code_period", table_name="user_data_fact")
    op.drop_index("ix_user_data_fact_subject_code", table_name="user_data_fact")
    op.drop_index("ix_user_data_fact_rendition", table_name="user_data_fact")
    op.drop_table("user_data_fact")
    op.drop_table("user_data_fact_raw")
