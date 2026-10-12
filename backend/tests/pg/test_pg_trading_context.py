import asyncio
from decimal import Decimal
import pytest
from sqlalchemy import update
from app.models.base import User
from app.models.fx import FxPair,FxWallet
from app.schemas.trading_context import GroupKey
from app.services.trading_prices import PricingRegistry
from app.services.credit.trading_context import read_trading_context
pytestmark=[pytest.mark.pg,pytest.mark.asyncio]

async def test_atomic_commit_crosses_read(pg_sessionmaker):
    async with pg_sessionmaker() as db:
        user=User(username='atomic',cash=Decimal('100')); pair=FxPair(currency_code='P',currency_name='P')
        db.add_all([user,pair]);await db.flush()
        db.add(FxWallet(user_id=user.id,pair_id=pair.id,foreign_amount=Decimal('1')));await db.commit()
        registry=PricingRegistry(pg_sessionmaker); original=registry.refresh_groups; committed=False
        async def crossing(keys):
            nonlocal committed
            if not committed:
                committed=True
                async with pg_sessionmaker() as writer:
                    await writer.execute(update(User).where(User.id==user.id).values(cash=Decimal('90'),economic_version=1))
                    await writer.execute(update(FxWallet).where(FxWallet.user_id==user.id).values(foreign_amount=Decimal('11')))
                    await writer.execute(update(FxPair).where(FxPair.id==pair.id).values(gold_reserve=Decimal('2'),pool_version=2));await writer.commit()
            return await original(keys)
        registry.refresh_groups=crossing
        result=await read_trading_context(db,user.id,registry=registry,session_factory=pg_sessionmaker)
        assert user.economic_version==0
        assert result.account.economic_version=='1' and result.account.cash=='90.000000'
        assert result.account.fx_wallets[0].foreign_amount=='11.000000'
        assert result.groups[0].gold_reserve=='2.000000'
        assert result.minimum_group_stamps[0].stamp==result.groups[0].stamp

async def test_serial_refresh_old_response(pg_sessionmaker):
    async with pg_sessionmaker() as db:
        pair=FxPair(currency_code='S',currency_name='S');db.add(pair);await db.commit();key=GroupKey(product='fx',id=pair.id)
    registry=PricingRegistry(pg_sessionmaker); original=registry._snapshot
    entered,release=asyncio.Event(),asyncio.Event(); first=True
    async def paused(db,key):
        nonlocal first
        result=await original(db,key)
        if first: first=False;entered.set();await release.wait()
        return result
    registry._snapshot=paused
    old_task=asyncio.create_task(registry.refresh_groups([key]));await entered.wait()
    async with pg_sessionmaker() as db:
        await db.execute(update(FxPair).where(FxPair.id==key.id).values(gold_reserve=Decimal('2')));await db.commit()
    new_task=asyncio.create_task(registry.refresh_groups([key]));release.set()
    old,new=await asyncio.gather(old_task,new_task)
    assert old[0].gold_reserve=='1.000000' and new[0].gold_reserve=='2.000000'
    assert int(new[0].stamp.revision)>int(old[0].stamp.revision)
    assert (await registry.refresh_groups([key]))[0].stamp==new[0].stamp
