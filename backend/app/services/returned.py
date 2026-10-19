"""Round lifecycle, paid submissions and atomic payouts. No external amounts."""
import asyncio
from datetime import timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.database import managed_transaction
from app.models.returned import ReturnedPrediction, ReturnedRound, utcnow
from app.schemas.returned import MyPrediction, RoundPublic, RoundSummary
from app.services import ledger_service, returned_events
from app.services.account_events import notifying_transaction
from app.services.credit.flags import get_flags
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.risk import check_cash_spend, discover_dependencies
from app.services.credit.version import bump_economic_version
from app.services.market_locks import lock_user

ZERO = Decimal('0')
STAKE = Decimal('100')
BANDS = ((16, '300'), (56, '200'), (100, '150'), (150, '75'))
# Single economic writer already enforced by OWNERSHIP; serialize concurrent starts.
_start_lock = asyncio.Lock()


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def public_round(round):
    return RoundPublic(
        **{key: getattr(round, key) for key in ('round_key', 'game_key', 'round_number', 'left_label', 'right_label', 'team_name', 'status')},
        stake=round.rules['stake'], bands=round.rules['bands'],
        target_position=round.target_position if round.status == 'settled' else None,
        created_at=_utc(round.created_at), closed_at=_utc(round.closed_at), finished_at=_utc(round.finished_at),
    )


async def get_round(db, key, *, lock=False):
    stmt = select(ReturnedRound).where(ReturnedRound.round_key == key)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    round = (await db.execute(stmt)).scalar_one_or_none()
    if round is None:
        raise HTTPException(404, '竞猜轮次不存在')
    return round


async def summary(db, round):
    count, paid, staked = (await db.execute(select(
        func.count(ReturnedPrediction.id), func.coalesce(func.sum(ReturnedPrediction.payout), 0),
        func.coalesce(func.sum(ReturnedPrediction.stake), 0),
    ).where(ReturnedPrediction.round_id == round.id))).one()
    return RoundSummary(**public_round(round).model_dump(), participant_count=count,
                        total_payout=paid, total_refund=staked if round.status == 'cancelled' else ZERO)


async def start_round(db, req):
    async with _start_lock:
        async with managed_transaction(db):
            OWNERSHIP.require_writes()
            existing = (await db.execute(select(ReturnedRound).where(
                ReturnedRound.round_key == req.round_key))).scalar_one_or_none()
            if existing is not None:
                if any(getattr(existing, key) != value for key, value in req.model_dump().items()):
                    raise HTTPException(409, '轮次编号已用于不同题目')
                return public_round(existing)
            active = (await db.execute(select(ReturnedRound.id).where(
                ReturnedRound.status.in_(['open', 'closed'])).limit(1))).first()
            if active:
                raise HTTPException(409, '请先结算或取消上一轮竞猜')
            round = ReturnedRound(**req.model_dump(), rules={
                'stake': str(STAKE), 'bands': [{'max_distance': d, 'payout': p} for d, p in BANDS],
            })
            db.add(round)
            await db.flush()
            result = public_round(round)
        returned_events.publish(round.round_key, round.status)
        return result


async def close_round(db, key):
    async with managed_transaction(db):
        round = await get_round(db, key, lock=True)
        OWNERSHIP.require_writes()
        if round.status == 'cancelled':
            raise HTTPException(409, '本轮竞猜已取消')
        if round.status == 'open':
            round.status, round.closed_at = 'closed', utcnow()
        result = public_round(round)
    returned_events.publish(key, round.status)
    return result


def my_prediction(round, prediction):
    payout = prediction.payout if prediction is not None else None
    stake = prediction.stake if prediction is not None else ZERO
    cancelled = round.status == 'cancelled'
    return MyPrediction(round_key=round.round_key, status=round.status,
        position=prediction.position if prediction else None, stake=stake,
        distance=prediction.distance if prediction else None, payout=payout,
        refund=stake if cancelled else ZERO,
        net_profit=ZERO if cancelled and prediction else (payout - stake if payout is not None else None),
        submitted_at=_utc(prediction.submitted_at) if prediction else None)


