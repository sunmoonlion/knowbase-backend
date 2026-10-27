from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import Base, TimestampMixin

_APPS = "('knowbench', 'knowlab', 'knowmart')"


class UserDataItem(TimestampMixin, Base):
    """用户资料：一份上传、代采或买来的文件。

    v1 按「用户 × 应用」分区（SDD 0042）；第四版（SDD 0043）里一行就是一份资料：
    不按应用分区（`app` 只留给 v1 时代的旧行），内容版本另记在 `user_data_version`，
    这里的 sha256、object_key、size_bytes、media_type 缓存当前版本给 v1 的读法用。
    """

    __tablename__ = "user_data_item"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    app: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    origin_ref: Mapped[str | None] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 买来、卖家写明不许转卖的（Fable 评审 P2-16）
    no_resale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # —— 第四版（SDD 0043 第 2.1、2.5 节）——
    content_origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default="upload"
    )
    license: Mapped[str] = mapped_column(String(16), nullable=False, default="self")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subject_code: Mapped[str | None] = mapped_column(String(64))
    doc_type: Mapped[str | None] = mapped_column(String(32))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    disclosed_on: Mapped[date | None] = mapped_column(Date)
    data_format: Mapped[str | None] = mapped_column(String(16))
    description_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unconfirmed"
    )
    vocab_version: Mapped[int | None] = mapped_column(Integer)
    # 出处：产出（哪个应用、哪件任务、哪次运行、用了哪组输入）、
    # 转为数据（从哪份产出转来）
    provenance: Mapped[dict | None] = mapped_column(JSONB)
    # 依法清除：内容、读法、描述都删了，只留编号级留痕
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"app IN {_APPS}", name="ck_user_data_item_app"),
        Index(
            "ix_user_data_item_description",
            "subject_code",
            "doc_type",
            "period_end",
            postgresql_where=text("subject_code IS NOT NULL"),
        ),
        CheckConstraint(
            "content_origin IN ('disclosure', 'collected', 'upload', 'output')",
            name="ck_user_data_item_content_origin",
        ),
        CheckConstraint(
            "license IN ('self', 'via_expert', 'sale')",
            name="ck_user_data_item_license",
        ),
        CheckConstraint(
            "description_status IN ('unconfirmed', 'confirmed', 'checked')",
            name="ck_user_data_item_description_status",
        ),
        CheckConstraint("current_version >= 1", name="ck_user_data_item_version"),
        CheckConstraint(
            "origin IN ('upload', 'collect', 'purchase')",
            name="ck_user_data_item_origin",
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_user_data_item_sha256"),
        CheckConstraint("size_bytes > 0", name="ck_user_data_item_size"),
        Index(
            "ix_user_data_item_owner",
            "owner_subject",
            "app",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # 同一笔成交、同一次代采只落一份
        Index(
            "uq_user_data_item_origin",
            "owner_subject",
            "app",
            "origin_ref",
            unique=True,
            postgresql_where=text("origin_ref IS NOT NULL"),
        ),
    )


class UserDataConsent(TimestampMixin, Base):
    """同意依据：用户本人留下「这几份可用于某件事」（数据拓扑第 5.2 节）。"""

    __tablename__ = "user_data_consent"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    app: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(String(80), nullable=False)
    ref: Mapped[str] = mapped_column(String(255), nullable=False)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"app IN {_APPS}", name="ck_user_data_consent_app"),
    )


class UserDataConsentItem(Base):
    __tablename__ = "user_data_consent_item"

    consent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_data_consent.id", ondelete="CASCADE"),
        primary_key=True,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_data_item.id"), primary_key=True
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (Index("ix_user_data_consent_item_item", "item_id"),)


class UserDataGrant(TimestampMixin, Base):
    """运行授权：某一次测试、委托、会话、孵化可读哪几份；到期或撤销就失效。"""

    __tablename__ = "user_data_grant"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    app: Mapped[str] = mapped_column(String(16), nullable=False)
    reader_app: Mapped[str] = mapped_column(String(16), nullable=False)
    ref: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[str] = mapped_column(String(8), nullable=False)
    issued_by: Mapped[str] = mapped_column(String(255), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_data_consent.id")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "kind IN ('test', 'delegation', 'session', 'incubation')",
            name="ck_user_data_grant_kind",
        ),
        CheckConstraint(f"app IN {_APPS}", name="ck_user_data_grant_app"),
        CheckConstraint(f"reader_app IN {_APPS}", name="ck_user_data_grant_reader"),
        CheckConstraint(
            "scope IN ('content', 'format')", name="ck_user_data_grant_scope"
        ),
        CheckConstraint("expires_at > issued_at", name="ck_user_data_grant_expiry"),
    )


