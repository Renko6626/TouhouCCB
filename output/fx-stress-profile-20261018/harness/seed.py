import asyncio, os
from decimal import Decimal
from pathlib import Path
from sqlalchemy import select
from app.core.database import async_session_maker, engine
from app.models.base import SiteConfig
from app.models.fx import FxPair, FxTreasury
from app.services.config_seed import seed_current_config
from scripts.fx_loadtest import prepare,mint

async def main():
    await seed_current_config()
    async with async_session_maker() as db:
        for key in ('fx_enabled','fx_short_enabled','loan_enabled'):
            row=(await db.execute(select(SiteConfig).where(SiteConfig.key==key))).scalar_one();row.value='true'
        row=(await db.execute(select(SiteConfig).where(SiteConfig.key=='bot_detection_enabled'))).scalar_one();row.value='false'
        pair=FxPair(currency_code='SOURCE',currency_name='Local source',status='trading',gold_reserve=Decimal('1000000'),foreign_reserve=Decimal('200000'),initial_price=Decimal('5'),short_lending_limit_foreign=Decimal('200000'))
        db.add(pair);await db.flush();db.add(FxTreasury(pair_id=pair.id,gold_balance=pair.gold_reserve,foreign_balance=pair.foreign_reserve));await db.commit();source=pair.id
    directory=Path(os.environ['LOCAL_RUN_DIRECTORY'])/'private';directory.mkdir(mode=0o700)
    async with async_session_maker() as db:
        async with db.begin():
            data=await prepare(db,tag='local_'+os.environ['LOCAL_USERS'],users=int(os.environ['LOCAL_USERS']),cash=Decimal('100000'),source_pair=source,manifest=directory/'manifest.json')
    async with async_session_maker() as db:
        await mint(db,data,directory/'tokens.json')
    await engine.dispose()
asyncio.run(main())
