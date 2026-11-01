"""正文、按页文字两种读法存进 PostgreSQL（SDD 0043 第五节，数据拓扑第 16.2 节）。

- `user_data_text`：正文，每个读法版本一行（标题、发布日期、正文）；
- `user_data_page`：按页文字，每个读法版本每页一行；
- `user_data_rendition.object_key` 删掉：原来正文、按页文字各存成对象存储里的一个对象，
  读法表只记对象键；现在内容都在库里（表格读法本来就是事实行），原件仍在对象存储。

第四版还没上线，生产上没有这类读法，不搬数据（decisions ⑰：不留旧版兼容）。万一哪个
环境里有存成对象的正文、按页文字读法，标成没成、写明原因，不留一个就绪却读不到内容的
读法版本；knowfeed 重新加工出的是新的读法版本。

downgrade 加回 `object_key`（空）、删两张表；库里的读法内容不恢复。

Revision ID: 20261029_0019
Revises: 20261028_0018
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261029_0019"
down_revision = "20261028_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_data_text",
        sa.Column(
            "rendition_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_rendition.id"),
            primary_key=True,
        ),
        sa.Column("title", sa.String(500)),
        sa.Column("published_on", sa.Date()),
        sa.Column("body", sa.Text(), nullable=False),
    )
    op.create_table(
        "user_data_page",
        sa.Column(
            "rendition_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_rendition.id"),
            primary_key=True,
        ),
        sa.Column("page", sa.Integer(), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.CheckConstraint("page >= 1", name="ck_user_data_page_no"),
    )
    op.execute(
        """
        UPDATE user_data_rendition
        SET status = 'failed',
            ready_at = NULL,
            detail = detail || '{"reason": "stored as an object before 0019"}'::jsonb
        WHERE object_key IS NOT NULL AND kind IN ('text', 'pages')
        """
    )
    op.drop_column("user_data_rendition", "object_key")


def downgrade() -> None:
    op.add_column("user_data_rendition", sa.Column("object_key", sa.Text()))
    op.execute(
        """
        UPDATE user_data_rendition
        SET status = 'failed',
            ready_at = NULL,
            detail = detail || '{"reason": "downgraded from 0019, content in postgres removed"}'::jsonb
        WHERE kind IN ('text', 'pages')
        """
    )
    op.drop_table("user_data_page")
    op.drop_table("user_data_text")
