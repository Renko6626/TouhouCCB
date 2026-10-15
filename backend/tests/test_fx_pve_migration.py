"""Incremental migration preserves assets and enforces engine bindings."""
from decimal import Decimal
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.models.base import User
from app.models.fx import FxPair, FxWallet

OLD = "remove_short_cash_lock_20261014"
NEW = "fx_pve_profiles_20261016"


def migrate(conn, revision, downgrade=False):
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.attributes["connection"] = conn
    (command.downgrade if downgrade else command.upgrade)(cfg, revision)


def test_upgrade_bindings_and_safe_downgrade(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/fx-pve.db")
    with engine.begin() as conn:
        migrate(conn, OLD)
    with Session(engine) as db:
        user = User(username="preserved-bot", is_bot=True, cash=Decimal("123.456"))
        db.add(user)
        db.commit()
        uid = user.id
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO bot_profile(user_id, template, params, status, created_at) VALUES (:uid, 'hodler', :params, 'paused', CURRENT_TIMESTAMP)"), {"uid": uid, "params": '{"a":1}'})
        migrate(conn, NEW)
        assert conn.execute(text("SELECT market_kind,fx_pair_id,strategy_state,params,status FROM bot_profile")).one() == ('lmsr', None, '{}', '{"a":1}', 'paused')
        assert conn.execute(text('SELECT cash FROM "user"')).scalar_one() == 123.456
        with pytest.raises(IntegrityError):
            conn.execute(text("UPDATE bot_profile SET market_kind='fx'"))
        with pytest.raises(IntegrityError):
            conn.execute(text("UPDATE bot_profile SET market_kind='other'"))
        conn.execute(FxPair.__table__.insert().values(currency_code="ABC", currency_name="ABC", status="trading", gold_reserve=100, foreign_reserve=200))
        pair_id = conn.execute(text("SELECT id FROM fx_pair")).scalar_one()
        conn.execute(FxWallet.__table__.insert().values(user_id=uid, pair_id=pair_id, foreign_amount=9, cost_basis=18))
        conn.execute(text("UPDATE bot_profile SET market_kind='fx',fx_pair_id=:pair,market_scope='null'"), {"pair": pair_id})
        with pytest.raises(RuntimeError, match="FX bot profiles"):
            migrate(conn, OLD, downgrade=True)
        with pytest.raises(IntegrityError):
            conn.execute(text("UPDATE bot_profile SET market_scope='[1]'"))
        conn.execute(text("UPDATE bot_profile SET market_kind='lmsr',fx_pair_id=NULL"))
        migrate(conn, OLD, downgrade=True)
        assert "market_kind" not in {c['name'] for c in inspect(conn).get_columns('bot_profile')}
        assert conn.execute(text('SELECT cash FROM "user"')).scalar_one() == 123.456
        assert conn.execute(text('SELECT foreign_amount,cost_basis FROM fx_wallet')).one() == (9,18)
        assert conn.execute(text('SELECT template,params,status FROM bot_profile')).one() == ('hodler','{"a":1}','paused')
    engine.dispose()
