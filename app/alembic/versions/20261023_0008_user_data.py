"""User data per user x app: items, consents, run grants, read log (SDD 0042)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261023_0008"
down_revision = "20260927_0007"
branch_labels = None
depends_on = None

APPS = "('knowbench', 'knowlab', 'knowmart')"


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade():
    op.create_table(
        "user_data_item",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column("app", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("media_type", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("origin_ref", sa.String(255)),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(f"app IN {APPS}", name="ck_user_data_item_app"),
        sa.CheckConstraint(
            "origin IN ('upload', 'collect', 'purchase')",
            name="ck_user_data_item_origin",
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name="ck_user_data_item_sha256"
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_user_data_item_size"),
    )
    op.create_index(
        "ix_user_data_item_owner",
        "user_data_item",
        ["owner_subject", "app"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_user_data_item_origin",
        "user_data_item",
        ["owner_subject", "app", "origin_ref"],
        unique=True,
        postgresql_where=sa.text("origin_ref IS NOT NULL"),
    )

    op.create_table(
        "user_data_consent",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column("app", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(80), nullable=False),
        sa.Column("ref", sa.String(255), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(f"app IN {APPS}", name="ck_user_data_consent_app"),
    )
    op.create_table(
        "user_data_consent_item",
        sa.Column(
            "consent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_consent.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_item.id"),
            primary_key=True,
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
    )
    op.create_index(
        "ix_user_data_consent_item_item", "user_data_consent_item", ["item_id"]
    )

    op.create_table(
        "user_data_grant",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column("app", sa.String(16), nullable=False),
        sa.Column("reader_app", sa.String(16), nullable=False),
        sa.Column("ref", sa.String(255), nullable=False),
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("issued_by", sa.String(255), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "consent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_consent.id"),
        ),
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
    op.create_table(
        "user_data_grant_item",
        sa.Column(
            "grant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_grant.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_data_item.id"),
            primary_key=True,
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
    )
    op.create_index("ix_user_data_grant_item_item", "user_data_grant_item", ["item_id"])

    op.create_table(
        "user_data_read",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column("app", sa.String(16), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("reader", sa.String(255), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True)),
        sa.CheckConstraint(
            "operation IN ('owner-download', 'content', 'format', 'purchase-copy')",
            name="ck_user_data_read_operation",
        ),
    )
    op.create_index("ix_user_data_read_at", "user_data_read", ["at"])
    op.create_index(
        "ix_user_data_read_owner", "user_data_read", ["owner_subject", "app", "at"]
    )


def downgrade():
    op.drop_table("user_data_read")
    op.drop_table("user_data_grant_item")
    op.drop_table("user_data_grant")
    op.drop_table("user_data_consent_item")
    op.drop_table("user_data_consent")
    op.drop_table("user_data_item")
