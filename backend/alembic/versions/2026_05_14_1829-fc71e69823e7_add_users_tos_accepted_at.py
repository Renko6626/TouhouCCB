"""add users.tos_accepted_at

Revision ID: fc71e69823e7
Revises: 695a707631a4
Create Date: 2026-05-14 18:29:40.263326

新增 User.tos_accepted_at（DateTime(tz) / nullable）：用户在 TosModal
勾选「我已阅读并同意」时由 POST /users/me/accept-tos 写入 now()。
默认 NULL 表示尚未同意——老用户首次登录后会被前端阻塞性弹窗强制补勾一次。

SQLite ALTER 支持差，但 ADD COLUMN NULLABLE 是 SQLite 也支持的最简形式，无需 batch_alter_table。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fc71e69823e7'
down_revision: Union[str, Sequence[str], None] = '695a707631a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user',
        sa.Column('tos_accepted_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('user', 'tos_accepted_at')