class UserDataGrantItem(Base):
    __tablename__ = "user_data_grant_item"

    grant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_data_grant.id", ondelete="CASCADE"),
        primary_key=True,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_data_item.id"), primary_key=True
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (Index("ix_user_data_grant_item_item", "item_id"),)


class UserDataRead(Base):
    """读数留痕：谁、凭哪条授权、读了哪一份；只记编号不记内容，留一年。"""

    __tablename__ = "user_data_read"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    # v1：资料所在的应用分区；第四版：代办这次读的应用（资料不再分区）
    app: Mapped[str | None] = mapped_column(String(16))
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reader: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    # v1 的授权或第四版的运行授权
    grant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # 第四版：受益人（请专家的人；专家绑定的数据所有者是作者）与读的一方是谁
    beneficiary: Mapped[str | None] = mapped_column(String(255))
    reader_role: Mapped[str | None] = mapped_column(String(16))

    __table_args__ = (
        CheckConstraint(
            "operation IN ('owner-download', 'content', 'format', 'purchase-copy',"
            " 'check', 'rendition', 'process')",
            name="ck_user_data_read_operation",
        ),
        Index("ix_user_data_read_at", "at"),
        Index("ix_user_data_read_owner", "owner_subject", "app", "at"),
    )


class UserDataVersion(Base):
    """资料的一个内容版本：内容的一次替换（SDD 0043 第 2.1 节），只增不改。"""

    __tablename__ = "user_data_version"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_data_item.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        UniqueConstraint("item_id", "version_no", name="uq_user_data_version_no"),
        CheckConstraint("version_no >= 1", name="ck_user_data_version_no"),
        CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name="ck_user_data_version_sha256"
        ),
        CheckConstraint("size_bytes > 0", name="ck_user_data_version_size"),
    )


class UserDataRendition(Base):
    """读法版本：某个内容版本的一种派生形态；加工器升级、质检改标都是新的一版。"""

    __tablename__ = "user_data_rendition"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_data_version.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    rendition_no: Mapped[int] = mapped_column(Integer, nullable=False)
    processor: Mapped[str] = mapped_column(String(80), nullable=False)
    processor_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    object_key: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "version_id", "kind", "rendition_no", name="uq_user_data_rendition_no"
        ),
        CheckConstraint(
            "kind IN ('text', 'pages', 'table', 'index', 'parse')",
            name="ck_user_data_rendition_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'ready', 'failed')",
            name="ck_user_data_rendition_status",
        ),
        CheckConstraint("rendition_no >= 1", name="ck_user_data_rendition_no"),
    )


class UserDataHolding(Base):
    """使用权：某个用户凭什么（取得依据）能怎么用一份资料（SDD 0043 第 2.2 节）。"""

    __tablename__ = "user_data_holding"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_data_item.id"), nullable=False
    )
    basis: Mapped[str] = mapped_column(String(16), nullable=False)
    license: Mapped[str] = mapped_column(String(16), nullable=False)
    version_no: Mapped[int | None] = mapped_column(Integer)
    ref: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(40))

    __table_args__ = (
        CheckConstraint(
            "basis IN ('upload', 'collect', 'purchase', 'reference', 'output',"
            " 'convert')",
            name="ck_user_data_holding_basis",
        ),
        CheckConstraint(
            "license IN ('self', 'via_expert', 'sale')",
            name="ck_user_data_holding_license",
        ),
        Index(
            "ix_user_data_holding_subject",
            "subject",
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index("ix_user_data_holding_item", "item_id"),
        # 同一笔交易、同一条需求依据只建一条有效使用权
        Index(
            "uq_user_data_holding_ref",
            "subject",
            "item_id",
            "ref",
            unique=True,
            postgresql_where=text("ref IS NOT NULL AND ended_at IS NULL"),
        ),
    )


class UserDataMaterialLog(Base):
    """资料的改动记录：描述、许可档改了都留痕（SDD 0043 第 2.5 节、第三轮评审 K 7）。

    改描述不产生新的内容版本；许可档改高要记下本人勾了「我有权」。
    """

    __tablename__ = "user_data_material_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_data_item.id"), nullable=False
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    what: Mapped[str] = mapped_column(String(16), nullable=False)
    before: Mapped[dict] = mapped_column(JSONB, nullable=False)
    after: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "what IN ('description', 'license')", name="ck_user_data_material_log_what"
        ),
        Index("ix_user_data_material_log_item", "item_id", "at"),
    )


