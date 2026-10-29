"""资料的改动留痕多记一种：上传时的识别（SDD 0044 第 2.3 节、0049 第六节）。

上传时 knowbase 按文件名与首页识别一次描述，把识别规则的版本号与认出的值记进
`user_data_material_log`（`what = 'recognized'`），以后能查出是按哪一版规则猜的；
改规则不回头改已识别过的描述。

downgrade 删掉这类留痕再收紧约束。

Revision ID: 20261029_0020
Revises: 20261029_0019
"""

from __future__ import annotations

from alembic import op

revision = "20261029_0020"
down_revision = "20261029_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_user_data_material_log_what", "user_data_material_log", type_="check"
    )
    op.create_check_constraint(
        "ck_user_data_material_log_what",
        "user_data_material_log",
        "what IN ('description', 'license', 'recognized')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM user_data_material_log WHERE what = 'recognized'")
    op.drop_constraint(
        "ck_user_data_material_log_what", "user_data_material_log", type_="check"
    )
    op.create_check_constraint(
        "ck_user_data_material_log_what",
        "user_data_material_log",
        "what IN ('description', 'license')",
    )
