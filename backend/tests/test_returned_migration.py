"""Upgrade/downgrade preserves prior money and creates usable prediction tables."""
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.models.base import User
from app.models.returned import ReturnedPrediction, ReturnedRound, ReturnedRoundGuard


def test_prediction_migration_preserves_accounts_and_round_trips(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path}/returned-migration.db')
    cfg = Config(str(Path(__file__).parents[1] / 'alembic.ini'))
    with engine.begin() as connection:
        cfg.attributes['connection'] = connection
        command.upgrade(cfg, 'fx_announcements_20261016')
    with Session(engine) as db:
        user = User(username='existing', cash=Decimal('123.456789'))
        db.add(user)
        db.commit()
        uid = user.id
    with engine.begin() as connection:
        cfg.attributes['connection'] = connection
        command.upgrade(cfg, 'wavelength_predictions_20261019')
    with Session(engine) as db:
        round = ReturnedRound(round_key='migrated', game_key='game', round_number=1,
                                left_label='left', right_label='right', rules={'stake': '100'})
        db.add(round)
        db.flush()
        db.add(ReturnedPrediction(round_id=round.id, user_id=uid, position=500, stake=Decimal('100')))
        db.commit()
        assert db.get(User, uid).cash == Decimal('123.456789')
        assert inspect(engine).get_unique_constraints('wavelength_prediction')[0]['column_names'] == ['round_id', 'user_id']
    with engine.begin() as connection:
        cfg.attributes['connection'] = connection
        command.upgrade(cfg, 'returned_cancel_20261023')
    with Session(engine) as db:
        db.add(ReturnedRoundGuard(round_key='never-created', cancelled=True))
        db.commit()
        assert db.get(ReturnedRoundGuard, 'never-created').cancelled
        assert db.get(User, uid).cash == Decimal('123.456789')
    with engine.begin() as connection:
        cfg.attributes['connection'] = connection
        command.downgrade(cfg, 'wavelength_predictions_20261019')
    assert 'wavelength_round_guard' not in inspect(engine).get_table_names()
    with Session(engine) as db:
        assert db.query(ReturnedPrediction).count() == 1
    with engine.begin() as connection:
        cfg.attributes['connection'] = connection
        command.downgrade(cfg, 'fx_announcements_20261016')
    with Session(engine) as db:
        assert db.get(User, uid).cash == Decimal('123.456789')
    assert 'wavelength_round' not in inspect(engine).get_table_names()
    assert 'wavelength_prediction' not in inspect(engine).get_table_names()
    engine.dispose()
