"""Prove local load-test maintenance removes real FX writes, keeping other data."""
import json
import os
from decimal import Decimal

import jwt
import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.base import SiteConfig, User
from app.models.fx import FxExecution, FxPair, FxPoolEvent, FxTrade, FxTreasury
from app.services.fx import net_trading
from app.schemas.fx import FxNetQuoteRequest, FxNetTradeRequest
from scripts.fx_loadtest import cleanup, mint, prepare

pytestmark = [pytest.mark.pg, pytest.mark.asyncio]


async def seed(db):
    user = User(username="untouched", cash=Decimal("123"))
    pair = FxPair(currency_code="ORIGINAL", currency_name="Original", status="trading",
                  gold_reserve=Decimal("1000000"), foreign_reserve=Decimal("200000"))
    db.add_all([user, pair, SiteConfig(key="fx_enabled", value="true", value_type="bool")])
    await db.flush()
    return user.id, pair.id


async def test_prepare_trade_cleanup_preserves_original(pg_sessionmaker, tmp_path, monkeypatch):
    manifest = tmp_path / "run" / "manifest.json"
    async with pg_sessionmaker() as db, db.begin():
        uid, original = await seed(db)
    async with pg_sessionmaker() as db, db.begin():
        data = await prepare(db, tag="test_100", users=100, cash=Decimal("100000"),
                             source_pair=original, manifest=manifest)
    async with pg_sessionmaker() as db:
        users = (await db.execute(select(User).where(User.id != uid))).scalars().all()
        assert len(users) == 100
        assert all(not u.is_bot and not u.is_superuser and u.tos_accepted_at for u in users)
        assert (await db.get(FxPair, original)).status == "paused"
        await mint(db, data, tmp_path / "run" / "tokens.json")
    creds = json.loads((tmp_path / "run" / "tokens.json").read_text())
    claims = jwt.decode(creds["users"][0]["token"], settings.SECRET_KEY, algorithms=["HS256"])
    assert int(claims["sub"]) in {u["uid"] for u in data["users"]}
    assert claims["exp"] - claims["iat"] == 3600
    assert (tmp_path / "run" / "tokens.json").stat().st_mode & 0o777 == 0o600
    # A real net execution creates receipt, trade, wallet, pool event and audits.
    from app.services.credit import ownership
    from app.services.fx import trading
    owner = ownership.WriteOwnership(url=os.environ["TEST_PG_DATABASE_URL"])
    assert await owner.acquire(required=True)
    monkeypatch.setattr(ownership, "OWNERSHIP", owner)
    monkeypatch.setattr(trading, "OWNERSHIP", owner)
    try:
        async with pg_sessionmaker() as db:
            q = await net_trading.quote_net_trade(db, data["users"][0]["uid"], data["pair_id"],
                                                 FxNetQuoteRequest(side="buy", amount="10"))
            assert q.executable
            result = await net_trading.execute_net_trade(db, data["users"][0]["uid"], data["pair_id"],
                FxNetTradeRequest(side="buy", amount="10", min_out="0", idempotency_key="loadtest-real-trade",
                                  allow_growth=True, expected_economic_version=q.economic_version))
            assert result.trade_ids
    finally:
        await owner.release()
    async with pg_sessionmaker() as db, db.begin():
        counts = await cleanup(db, data)
        assert counts["fx_trade"] == 1
        assert await db.get(User, data["users"][0]["uid"])  # preview did not delete
    async with pg_sessionmaker() as db, db.begin():
        await cleanup(db, data, execute=True)
    async with pg_sessionmaker() as db:
        assert (await db.get(User, uid)).cash == Decimal("123")
        assert (await db.get(FxPair, original)).status == "trading"
        assert await db.get(FxPair, data["pair_id"]) is None
        assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 1
        for model in (FxExecution, FxTrade, FxPoolEvent, FxTreasury):
            assert (await db.execute(select(func.count()).select_from(model))).scalar_one() == 0


async def test_repeat_prepare_and_foreign_participant_refuse(pg_sessionmaker, tmp_path):
    async with pg_sessionmaker() as db, db.begin():
        uid, original = await seed(db)
        data = await prepare(db, tag="a_b", users=1, cash=Decimal("99"), source_pair=original,
                             manifest=tmp_path / "manifest.json")
    async with pg_sessionmaker() as db, db.begin():
        with pytest.raises(ValueError, match="already"):
            await prepare(db, tag="a_b", users=1, cash=Decimal("100"), source_pair=original,
                          manifest=tmp_path / "manifest.json")
    from app.models.fx import FxWallet
    async with pg_sessionmaker() as db, db.begin():
        db.add(FxWallet(user_id=uid, pair_id=data["pair_id"], foreign_amount=Decimal("1")))
    async with pg_sessionmaker() as db, db.begin():
        with pytest.raises(ValueError, match="unrelated participation"):
            await cleanup(db, data, execute=True)
    async with pg_sessionmaker() as db:
        assert (await db.get(User, data["users"][0]["uid"])).cash == Decimal("99")
        assert await db.get(FxPair, data["pair_id"])
