import asyncio
from sqlmodel import select
import app.models.base  # noqa
import app.models.redemption, app.models.title, app.models.ledger, app.models.audit  # noqa
import app.models.bot, app.models.fx, app.models.credit  # noqa
from app.core.database import async_session_maker, engine
from app.models.base import SiteConfig

EXTRA = {
    "credit_leverage": "10",
    "credit_maintenance_ratio": "0.04",
    "fx_enabled": "true",
    "fx_short_enabled": "true",
    "loan_enabled": "true",
    "homepage_fx_enabled": "true",
}

async def main():
    async with async_session_maker() as db, db.begin():
        rows = {r.key: r for r in (await db.execute(select(SiteConfig))).scalars()}
        for k, v in EXTRA.items():
            if k in rows:
                rows[k].value = v
            else:
                db.add(SiteConfig(key=k, value=v, value_type="bool" if k in
                                  {"fx_enabled","fx_short_enabled","loan_enabled","homepage_fx_enabled"} else "decimal"))
        print("configs now:", sorted({*rows, *EXTRA}))
    await engine.dispose()

asyncio.run(main())
