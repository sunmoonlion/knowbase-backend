"""删旧 SQLite 数据集登记与用户资料 v1 的同意依据、运行授权（decisions ⑰：不留旧版兼容）。

- `knowledge_dataset`（0007）：knowfeed 建的 SQLite 数据集向这里登记的版本。旧知识 MCP
  （`/api/mcp/knowledge`）、管理端数据目录与登记页都删了，没有人再读它；
- `user_data_consent`、`user_data_consent_item`、`user_data_grant`、`user_data_grant_item`
  （0008）：`user-data/v1` 的同意依据与运行授权。第四版的运行授权在
  `user_data_run_grant`（0011），同意依据是 `user_data_use`，都不引这几张表。

留着的：`user_data_item`、`user_data_read`（第四版照用，`app` 两栏也照用：资料那栏是
v1 时代旧行的应用分区，第四版展示成 `legacy_app`；读数那栏是代办这次读的应用）。
读数留痕里 v1 时代的行照留（只记编号，`grant_id` 指的那张表没了，不设外键，不影响）。

downgrade 建回 0017 时的空表、索引与约束；删掉的行不恢复。

Revision ID: 20261028_0018
Revises: 20261028_0017
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261028_0018"
down_revision = "20261028_0017"
branch_labels = None
depends_on = None

APPS = "('knowbench', 'knowlab', 'knowmart')"
# 先删引用别人的
TABLES = (
    "user_data_grant_item",
    "user_data_grant",
    "user_data_consent_item",
    "user_data_consent",
    "knowledge_dataset",
)


def upgrade() -> None:
    for table in TABLES:
        op.drop_table(table)


def _uuid(name: str, *args, **kwargs) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, **kwargs)


def _stamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def _pins(table: str, parent: str, key: str) -> None:
    """同意依据、授权钉住的那几份资料：编号加摘要（0008 的样子）。"""
    op.create_table(
        table,
        _uuid(
            key,
            sa.ForeignKey(f"{parent}.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        _uuid("item_id", sa.ForeignKey("user_data_item.id"), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
    )
    op.create_index(f"ix_{table}_item", table, ["item_id"])


def downgrade() -> None:
    """建回 0017 时的空表（0007、0008 建的样子）；删掉的行不恢复。"""
    op.create_table(
        "knowledge_dataset",
        _uuid("id", primary_key=True, server_default=sa.text("gen_random_uuid()")),
        *_stamps(),
        sa.Column("dataset_id", sa.String(80), nullable=False),
        sa.Column("data_version", sa.String(160), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("security_code", sa.String(6)),
        sa.Column("bucket", sa.String(63), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("object_version_id", sa.String(255)),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("start_date", sa.String(10), nullable=False),
        sa.Column("end_date", sa.String(10), nullable=False),
        sa.Column("source_app", sa.String(80), nullable=False),
        sa.Column("source_ref", sa.String(255)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("registered_by", sa.String(255), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "dataset_id", "data_version", name="uq_knowledge_dataset_version"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded')", name="ck_knowledge_dataset_status"
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name="ck_knowledge_dataset_sha256"
        ),
    )
    op.create_index(
        "uq_knowledge_dataset_one_active",
        "knowledge_dataset",
        ["dataset_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "user_data_consent",
        _uuid("id", primary_key=True),
        *_stamps(),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column("app", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(80), nullable=False),
        sa.Column("ref", sa.String(255), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(f"app IN {APPS}", name="ck_user_data_consent_app"),
    )
    _pins("user_data_consent_item", "user_data_consent", "consent_id")

    op.create_table(
        "user_data_grant",
        _uuid("id", primary_key=True),
        *_stamps(),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column("app", sa.String(16), nullable=False),
        sa.Column("reader_app", sa.String(16), nullable=False),
        sa.Column("ref", sa.String(255), nullable=False),
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("issued_by", sa.String(255), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _uuid("consent_id", sa.ForeignKey("user_data_consent.id")),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('test', 'delegation', 'session', 'incubation')",
            name="ck_user_data_grant_kind",
        ),
        sa.CheckConstraint(f"app IN {APPS}", name="ck_user_data_grant_app"),
        sa.CheckConstraint(f"reader_app IN {APPS}", name="ck_user_data_grant_reader"),
        sa.CheckConstraint(
            "scope IN ('content', 'format')", name="ck_user_data_grant_scope"
        ),
        sa.CheckConstraint("expires_at > issued_at", name="ck_user_data_grant_expiry"),
    )
    _pins("user_data_grant_item", "user_data_grant", "grant_id")
