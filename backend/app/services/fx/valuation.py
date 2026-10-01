"""Read-only FX mark-to-market valuation for display and ranking."""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import User
from app.models.fx import FxPair, FxWallet
from app.services.fx.amm import marginal_price

ZERO = Decimal("0")
QUANT = Decimal("0.000001")


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(QUANT)


async def compute_fx_mtm(
    db: AsyncSession,
    user_ids: Optional[Iterable[int]] = None,
) -> Dict[int, Decimal]:
    """Return ``user_id -> foreign amount * current AMM marginal price``.

    This is a display value.  It deliberately reads only live pair reserves and
    wallets; FX is not included in collateral or liquidation calculations.
    """
    ids: Optional[List[int]] = None
    if user_ids is not None:
        ids = list(user_ids)
        if not ids:
            return {}
    stmt = (
        select(FxWallet.user_id, FxWallet.foreign_amount,
               FxPair.gold_reserve, FxPair.foreign_reserve)
        .join(FxPair, FxPair.id == FxWallet.pair_id)
        .where(FxWallet.foreign_amount > ZERO)
    )
    if ids is not None:
        stmt = stmt.where(FxWallet.user_id.in_(ids))
    rows = (await db.execute(stmt)).all()
    result: Dict[int, Decimal] = {}
    for user_id, amount, gold_reserve, foreign_reserve in rows:
        price = marginal_price(Decimal(gold_reserve), Decimal(foreign_reserve))
        result[int(user_id)] = _q(result.get(int(user_id), ZERO) + Decimal(amount) * price)
    return result


async def compute_total_net_worth(
    db: AsyncSession,
    user_ids: Optional[Iterable[int]] = None,
) -> Dict[int, Decimal | None]:
    """Return display wealth, retaining unknown unified liability estimates."""
    from app.services.credit import flags
    from app.services.credit.valuation import value_users_batch
    from app.services import site_config
    from app.services.wealth import compute_users_holdings_value_mtm

    ids = list(user_ids) if user_ids is not None else None
    stmt = select(User.id, User.cash, User.debt)
    if ids is not None:
        if not ids:
            return {}
        stmt = stmt.where(User.id.in_(ids))
    users = (await db.execute(stmt)).all()
    if not users:
        return {}
    if flags.get_flags().unified_credit_enabled:
        rate = await site_config.get_decimal_or(db, "loan_daily_rate", ZERO)
        valuations = await value_users_batch(db, [int(u[0]) for u in users], daily_rate=rate)
        return {uid: value.display_equity for uid, value in valuations.items()}
    lmsr = await compute_users_holdings_value_mtm(db, user_ids=ids)
    fx = await compute_fx_mtm(db, user_ids=ids)
    return {
        int(uid): _q(Decimal(cash) - Decimal(debt)
                     + lmsr.get(int(uid), ZERO) + fx.get(int(uid), ZERO))
        for uid, cash, debt in users
    }