async def get_mine(db, key, uid):
    round = await get_round(db, key)
    prediction = (await db.execute(select(ReturnedPrediction).where(
        ReturnedPrediction.round_id == round.id, ReturnedPrediction.user_id == uid))).scalar_one_or_none()
    return my_prediction(round, prediction)


async def _money(db, user, delta, kind, key, *, operator_user_id=None):
    OWNERSHIP.require_writes()
    user.cash += delta
    bump_economic_version(user)
    await ledger_service.record_entry(db, user=user, entry_type=kind,
        cash_delta=delta, debt_delta=ZERO, daily_rate=None, reason=f'returned:{key}',
        operator_user_id=operator_user_id)


async def submit_prediction(db, key, uid, position):
    async with notifying_transaction(db, managed_transaction(db), 'wavelength_stake'):
        round = await get_round(db, key, lock=True)
        OWNERSHIP.require_writes()
        if round.status != 'open':
            raise HTTPException(409, '本轮竞猜已停止提交')
        prediction = (await db.execute(select(ReturnedPrediction).where(
            ReturnedPrediction.round_id == round.id, ReturnedPrediction.user_id == uid))).scalar_one_or_none()
        user = await lock_user(db, uid)
        if not user.is_active:
            raise HTTPException(403, 'USER_BANNED')
        if prediction is None:
            stake = Decimal(round.rules['stake'])
            if user.cash < stake:
                raise HTTPException(422, '金圆券不足，参与本轮需要 100 金圆券')
            thresholds = get_flags().thresholds
            if thresholds is None:
                raise HTTPException(503, '统一信贷风险引擎不可用')
            deps = await discover_dependencies(db, uid, account_locked=True)
            decision = await check_cash_spend(db, user=user, deps=deps, spend=stake,
                thresholds=thresholds, partial_pct=Decimal('1'), now=utcnow())
            if not decision.allowed:
                raise HTTPException(409, f'竞猜投入被资金风控拒绝：{decision.reason}')
            await _money(db, user, -stake, 'wavelength_stake', key)
            prediction = ReturnedPrediction(round_id=round.id, user_id=uid, position=position, stake=stake)
            db.add(prediction)
        else:
            prediction.position, prediction.submitted_at = position, utcnow()
        await db.flush()
        result = my_prediction(round, prediction)
    return result


async def finish_round(db, key, *, target=None, cancel=False, operator_user_id=None):
    reason = 'wavelength_refund' if cancel else 'wavelength_reward'
    async with notifying_transaction(db, managed_transaction(db), reason):
        round = await get_round(db, key, lock=True)
        OWNERSHIP.require_writes()
        if round.status in ('settled', 'cancelled'):
            if (cancel and round.status == 'cancelled') or (not cancel and round.status == 'settled' and round.target_position == target):
                return await summary(db, round)
            raise HTTPException(409, '本轮已结束，不能改变结算结果')
        if not cancel and round.status != 'closed':
            raise HTTPException(409, '请先停止竞猜，再公布结果')
        predictions = (await db.execute(select(ReturnedPrediction).where(
            ReturnedPrediction.round_id == round.id).order_by(ReturnedPrediction.user_id))).scalars().all()
        for prediction in predictions:
            user = await lock_user(db, prediction.user_id)
            if cancel:
                amount = prediction.stake
            else:
                prediction.distance = abs(prediction.position - target)
                amount = next((Decimal(b['payout']) for b in round.rules['bands']
                               if prediction.distance <= b['max_distance']), ZERO)
                prediction.payout = amount
            if amount:
                await _money(db, user, amount, reason, key, operator_user_id=operator_user_id)
        round.status = 'cancelled' if cancel else 'settled'
        round.target_position = None if cancel else target
        round.finished_at = utcnow()
        await db.flush()
        result = await summary(db, round)
    returned_events.publish(key, round.status)
    return result
