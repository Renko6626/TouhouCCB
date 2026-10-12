"""Link atomic net requests to their real trades, retaining legacy identity."""
from alembic import op
import sqlalchemy as sa

revision = 'fx_net_execution_20261011'
down_revision = 'fx_candle_pre_price_20261011'
branch_labels = None
depends_on = None

LINK = "(execution_id IS NULL AND execution_leg IS NULL) OR (execution_id IS NOT NULL AND execution_leg IS NOT NULL AND execution_leg IN (1,2) AND idempotency_key IS NULL)"


def upgrade():
    op.create_table('fx_execution',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('pair_id', sa.Integer(), sa.ForeignKey('fx_pair.id'), nullable=False),
        sa.Column('idempotency_key', sa.String(128), nullable=False),
        sa.Column('request_payload', sa.JSON(), nullable=False),
        sa.Column('response_version', sa.Integer(), nullable=False),
        sa.Column('response_payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'idempotency_key', name='uq_fx_execution_user_key'))
    with op.batch_alter_table('fx_trade') as batch:
        batch.add_column(sa.Column('execution_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('execution_leg', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_fx_trade_execution', 'fx_execution', ['execution_id'], ['id'])
        batch.create_unique_constraint('uq_fx_trade_execution_leg', ['execution_id', 'execution_leg'])
        batch.create_check_constraint('ck_fx_trade_execution_link', LINK)
        batch.create_index('ix_fx_trade_execution_id', ['execution_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM fx_execution LIMIT 1')).first():
        raise RuntimeError('net FX request history must be preserved; downgrade refused')
    with op.batch_alter_table('fx_trade') as batch:
        batch.drop_index('ix_fx_trade_execution_id')
        batch.drop_constraint('ck_fx_trade_execution_link', type_='check')
        batch.drop_constraint('uq_fx_trade_execution_leg', type_='unique')
        batch.drop_constraint('fk_fx_trade_execution', type_='foreignkey')
        batch.drop_column('execution_leg')
        batch.drop_column('execution_id')
    op.drop_table('fx_execution')
