"""Durable cancellation of not-yet-created audience rounds."""
from alembic import op
import sqlalchemy as sa

revision = 'returned_cancel_20261023'
down_revision = 'wavelength_predictions_20261019'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('wavelength_round_guard',
        sa.Column('round_key', sa.String(128), primary_key=True),
        sa.Column('cancelled', sa.Boolean(), nullable=False))


def downgrade():
    op.drop_table('wavelength_round_guard')
