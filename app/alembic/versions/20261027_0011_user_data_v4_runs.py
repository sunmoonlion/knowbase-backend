"""User data v4 runs: output zone, input sets, run grants, deletions (SDD 0043 1c).

Expand only. v1 grants stay in `user_data_grant`; v4 grants go to the new
`user_data_run_grant` with one row per item recording its own basis and
content owner. Input sets are content addressed (the id is the sha256 of the
canonical pins), so a pinned combination can never change.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261027_0011"
down_revision = "20261026_0010"
branch_labels = None
depends_on = None

_OLD_READ_OPERATIONS = (
    "operation IN ('owner-download', 'content', 'format', 'purchase-copy', 'check')"
)
_READ_OPERATIONS = (
    "operation IN ('owner-download', 'content', 'format', 'purchase-copy', 'check',"
    " 'rendition', 'process')"
)
_OLD_BASES = "basis IN ('upload', 'collect', 'purchase', 'reference', 'output')"
_BASES = "basis IN ('upload', 'collect', 'purchase', 'reference', 'output', 'convert')"


def upgrade() -> None:
    # —— 资料：出处（产出、转为数据）、真删的时间 ——
    op.add_column("user_data_item", sa.Column("provenance", postgresql.JSONB()))
    op.add_column("user_data_item", sa.Column("purged_at", sa.DateTime(timezone=True)))
    op.drop_constraint("ck_user_data_holding_basis", "user_data_holding", type_="check")
    op.create_check_constraint(
        "ck_user_data_holding_basis", "user_data_holding", _BASES
    )

    # —— 产出区：哪个应用替谁、哪件任务哪次运行写的；按这几样幂等 ——
    op.create_table(
        "user_data_output",
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_item.id"),
            primary_key=True,
        ),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column("caller", sa.String(64), nullable=False),
        sa.Column("task_ref", sa.String(255), nullable=False),
        sa.Column("run_ref", sa.String(255), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("evidence", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner_subject",
            "caller",
            "task_ref",
            "run_ref",
            "name",
            name="uq_user_data_output_run",
        ),
    )

    # —— 钉住的输入组合：编号就是内容摘要，只增不改 ——
    op.create_table(
        "user_data_input_set",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("pins", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id ~ '^[0-9a-f]{64}$'", name="ck_user_data_input_set_id"),
    )

    # —— 运行授权（第四版）：外层记过程与执行主体，逐项记依据与内容所有者 ——
    op.create_table(
        "user_data_run_grant",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("process", sa.String(16), nullable=False),
        sa.Column("executor_app", sa.String(16), nullable=False),
        sa.Column("beneficiary", sa.String(255), nullable=False),
        sa.Column("issued_by", sa.String(255), nullable=False),
        sa.Column("ref", sa.String(255), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reader_role", sa.String(16), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column(
            "input_set_id",
            sa.String(64),
            sa.ForeignKey("user_data_input_set.id"),
            nullable=False,
        ),
        sa.Column("platform_use", sa.String(160)),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoke_reason", sa.String(40)),
        sa.UniqueConstraint(
            "executor_app", "ref", "revision", name="uq_user_data_run_grant_revision"
        ),
        sa.CheckConstraint(
            "process IN ('test', 'delegation', 'session', 'incubation', 'platform',"
            " 'processing')",
            name="ck_user_data_run_grant_process",
        ),
        sa.CheckConstraint(
            "reader_role IN ('app', 'operator', 'machine', 'system')",
            name="ck_user_data_run_grant_reader_role",
        ),
        sa.CheckConstraint(
            "scope IN ('content', 'format')", name="ck_user_data_run_grant_scope"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_user_data_run_grant_revision"),
    )
    op.create_index(
        "ix_user_data_run_grant_beneficiary",
        "user_data_run_grant",
        ["beneficiary", "issued_at"],
    )
    op.create_index(
        "ix_user_data_run_grant_platform",
        "user_data_run_grant",
        ["beneficiary", "platform_use"],
        postgresql_where=sa.text("platform_use IS NOT NULL AND revoked_at IS NULL"),
    )
    op.create_table(
        "user_data_run_grant_item",
        sa.Column(
            "grant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_run_grant.id"),
            primary_key=True,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_item.id"),
            primary_key=True,
        ),
        sa.Column("version_no", sa.Integer(), primary_key=True),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column("basis_ref", sa.String(160), nullable=False),
        sa.Column("content_owner", sa.String(255), nullable=False),
        sa.CheckConstraint(
            "basis IN ('display', 'frozen_binding', 'platform', 'processing',"
            " 'reference')",
            name="ck_user_data_run_grant_item_basis",
        ),
    )
    op.create_index(
        "ix_user_data_run_grant_item_item", "user_data_run_grant_item", ["item_id"]
    )

    # —— 读数留痕：受益人与读的一方是谁（运营、机器、系统加工）——
    op.add_column("user_data_read", sa.Column("beneficiary", sa.String(255)))
    op.add_column("user_data_read", sa.Column("reader_role", sa.String(16)))
    op.drop_constraint("ck_user_data_read_operation", "user_data_read", type_="check")
    op.create_check_constraint(
        "ck_user_data_read_operation", "user_data_read", _READ_OPERATIONS
    )

    # —— 删除事件：写明对象、动作与原因；真删要等各处回执 ——
    op.create_table(
        "user_data_deletion",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column(
            "item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("user_data_item.id")
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column(
            "pending",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("done_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "action IN ('relation', 'disable', 'purge')",
            name="ck_user_data_deletion_action",
        ),
        sa.CheckConstraint(
            "state IN ('processing', 'done')", name="ck_user_data_deletion_state"
        ),
    )
    op.create_index(
        "ix_user_data_deletion_subject",
        "user_data_deletion",
        ["subject", "requested_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_data_deletion_subject", table_name="user_data_deletion")
    op.drop_table("user_data_deletion")
    op.execute("DELETE FROM user_data_read WHERE operation IN ('rendition', 'process')")
    op.drop_constraint("ck_user_data_read_operation", "user_data_read", type_="check")
    op.create_check_constraint(
        "ck_user_data_read_operation", "user_data_read", _OLD_READ_OPERATIONS
    )
    op.drop_column("user_data_read", "reader_role")
    op.drop_column("user_data_read", "beneficiary")
    op.drop_index(
        "ix_user_data_run_grant_item_item", table_name="user_data_run_grant_item"
    )
    op.drop_table("user_data_run_grant_item")
    op.drop_index("ix_user_data_run_grant_platform", table_name="user_data_run_grant")
    op.drop_index(
        "ix_user_data_run_grant_beneficiary", table_name="user_data_run_grant"
    )
    op.drop_table("user_data_run_grant")
    op.drop_table("user_data_input_set")
    op.drop_table("user_data_output")
    op.execute("UPDATE user_data_holding SET basis = 'upload' WHERE basis = 'convert'")
    op.drop_constraint("ck_user_data_holding_basis", "user_data_holding", type_="check")
    op.create_check_constraint(
        "ck_user_data_holding_basis", "user_data_holding", _OLD_BASES
    )
    op.drop_column("user_data_item", "purged_at")
    op.drop_column("user_data_item", "provenance")
