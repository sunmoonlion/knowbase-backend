from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import Base, TimestampMixin

_APPS = "('knowbench', 'knowlab', 'knowmart')"


class UserDataItem(TimestampMixin, Base):
    """用户资料：一份上传、代采或买来的文件。

    按「用户 × 应用」分区（SDD 0042）；内容不改。
    """

    __tablename__ = "user_data_item"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    app: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    origin_ref: Mapped[str | None] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"app IN {_APPS}", name="ck_user_data_item_app"),
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
    app: Mapped[str] = mapped_column(String(16), nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reader: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    grant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint(
            "operation IN ('owner-download', 'content', 'format', 'purchase-copy')",
            name="ck_user_data_read_operation",
        ),
        Index("ix_user_data_read_at", "at"),
        Index("ix_user_data_read_owner", "owner_subject", "app", "at"),
    )
