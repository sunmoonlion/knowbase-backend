"""User data v4: materials, content versions, renditions, holdings (SDD 0043).

Expand only. v1 keeps working on the same rows: `user_data_item` keeps its v1
columns (they cache the current version) and `app` becomes nullable because
v4 materials are not partitioned by app. Every existing row is backfilled with
its first content version and its owner's holding; rows already deleted keep
their deletion mark and get no new display (SDD 0043 section 6).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261026_0010"
down_revision = "20261025_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # —— 资料（user_data_item）加第四版的栏 ——
    op.alter_column("user_data_item", "app", existing_type=sa.String(16), nullable=True)
    op.add_column(
        "user_data_item",
        sa.Column(
            "content_origin", sa.String(16), nullable=False, server_default="upload"
        ),
    )
    op.add_column(
        "user_data_item",
        sa.Column("license", sa.String(16), nullable=False, server_default="self"),
    )
    op.add_column(
        "user_data_item",
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "user_data_item", sa.Column("disabled_at", sa.DateTime(timezone=True))
    )
    op.add_column("user_data_item", sa.Column("subject_code", sa.String(64)))
    op.add_column("user_data_item", sa.Column("doc_type", sa.String(32)))
    op.add_column("user_data_item", sa.Column("period_start", sa.Date()))
    op.add_column("user_data_item", sa.Column("period_end", sa.Date()))
    op.add_column("user_data_item", sa.Column("disclosed_on", sa.Date()))
    op.add_column("user_data_item", sa.Column("data_format", sa.String(16)))
    op.add_column(
        "user_data_item",
        sa.Column(
            "description_status",
            sa.String(16),
            nullable=False,
            server_default="unconfirmed",
        ),
    )
    op.add_column("user_data_item", sa.Column("vocab_version", sa.Integer()))
    op.create_check_constraint(
        "ck_user_data_item_content_origin",
        "user_data_item",
        "content_origin IN ('disclosure', 'collected', 'upload', 'output')",
    )
    op.create_check_constraint(
        "ck_user_data_item_license",
        "user_data_item",
        "license IN ('self', 'via_expert', 'sale')",
    )
    op.create_check_constraint(
        "ck_user_data_item_description_status",
        "user_data_item",
        "description_status IN ('unconfirmed', 'confirmed', 'checked')",
    )
    op.create_check_constraint(
        "ck_user_data_item_version", "user_data_item", "current_version >= 1"
    )

    # —— 内容版本 ——
    op.create_table(
        "user_data_version",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_item.id"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(120), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.UniqueConstraint("item_id", "version_no", name="uq_user_data_version_no"),
        sa.CheckConstraint("version_no >= 1", name="ck_user_data_version_no"),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name="ck_user_data_version_sha256"
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_user_data_version_size"),
    )

    # —— 读法版本 ——
    op.create_table(
        "user_data_rendition",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_version.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("rendition_no", sa.Integer(), nullable=False),
        sa.Column("processor", sa.String(80), nullable=False),
        sa.Column("processor_version", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("object_key", sa.Text()),
        sa.Column(
            "detail",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "version_id", "kind", "rendition_no", name="uq_user_data_rendition_no"
        ),
        sa.CheckConstraint(
            "kind IN ('text', 'pages', 'table', 'index', 'parse')",
            name="ck_user_data_rendition_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed')",
            name="ck_user_data_rendition_status",
        ),
        sa.CheckConstraint("rendition_no >= 1", name="ck_user_data_rendition_no"),
    )

    # —— 使用权 ——
    op.create_table(
        "user_data_holding",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_item.id"),
            nullable=False,
        ),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column("license", sa.String(16), nullable=False),
        sa.Column("version_no", sa.Integer()),
        sa.Column("ref", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("end_reason", sa.String(40)),
        sa.CheckConstraint(
            "basis IN ('upload', 'collect', 'purchase', 'reference', 'output')",
            name="ck_user_data_holding_basis",
        ),
        sa.CheckConstraint(
            "license IN ('self', 'via_expert', 'sale')",
            name="ck_user_data_holding_license",
        ),
    )
    op.create_index(
        "ix_user_data_holding_subject",
        "user_data_holding",
        ["subject"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index("ix_user_data_holding_item", "user_data_holding", ["item_id"])
    op.create_index(
        "uq_user_data_holding_ref",
        "user_data_holding",
        ["subject", "item_id", "ref"],
        unique=True,
        postgresql_where=sa.text("ref IS NOT NULL AND ended_at IS NULL"),
    )

    # —— 资料改动记录（描述、许可档）——
    op.create_table(
        "user_data_material_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_item.id"),
            nullable=False,
        ),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("what", sa.String(16), nullable=False),
        sa.Column("before", postgresql.JSONB(), nullable=False),
        sa.Column("after", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "what IN ('description', 'license')", name="ck_user_data_material_log_what"
        ),
    )
    op.create_index(
        "ix_user_data_material_log_item", "user_data_material_log", ["item_id", "at"]
    )
    # 第四版的读不再属于某个应用分区：app 记代办这次读的应用，可以为空
    op.alter_column("user_data_read", "app", existing_type=sa.String(16), nullable=True)

    # —— 回填：每一行一个第 1 版、所有者一条使用权 ——
    # 版本号用资料编号派生（md5 → uuid），重跑迁移不会生出两份
    op.execute(
        """
        INSERT INTO user_data_version
            (id, item_id, version_no, sha256, size_bytes, media_type, object_key,
             created_at, created_by)
        SELECT md5('user_data_version:' || id::text)::uuid, id, 1, sha256,
               size_bytes, media_type, object_key, created_at, owner_subject
        FROM user_data_item
        """
    )
    op.execute(
        """
        UPDATE user_data_item SET content_origin = CASE origin
            WHEN 'collect' THEN 'collected' ELSE 'upload' END
        """
    )
    # 买来不许转卖的，许可就是「只许本人用」（默认值已是 self）
    op.execute(
        """
        INSERT INTO user_data_holding
            (id, subject, item_id, basis, license, version_no, ref, created_at,
             ended_at, end_reason)
        SELECT md5('user_data_holding:' || id::text)::uuid, owner_subject, id,
               origin, license,
               CASE WHEN origin = 'purchase' THEN 1 END,
               CASE WHEN origin = 'purchase' THEN origin_ref END,
               created_at, deleted_at,
               CASE WHEN deleted_at IS NOT NULL THEN 'deleted' END
        FROM user_data_item
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM user_data_read WHERE app IS NULL")
    op.alter_column(
        "user_data_read", "app", existing_type=sa.String(16), nullable=False
    )
    op.drop_index("ix_user_data_material_log_item", table_name="user_data_material_log")
    op.drop_table("user_data_material_log")
    op.drop_index("uq_user_data_holding_ref", table_name="user_data_holding")
    op.drop_index("ix_user_data_holding_item", table_name="user_data_holding")
    op.drop_index("ix_user_data_holding_subject", table_name="user_data_holding")
    op.drop_table("user_data_holding")
    op.drop_table("user_data_rendition")
    op.drop_table("user_data_version")
    for name in (
        "ck_user_data_item_version",
        "ck_user_data_item_description_status",
        "ck_user_data_item_license",
        "ck_user_data_item_content_origin",
    ):
        op.drop_constraint(name, "user_data_item", type_="check")
    for column in (
        "vocab_version",
        "description_status",
        "data_format",
        "disclosed_on",
        "period_end",
        "period_start",
        "doc_type",
        "subject_code",
        "disabled_at",
        "current_version",
        "license",
        "content_origin",
    ):
        op.drop_column("user_data_item", column)
    # v4 新传的资料没有应用分区；退回 v1 之前先删掉它们（只扩不缩：正常不退）
    op.execute("DELETE FROM user_data_item WHERE app IS NULL")
    op.alter_column(
        "user_data_item", "app", existing_type=sa.String(16), nullable=False
    )
