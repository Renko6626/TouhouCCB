"""add bot_suspicion table

Revision ID: 0c44d2ded970
Revises: fd23d01dc1eb
Create Date: 2026-05-20 01:52:30.184094

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '0c44d2ded970'
down_revision: Union[str, Sequence[str], None] = 'fd23d01dc1eb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('bot_suspicion',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('triggered_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('signals', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
    sa.Column('metrics', sqlmodel.sql.sqltypes.AutoString(length=500), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('window_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('review_status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('reviewed_by', sa.Integer(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('review_note', sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
    sa.ForeignKeyConstraint(['reviewed_by'], ['user.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_bot_suspicion_triggered_at'), 'bot_suspicion', ['triggered_at'], unique=False)
    op.create_index(op.f('ix_bot_suspicion_user_id'), 'bot_suspicion', ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_bot_suspicion_user_id'), table_name='bot_suspicion')
    op.drop_index(op.f('ix_bot_suspicion_triggered_at'), table_name='bot_suspicion')
    op.drop_table('bot_suspicion')
