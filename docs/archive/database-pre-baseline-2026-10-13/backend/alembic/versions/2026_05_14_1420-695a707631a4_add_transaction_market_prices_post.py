"""add transaction.market_prices_post JSON column for chart fast path

Revision ID: 695a707631a4
Revises: 6ea6f84ae44e
Create Date: 2026-05-14 14:20:00.000000

新增 Transaction.market_prices_post（JSON / nullable）：buy/sell 写入全市场 post 价快照
（list[float]，按 outcome.id 升序），chart 接口读取范围内全部非 NULL 时走 fast path，
任一 NULL 退回 NumPy 重放。settle/settle_lose 留 NULL；老历史数据由回填脚本 backfill_market_prices_post.py 补齐。

SQLite ALTER 支持差，但 ADD COLUMN NULLABLE 是 SQLite 也支持的最简形式，无需 batch_alter_table。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '695a707631a4'
down_revision: Union[str, Sequence[str], None] = '6ea6f84ae44e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'transaction',
        sa.Column('market_prices_post', sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('transaction', 'market_prices_post')
