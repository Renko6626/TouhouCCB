"""Paid Wavelength audience predictions."""
from alembic import op
import sqlalchemy as sa

revision = 'wavelength_predictions_20261019'
down_revision = 'fx_announcements_20261016'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('wavelength_round',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('round_key', sa.String(128), nullable=False, unique=True),
        sa.Column('game_key', sa.String(128), nullable=False),
        sa.Column('round_number', sa.Integer(), nullable=False),
        sa.Column('left_label', sa.String(200), nullable=False),
        sa.Column('right_label', sa.String(200), nullable=False),
        sa.Column('team_name', sa.String(100), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('rules', sa.JSON(), nullable=False),
        sa.Column('target_position', sa.Integer()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True)),
        sa.Column('finished_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('open', 'closed', 'settled', 'cancelled')", name='ck_wavelength_round_status'),
        sa.CheckConstraint('target_position IS NULL OR target_position BETWEEN 0 AND 1000', name='ck_wavelength_target'))
    op.create_table('wavelength_prediction',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('round_id', sa.Integer(), sa.ForeignKey('wavelength_round.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('stake', sa.Numeric(16, 6), nullable=False),
        sa.Column('distance', sa.Integer()),
        sa.Column('payout', sa.Numeric(16, 6)),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('round_id', 'user_id', name='uq_wavelength_prediction_round_user'),
        sa.CheckConstraint('position BETWEEN 0 AND 1000', name='ck_wavelength_prediction_position'))
    op.create_index('ix_wavelength_prediction_user_id', 'wavelength_prediction', ['user_id'])


def downgrade():
    op.drop_table('wavelength_prediction')
    op.drop_table('wavelength_round')