class UserDataUse(Base):
    """同意依据（第四版）：用户本人同意一份资料给哪种用途（SDD 0043 第 2.3 节）。

    `use` 的写法：`app:knowbench`、`app:knowlab`（展示给这个应用）、`market`（交给
    knowmart 待上架）、`expert:<专家>`（绑给名下这位专家，下一版用）、
    `expert:<专家>@<版本>`（交付时冻结）、`platform:<应用>:<过程>:<过程编号>`、
    `subscription:<订阅>@<修订>`、`processing:<内容版本>`。
    按资料的那几种 `item_id` 有值（平台过程每份一行）；订阅这类按范围的 `item_id`
    为空、范围在 `scope`。
    """

    __tablename__ = "user_data_use"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_data_item.id")
    )
    use: Mapped[str] = mapped_column(String(160), nullable=False)
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    version_no: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_via: Mapped[str] = mapped_column(String(40), nullable=False)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "uq_user_data_use_live",
            "subject",
            "item_id",
            "use",
            unique=True,
            postgresql_where=text("withdrawn_at IS NULL AND item_id IS NOT NULL"),
        ),
        Index(
            "uq_user_data_use_account_live",
            "subject",
            "use",
            unique=True,
            postgresql_where=text("withdrawn_at IS NULL AND item_id IS NULL"),
        ),
        Index(
            "ix_user_data_use_subject",
            "subject",
            "use",
            postgresql_where=text("withdrawn_at IS NULL"),
        ),
    )


class UserDataAccount(Base):
    """账号级设置：新数据默认展示给全部应用（SDD 0043 第 2.3 节）。

    `default_display_seen_at` 是用户第一次进 knowbase web端看过这个开关、
    点了一下的时间：
    点过才算同意依据，没点过的不自动展示。
    """

    __tablename__ = "user_data_account"

    subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    default_display: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_display_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class UserDataExpert(Base):
    """名下专家的只读影子：knowlab 建版本时推来，绑定时只认这里有的。

    SDD 0044 第三节。
    """

    __tablename__ = "user_data_expert"

    expert_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    latest_version: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (Index("ix_user_data_expert_owner", "owner_subject"),)


class UserDataOutput(Base):
    """产出区的一份：哪个应用替谁、哪件任务哪次运行写的（SDD 0043 第 2.6 节）。"""

    __tablename__ = "user_data_output"

    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_data_item.id"), primary_key=True
    )
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    caller: Mapped[str] = mapped_column(String(64), nullable=False)
    task_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    run_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 测试证据：只增不改
    evidence: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "owner_subject",
            "caller",
            "task_ref",
            "run_ref",
            "name",
            name="uq_user_data_output_run",
        ),
    )


class UserDataInputSet(Base):
    """钉住的输入组合：内容版本、读法版本、各自摘要（第 2.9 节）。

    编号就是组合的摘要。
    """

    __tablename__ = "user_data_input_set"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    pins: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint("id ~ '^[0-9a-f]{64}$'", name="ck_user_data_input_set_id"),
    )


class UserDataRunGrant(Base):
    """运行授权（第四版）：外层记过程、执行主体、受益人（SDD 0043 第 2.7 节）。

    同一件事（执行应用 + `ref`）运行中追加的数据新签一条，`revision` 往上加，
    已签的不扩大。平台过程的授权记下凭的是哪条 `platform:` 依据，撤回时一并撤。
    """

    __tablename__ = "user_data_run_grant"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    process: Mapped[str] = mapped_column(String(16), nullable=False)
    executor_app: Mapped[str] = mapped_column(String(16), nullable=False)
    beneficiary: Mapped[str] = mapped_column(String(255), nullable=False)
    issued_by: Mapped[str] = mapped_column(String(255), nullable=False)
    ref: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reader_role: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    input_set_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("user_data_input_set.id"), nullable=False
    )
    platform_use: Mapped[str | None] = mapped_column(String(160))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(String(40))

    __table_args__ = (
        UniqueConstraint(
            "executor_app", "ref", "revision", name="uq_user_data_run_grant_revision"
        ),
        CheckConstraint(
            "process IN ('test', 'delegation', 'session', 'incubation', 'platform',"
            " 'processing')",
            name="ck_user_data_run_grant_process",
        ),
        CheckConstraint(
            "reader_role IN ('app', 'operator', 'machine', 'system')",
            name="ck_user_data_run_grant_reader_role",
        ),
        CheckConstraint(
            "scope IN ('content', 'format')", name="ck_user_data_run_grant_scope"
        ),
        CheckConstraint("revision >= 1", name="ck_user_data_run_grant_revision"),
        Index("ix_user_data_run_grant_beneficiary", "beneficiary", "issued_at"),
        Index(
            "ix_user_data_run_grant_platform",
            "beneficiary",
            "platform_use",
            postgresql_where=text("platform_use IS NOT NULL AND revoked_at IS NULL"),
        ),
    )


