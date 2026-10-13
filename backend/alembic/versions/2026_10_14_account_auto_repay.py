"""Account preference for ordinary sale proceeds repayment."""
from alembic import op
import sqlalchemy as sa

revision = 'account_auto_repay_20261014'
down_revision = 'fx_trigger_orders_20261014'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('user', sa.Column('auto_repay', sa.Boolean(), nullable=False,
                                    server_default=sa.true()))


def downgrade():
    op.drop_column('user', 'auto_repay')
