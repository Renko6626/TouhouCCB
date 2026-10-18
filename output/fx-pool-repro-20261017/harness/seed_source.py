"""Seed one trading FX pair + feature flags on the isolated repro database."""
import asyncio
from decimal import Decimal

from sqlmodel import SQLModel, select

import app.models.base  # noqa: F401
import app.models.redemption  # noqa: F401
import app.models.title  # noqa: F401
import app.models.ledger  # noqa: F401
import app.models.audit  # noqa: F401
import app.models.bot  # noqa: F401
import app.models.fx  # noqa: F401
import app.models.credit  # noqa: F401
from app.core.database import async_session_maker, engine
from app.models.base import SiteConfig
from app.models.fx import FxPair, FxTreasury

FLAGS = {
    "fx_enabled": "true",
    "fx_short_enabled": "true",
    "loan_enabled": "true",
    "homepage_fx_enabled": "true",
}


async def main() -> None:
    async with async_session_maker() as db, db.begin():
        existing = (await db.execute(select(FxPair))).scalars().first()
        if existing is not None:
            print("pair already present", existing.id, existing.currency_code, existing.status)
            return
        pair = FxPair(
            currency_code="REPRO",
            currency_name="Repro Market",
            status="trading",
            gold_reserve=Decimal("1000000"),
            foreign_reserve=Decimal("200000"),
            initial_price=Decimal("5"),
            buy_fee_rate=Decimal("0.003"),
            sell_fee_rate=Decimal("0.003"),
            short_lending_limit_foreign=Decimal("50000"),
        )
        db.add(pair)
        await db.flush()
        db.add(FxTreasury(pair_id=pair.id, gold_balance=pair.gold_reserve,
                          foreign_balance=pair.foreign_reserve))
        for key, value in FLAGS.items():
            row = (await db.execute(select(SiteConfig).where(SiteConfig.key == key))).scalar_one_or_none()
            if row is None:
                db.add(SiteConfig(key=key, value=value, value_type="bool"))
            else:
                row.value = value
        print("seeded pair", pair.id)
    await engine.dispose()


asyncio.run(main())
