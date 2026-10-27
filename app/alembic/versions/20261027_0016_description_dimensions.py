"""资料的描述分维度（SDD 0049，决定 ⑬）：词表第 2 版。

- 进键的版本形态 `doc_edition`（全文、摘要、英文版，缺省全文）；
- 筛选的事项 `topics`（可多选）、行业 `industry`（Wind 29 个）、语言 `language`、
  研报的几维 `attributes`（来源机构、研究员、评级、盈利预测、市场、深度、更新频率）；
- 类型改成大类（发布者）加细类（文献类型），词表升到第 2 版：旧描述的词表版本是 1，
  不再参与解析，要重新确认（生产上还没有第四版的描述）。
- 非定期报告按披露时间匹配，加披露日的索引。

Revision ID: 20261027_0016
Revises: 20261027_0015
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20261027_0016"
down_revision = "20261027_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_data_item",
        sa.Column("doc_edition", sa.String(16), nullable=False, server_default="full"),
    )
    op.add_column(
        "user_data_item",
        sa.Column("topics", pg.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column("user_data_item", sa.Column("industry", sa.String(16)))
    op.add_column("user_data_item", sa.Column("language", sa.String(8)))
    op.add_column(
        "user_data_item",
        sa.Column("attributes", pg.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_check_constraint(
        "ck_user_data_item_doc_edition",
        "user_data_item",
        "doc_edition IN ('full', 'summary', 'english')",
    )
    op.create_index(
        "ix_user_data_item_disclosed",
        "user_data_item",
        ["subject_code", "doc_type", "disclosed_on"],
        postgresql_where=sa.text("subject_code IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_user_data_item_disclosed", table_name="user_data_item")
    op.drop_constraint("ck_user_data_item_doc_edition", "user_data_item", type_="check")
    for column in ("attributes", "language", "industry", "topics", "doc_edition"):
        op.drop_column("user_data_item", column)
