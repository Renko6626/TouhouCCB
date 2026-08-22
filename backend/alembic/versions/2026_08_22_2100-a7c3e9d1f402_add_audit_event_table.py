"""add audit_event table

Revision ID: a7c3e9d1f402
Revises: b2cd21122925
Create Date: 2026-08-22 21:00:00

审计事件流：docs/superpowers/specs/2026-08-22-audit-events-design.md
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = 'a7c3e9d1f402'
down_revision: Union[str, Sequence[str], None] = 'b2cd21122925'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('audit_event',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('event_type', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('market_id', sa.Integer(), nullable=True),
    sa.Column('outcome_id', sa.Integer(), nullable=True),
    sa.Column('operator_user_id', sa.Integer(), nullable=True),
    sa.Column('ref_table', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=True),
    sa.Column('ref_id', sa.Integer(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('user_after', sa.JSON(), nullable=True),
    sa.Column('position_after', sa.JSON(), nullable=True),
    sa.Column('market_after', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_event', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_event_ts'), ['ts'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_event_event_type'), ['event_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_event_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_event_market_id'), ['market_id'], unique=False)
        batch_op.create_index('ix_audit_event_user_id_id', ['user_id', 'id'], unique=False)
        batch_op.create_index('ix_audit_event_market_id_id', ['market_id', 'id'], unique=False)
        batch_op.create_index('ix_audit_event_type_id', ['event_type', 'id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('audit_event', schema=None) as batch_op:
        batch_op.drop_index('ix_audit_event_type_id')
        batch_op.drop_index('ix_audit_event_market_id_id')
        batch_op.drop_index('ix_audit_event_user_id_id')
        batch_op.drop_index(batch_op.f('ix_audit_event_market_id'))
        batch_op.drop_index(batch_op.f('ix_audit_event_user_id'))
        batch_op.drop_index(batch_op.f('ix_audit_event_event_type'))
        batch_op.drop_index(batch_op.f('ix_audit_event_ts'))

    op.drop_table('audit_event')
