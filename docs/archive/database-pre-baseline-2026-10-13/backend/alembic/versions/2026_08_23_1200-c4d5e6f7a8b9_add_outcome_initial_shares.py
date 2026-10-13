"""add outcome.initial_shares（建市场先验起手份额）

Revision ID: c4d5e6f7a8b9
Revises: a7c3e9d1f402
Create Date: 2026-08-23 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'a7c3e9d1f402'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 老市场起手份额全为 0（建市场时 total_shares 默认 0），server_default 即正确回填
    with op.batch_alter_table('outcome') as batch_op:
        batch_op.add_column(sa.Column(
            'initial_shares', sa.Numeric(precision=16, scale=6),
            nullable=False, server_default='0',
        ))


def downgrade() -> None:
    with op.batch_alter_table('outcome') as batch_op:
        batch_op.drop_column('initial_shares')
