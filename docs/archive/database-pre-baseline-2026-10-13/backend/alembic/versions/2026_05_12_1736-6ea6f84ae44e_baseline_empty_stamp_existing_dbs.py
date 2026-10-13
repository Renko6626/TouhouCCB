"""baseline empty stamp existing dbs

Revision ID: 6ea6f84ae44e
Revises:
Create Date: 2026-05-12 17:36:59.836903

引入 alembic 时的 baseline revision：upgrade/downgrade 都是空 pass。

用法（一次性，每个已有环境）：
    cd backend
    alembic stamp head

这告诉 alembic「我的 DB 已经在 baseline 状态」，不会去重建已存在的表。
之后再加新的 revision，就走正常的 `alembic upgrade head` 流程。
全新 DB 跑 `alembic upgrade head` 也是 no-op（baseline 是空的），
新 DB 仍然走 init_db.py 的 create_all。

详见 docs/migrations.md。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6ea6f84ae44e'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
