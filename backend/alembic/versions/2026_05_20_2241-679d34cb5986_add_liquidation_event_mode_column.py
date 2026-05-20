"""add liquidation_event mode column

Revision ID: 679d34cb5986
Revises: 0c44d2ded970
Create Date: 2026-05-20 22:41:15.978602

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = '679d34cb5986'
down_revision: Union[str, Sequence[str], None] = '0c44d2ded970'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'liquidation_events',
        sa.Column(
            'mode',
            sqlmodel.sql.sqltypes.AutoString(length=20),
            nullable=False,
            server_default='emergency',
        ),
    )


def downgrade() -> None:
    op.drop_column('liquidation_events', 'mode')
