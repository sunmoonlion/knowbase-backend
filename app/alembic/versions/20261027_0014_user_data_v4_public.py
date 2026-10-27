"""User data v4 public originals: one material per channel document (SDD 0047 s2).

Public disclosures are materials owned by the reserved subject
`public:disclosure`; a channel document maps to exactly one material (a
changed document becomes a new content version). Run grant items gain the
basis `public` (readable by anyone while public access is open).
"""


from alembic import op

revision = "20261027_0014"
down_revision = "20261027_0013"
branch_labels = None
depends_on = None

_OLD = "basis IN ('display', 'frozen_binding', 'platform', 'processing', 'reference')"
_NEW = (
    "basis IN ('display', 'frozen_binding', 'platform', 'processing', 'reference',"
    " 'public')"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX uq_user_data_item_public_document
        ON user_data_item
            ((provenance ->> 'channel'), (provenance ->> 'channel_doc_id'))
        WHERE owner_subject = 'public:disclosure'
        """
    )
    op.drop_constraint(
        "ck_user_data_run_grant_item_basis", "user_data_run_grant_item", type_="check"
    )
    op.create_check_constraint(
        "ck_user_data_run_grant_item_basis", "user_data_run_grant_item", _NEW
    )


def downgrade() -> None:
    op.execute("DELETE FROM user_data_run_grant_item WHERE basis = 'public'")
    op.drop_constraint(
        "ck_user_data_run_grant_item_basis", "user_data_run_grant_item", type_="check"
    )
    op.create_check_constraint(
        "ck_user_data_run_grant_item_basis", "user_data_run_grant_item", _OLD
    )
    op.execute("DROP INDEX uq_user_data_item_public_document")
