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

    # —— 同意依据（第四版）、账号设置、名下专家影子 ——
    op.create_table(
        "user_data_use",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_item.id"),
        ),
        sa.Column("use", sa.String(160), nullable=False),
        sa.Column(
            "scope",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("version_no", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_via", sa.String(40), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "uq_user_data_use_live",
        "user_data_use",
        ["subject", "item_id", "use"],
        unique=True,
        postgresql_where=sa.text("withdrawn_at IS NULL AND item_id IS NOT NULL"),
    )
    op.create_index(
        "uq_user_data_use_account_live",
        "user_data_use",
        ["subject", "use"],
        unique=True,
        postgresql_where=sa.text("withdrawn_at IS NULL AND item_id IS NULL"),
    )
    op.create_index(
        "ix_user_data_use_subject",
        "user_data_use",
        ["subject", "use"],
        postgresql_where=sa.text("withdrawn_at IS NULL"),
    )
    op.create_table(
        "user_data_account",
        sa.Column("subject", sa.String(255), primary_key=True),
        sa.Column(
            "default_display", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column("default_display_seen_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "user_data_expert",
        sa.Column("expert_id", sa.String(64), primary_key=True),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("latest_version", sa.String(32)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_user_data_expert_owner", "user_data_expert", ["owner_subject"])

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
    # 原来在哪个应用分区，就照原范围展示给那个应用（knowmart 的分区就是交给 knowmart）；
    # 已删的不建展示，不套「默认全展示」（SDD 0043 第六节）
    op.execute(
        """
        INSERT INTO user_data_use
            (id, subject, item_id, use, version_no, created_at, created_via)
        SELECT md5('user_data_use:' || id::text)::uuid, owner_subject, id,
               CASE app WHEN 'knowmart' THEN 'market' ELSE 'app:' || app END,
               NULL, created_at, 'migration-0010'
        FROM user_data_item
        WHERE deleted_at IS NULL AND app IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM user_data_read WHERE app IS NULL")
    op.alter_column(
        "user_data_read", "app", existing_type=sa.String(16), nullable=False
    )
    op.drop_index("ix_user_data_expert_owner", table_name="user_data_expert")
    op.drop_table("user_data_expert")
    op.drop_table("user_data_account")
    op.drop_index("ix_user_data_use_subject", table_name="user_data_use")
    op.drop_index("uq_user_data_use_account_live", table_name="user_data_use")
    op.drop_index("uq_user_data_use_live", table_name="user_data_use")
    op.drop_table("user_data_use")
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
