"""每件委托要什么数据由第 0 步提出（decisions ⑭）：偏好按描述记。

偏好原来的键是「用户 × 使用者 × 声明契约版本 × 需求 key × 规范化的任务参数」，10-28 起
清单里不再有数据需求声明，也没有任务参数，键改成「用户 × 使用者 × 描述的摘要」。
偏好还没有写入的路（块 6 未做），表是空的，删了重建。

Revision ID: 20261028_0017
Revises: 20261027_0016
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20261028_0017"
down_revision = "20261027_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("user_data_preference")
    op.create_table(
        "user_data_preference",
        sa.Column("subject", sa.String(255), primary_key=True),
        sa.Column("consumer", sa.String(160), primary_key=True),
        sa.Column("key_id", sa.String(64), primary_key=True),
        sa.Column("material_ids", pg.JSONB(), nullable=False),
        sa.Column("vocab_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_data_preference")
    op.create_table(
        "user_data_preference",
        sa.Column("subject", sa.String(255), primary_key=True),
        sa.Column("consumer", sa.String(160), primary_key=True),
        sa.Column("contract_version", sa.String(40), primary_key=True),
        sa.Column("need_key", sa.String(40), primary_key=True),
        sa.Column("params_hash", sa.String(64), primary_key=True),
        sa.Column("material_ids", pg.JSONB(), nullable=False),
        sa.Column("vocab_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
