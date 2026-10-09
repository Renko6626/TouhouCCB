import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Session

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.models.fx import FxPair, FxTrade, FxTreasury, FxWallet  # noqa: E402
from app.models.base import User  # noqa: E402,F401  (register FK target)
from app.schemas.fx import FxPairPublic  # noqa: E402


def test_fx_tables_and_constraints_enforce_interface():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine, tables=[
        FxPair.__table__, FxTreasury.__table__, FxWallet.__table__,
        FxTrade.__table__,
    ])
    names = set(inspect(engine).get_table_names())
    assert {"fx_pair", "fx_treasury", "fx_wallet", "fx_trade"} <= names

    with Session(engine) as session:
        pair = FxPair(currency_code="GFC", currency_name="幻想外币", gold_reserve=Decimal("100"), foreign_reserve=Decimal("100"))
        session.add(pair)
        session.commit()
        session.refresh(pair)
        session.add(FxWallet(user_id=1, pair_id=pair.id, foreign_amount=Decimal("1"), cost_basis=Decimal("1")))
        session.commit()
        session.add(FxWallet(user_id=1, pair_id=pair.id, foreign_amount=Decimal("2"), cost_basis=Decimal("2")))
        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()
        for values in (
            {"status": "invalid"},
            {"buy_fee_rate": Decimal("1.01")},
            {"gold_reserve": Decimal("0")},
        ):
            session.add(FxPair(currency_code="X" + str(len(values)), currency_name="X", **values))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

        treasury = FxTreasury(pair_id=pair.id, gold_balance=Decimal("0"), foreign_balance=Decimal("0"))
        session.add(treasury)
        session.commit()
        for field in ("gold_balance", "foreign_balance"):
            session.add(FxTreasury(pair_id=pair.id, **{field: Decimal("-1")}))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

        session.rollback()
        session.add(FxTrade(pair_id=pair.id, user_id=1, side="buy", input_amount=Decimal("1"), output_amount=Decimal("1"), fee_amount=Decimal("0"), pre_gold_reserve=Decimal("100"), pre_foreign_reserve=Decimal("100"), post_gold_reserve=Decimal("101"), post_foreign_reserve=Decimal("99"), post_price=Decimal("1.02"), source="player", idempotency_key="k"))
        session.commit()
        session.add(FxTrade(pair_id=pair.id, user_id=1, side="buy", input_amount=Decimal("1"), output_amount=Decimal("1"), fee_amount=Decimal("0"), pre_gold_reserve=Decimal("100"), pre_foreign_reserve=Decimal("100"), post_gold_reserve=Decimal("101"), post_foreign_reserve=Decimal("99"), post_price=Decimal("1.02"), source="player", idempotency_key="k"))
        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()
        for field in ("pre_gold_reserve", "pre_foreign_reserve", "post_gold_reserve", "post_foreign_reserve"):
            kwargs = {
                "pair_id": pair.id,
                "user_id": None,
                "side": "buy",
                "input_amount": Decimal("1"),
                "output_amount": Decimal("1"),
                "fee_amount": Decimal("0"),
                "pre_gold_reserve": Decimal("100"),
                "pre_foreign_reserve": Decimal("100"),
                "post_gold_reserve": Decimal("101"),
                "post_foreign_reserve": Decimal("99"),
                "post_price": Decimal("1.02"),
                "source": "player",
                "idempotency_key": None,
            }
            kwargs[field] = Decimal("0")
            session.add(FxTrade(**kwargs))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()


def test_public_schemas_do_not_expose_private_fx_parameters():
    assert {"target_price", "target_min", "target_max"}.isdisjoint(FxPairPublic.model_fields)