class UserDataRunGrantItem(Base):
    """授权的一项：哪份资料哪一版，凭什么依据，内容归谁。"""

    __tablename__ = "user_data_run_grant_item"

    grant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_data_run_grant.id"), primary_key=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_data_item.id"), primary_key=True
    )
    version_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    basis: Mapped[str] = mapped_column(String(16), nullable=False)
    basis_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    content_owner: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "basis IN ('display', 'frozen_binding', 'platform', 'processing',"
            " 'reference', 'public')",
            name="ck_user_data_run_grant_item_basis",
        ),
        Index("ix_user_data_run_grant_item_item", "item_id"),
    )


class UserDataDeletion(Base):
    """删除事件：对象、动作、原因；真删要等各处回执（SDD 0043 第 2.8 节）。

    `relation`：撤某个人的关系（不删仍被别人合法引用的内容）；`disable`：禁用、
    违规处置；`purge`：注销、依法删除，`pending` 列还在等谁的回执（如 RAGFlow）。
    """

    __tablename__ = "user_data_deletion"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_data_item.id")
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(80), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    pending: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "action IN ('relation', 'disable', 'purge')",
            name="ck_user_data_deletion_action",
        ),
        CheckConstraint(
            "state IN ('processing', 'done')", name="ck_user_data_deletion_state"
        ),
        Index("ix_user_data_deletion_subject", "subject", "requested_at"),
    )


class UserDataResolution(Base):
    """解析清单（SDD 0043 第 3.2 节）：每个修订一行，运行中冒出的新需求出新修订。"""

    __tablename__ = "user_data_resolution"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    app: Mapped[str] = mapped_column(String(16), nullable=False)
    consumer: Mapped[str] = mapped_column(String(160), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(40), nullable=False)
    vocab_version: Mapped[int] = mapped_column(Integer, nullable=False)
    task_ref: Mapped[str | None] = mapped_column(String(255))
    needs: Mapped[list] = mapped_column(JSONB, nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    items: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_user_data_resolution_revision"),
        Index("ix_user_data_resolution_subject", "subject", "created_at"),
    )


class UserDataConfirmation(Base):
    """确认：清单修订、任务、声明快照、确认的条目、供给条件快照；7 天有效。"""

    __tablename__ = "user_data_confirmation"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    app: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    task_ref: Mapped[str | None] = mapped_column(String(255))
    resolution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resolution_revision: Mapped[int | None] = mapped_column(Integer)
    needs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    items: Mapped[list] = mapped_column(JSONB, nullable=False)
    supply: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('description', 'url', 'topic')",
            name="ck_user_data_confirmation_kind",
        ),
        Index("ix_user_data_confirmation_subject", "subject", "expires_at"),
    )


class UserDataPreference(Base):
    """偏好：用户 × 使用者 × 声明契约版本 × 需求 key × 规范化的任务参数 → 资料。"""

    __tablename__ = "user_data_preference"

    subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    consumer: Mapped[str] = mapped_column(String(160), primary_key=True)
    contract_version: Mapped[str] = mapped_column(String(40), primary_key=True)
    need_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    params_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    vocab_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class UserDataSession(Base):
    """会话登记（SDD 0048 第五节）：本人经应用登记一次，模型首读一份时凭它签小授权。"""

    __tablename__ = "user_data_session"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    executor_app: Mapped[str] = mapped_column(String(16), nullable=False)
    # 范围：他放开给这个应用的（`app:<应用>`），外加公开原件（开关开着时）
    scope_use: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_user_data_session_subject", "subject", "created_at"),)
