"""Private trigger order storage; no funds are reserved here."""
from datetime import datetime, timezone
from decimal import Decimal
import logging

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fx import FxExecution, FxPair, FxTrade, FxTriggerOrder
from app.schemas.fx import FxOrderResult, FxTradeInstruction, FxTriggerOrderCreate, FxTriggerOrderRead
from app.services.market_locks import lock_user


async def create_order_in_session(db: AsyncSession, user_id: int, req: FxTriggerOrderCreate) -> FxTriggerOrderRead:
    # Pair precedes User, matching trading/admin lock ordering.
    pair = (await db.execute(select(FxPair).where(FxPair.id == req.pair_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if pair is None or pair.archived or pair.status == 'draft':
        raise HTTPException(404, 'FX pair not found')
    if pair.status == 'closed':
        raise HTTPException(409, 'FX pair is closed')
    user = await lock_user(db, user_id)
    if not user.is_active:
        raise HTTPException(403, 'User is inactive')
    count = (await db.execute(select(func.count()).select_from(FxTriggerOrder).where(
        FxTriggerOrder.user_id == user_id, FxTriggerOrder.status == 'waiting'))).scalar_one()
    if count >= 3:
        raise HTTPException(409, '最多只能同时保留 3 张有效挂单')
    order = FxTriggerOrder(user_id=user_id, **req.model_dump())
    db.add(order)
    await db.flush()
    result = FxTriggerOrderRead.model_validate(order)
    return result


logger = logging.getLogger(__name__)


def order_request_key(order_id: int) -> str:
    return f'fx-order:{order_id}'


async def _locked_order(db, order_id):
    return (await db.execute(select(FxTriggerOrder).where(FxTriggerOrder.id == order_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()


async def _check_no_receipt(db, order):
    receipt = (await db.execute(select(FxExecution.id).where(
        FxExecution.user_id == order.user_id,
        FxExecution.idempotency_key == order_request_key(order.id)))).scalar_one_or_none()
    legacy = (await db.execute(select(FxTrade.id).where(
        FxTrade.user_id == order.user_id,
        FxTrade.idempotency_key == order_request_key(order.id)))).scalar_one_or_none()
    if receipt is not None or legacy is not None or order.execution_id or order.trade_id:
        logger.error('Unexecuted FX order %s has execution evidence; manual check required', order.id)
        raise RuntimeError('FX order/receipt mismatch; manual check required')


async def _record_failure(writer, db, user_id, order_id, price, reason):
    # The money transaction has already rolled back. Cancellation wins if it
    # committed while these locks were released.
    await lock_user(db, user_id)
    order = await _locked_order(db, order_id)
    if order is None or order.status != 'waiting':
        await db.rollback()
        return None
    await _check_no_receipt(db, order)
    order.status = 'failed'
    order.failure_reason = reason[:512]
    order.observed_trigger_price = price
    order.finished_at = datetime.now(timezone.utc)
    await db.flush()
    result = FxTriggerOrderRead.model_validate(order)
    from app.services.fx.writer_ops import commit_and_install
    await commit_and_install(writer, db)
    return result


async def _attempt_order_owned(writer, order_id: int, observed_price: Decimal) -> FxTriggerOrderRead | None:
    """Try a price candidate once; money and terminal outcome commit together.

    Infrastructure/commit errors propagate and stop the scan. A subsequent
    call reads persisted state first; it never retries a waiting order with a
    receipt. Only explicit business rejection becomes a failed outcome.
    """
    from app.core.database import async_session_maker
    from app.services import site_config
    from app.services.account_events import capture_accounts, publish_accounts
    from app.services.credit import flags
    from app.services.credit.gates import GATES
    from app.services.credit.keys import GroupKey
    from app.services.credit.risk import discover_dependencies
    from app.services.fx import net_trading, publisher, shorts, trading

    async with async_session_maker() as db:
        order = await db.get(FxTriggerOrder, order_id)
        if order is None:
            return None
        if order.status != 'waiting':
            # A prior commit may have succeeded even if its acknowledgement
            # failed. Read its existing evidence; never reconstruct a trade.
            try:
                if order.status == 'succeeded':
                    await result_for_order(db, FxTriggerOrderRead.model_validate(order))
                else:
                    await _check_no_receipt(db, order)
            except BaseException:
                logger.exception('FX order %s result cannot be verified; stopping scan', order_id)
                raise
            return None
        if not (observed_price <= order.trigger_price if order.comparison == 'lte'
                else observed_price >= order.trigger_price):
            return None
        user_id, pair_id = order.user_id, order.pair_id
        await db.rollback()
        key = GroupKey('fx', pair_id)
        needs_discovery = False
        failure = None
        for attempt in range(flags.get_flags().credit_risk_retry_limit + 2):
            try:
                deps = await discover_dependencies(db, user_id, extra_groups=[key]) if needs_discovery else None
                if db.in_transaction():
                    await db.rollback()
                async with GATES.hold(exclusive=[key], shared=deps.groups if deps else ()):
                    try:
                        pair = await trading._pair(db, pair_id, lock=True)
                        user = await lock_user(db, user_id)
                        order = await _locked_order(db, order_id)
                        if order is None or order.status != 'waiting':
                            await db.rollback()
                            return None
                        await _check_no_receipt(db, order)
                        if not user.is_active:
                            raise HTTPException(403, 'User is inactive')
                        if pair.archived or pair.status != 'trading':
                            raise HTTPException(400, 'FX pair is not trading')
                        if not await site_config.get_bool_or(db, 'fx_enabled', False):
                            raise HTTPException(400, 'FX trading is disabled')
                        instruction = FxTradeInstruction.model_validate({
                            name: getattr(order, name) for name in FxTradeInstruction.model_fields})
                        try:
                            execution = await net_trading.execute_fx_instruction_in_session(db,
                                user_id=user_id, pair_id=pair_id, instruction=instruction,
                                request_key=order_request_key(order_id), credit_deps=deps)
                        except (ValueError, ArithmeticError) as exc:
                            raise trading.TradeRejected(str(exc)) from exc
                        if execution.replay:
                            raise RuntimeError('FX order/receipt mismatch; manual check required')
                        order.status = 'succeeded'
                        order.finished_at = datetime.now(timezone.utc)
                        order.observed_trigger_price = observed_price
                        if isinstance(execution, shorts.FxShortExecution):
                            order.trade_id = execution.trade_id
                            trade_id = execution.trade_id
                        else:
                            order.execution_id = execution.execution_id
                            trade_id = execution.trade_ids[-1]
                        await db.flush()
                        result = FxTriggerOrderRead.model_validate(order)
                        post_price = execution.post_price
                        hints = capture_accounts(db)
                        from app.services.fx.writer_ops import commit_and_install
                        await commit_and_install(writer, db)
                    except BaseException:
                        await db.rollback()
                        raise
                    publish_accounts(hints, 'fx_orders')
                    trading.notify_market_data_committed(pair_id)
                    publisher.enqueue_publication(pair_id=pair_id, post_price=post_price, trade_id=trade_id)
                    return result
            except (trading._RetryCredit, shorts.ShortRetryCredit):
                needs_discovery = True
                if attempt == flags.get_flags().credit_risk_retry_limit + 1:
                    failure = 'version_conflict; retry'
            except (HTTPException, trading.TradeRejected, shorts.ShortRejected) as exc:
                failure = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
            except BaseException:
                logger.exception('FX order %s execution interrupted; stopping scan', order_id)
                raise
            if failure is not None:
                await db.rollback()
                async with GATES.hold(exclusive=[key]):
                    return await _record_failure(writer, db, user_id, order_id, observed_price, failure)
        raise RuntimeError('FX order retry loop exhausted')


async def fail_unavailable_in_session(db, order_id: int) -> FxTriggerOrderRead | None:
    """End a permanently unavailable pair's order, rechecking under locks."""
    candidate = await db.get(FxTriggerOrder, order_id)
    if candidate is None or candidate.status != 'waiting':
        return None
    user_id, pair_id = candidate.user_id, candidate.pair_id
    pair = (await db.execute(select(FxPair).where(FxPair.id == pair_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one()
    await lock_user(db, user_id)
    order = await _locked_order(db, order_id)
    if order is None or order.status != 'waiting' or not (pair.archived or pair.status == 'closed'):
        return None
    await _check_no_receipt(db, order)
    order.status = 'failed'
    order.failure_reason = 'FX pair is permanently unavailable'
    order.finished_at = datetime.now(timezone.utc)
    await db.flush()
    result = FxTriggerOrderRead.model_validate(order)
    return result


async def result_for_order(db: AsyncSession, order: FxTriggerOrderRead) -> FxOrderResult:
    """Build a private notification from a committed terminal outcome."""
    pair = await db.get(FxPair, order.pair_id)
    result = FxOrderResult(order_id=order.id, pair_id=order.pair_id,
        currency_code=pair.currency_code, action=order.action, status=order.status,
        occurred_at=order.finished_at, execution_id=order.execution_id,
        trade_id=order.trade_id, failure_reason=order.failure_reason)
    if order.status == 'succeeded':
        receipt = (await db.execute(select(FxExecution).where(
            FxExecution.user_id == order.user_id,
            FxExecution.idempotency_key == order_request_key(order.id)))).scalar_one_or_none()
        if receipt is None:
            raise RuntimeError('FX order/receipt mismatch; manual check required')
        if order.execution_id:
            if receipt.id != order.execution_id:
                raise RuntimeError('FX order/receipt mismatch; manual check required')
            result.input_amount = Decimal(receipt.response_payload['input_amount'])
            result.output_amount = Decimal(receipt.response_payload['output_amount'])
            result.borrow_amount = Decimal(receipt.response_payload['borrow_amount'])
        else:
            if receipt.response_payload.get('trade_id') != order.trade_id:
                raise RuntimeError('FX order/receipt mismatch; manual check required')
            trade = await db.get(FxTrade, order.trade_id)
            result.input_amount, result.output_amount = trade.input_amount, trade.output_amount
            result.borrow_amount = Decimal('0')
    return result


async def list_orders(db: AsyncSession, user_id: int, *, status: str | None = None,
                      before_id: int | None = None, limit: int = 50) -> list[FxTriggerOrderRead]:
    if status is not None and status not in {'waiting', 'succeeded', 'failed', 'cancelled'}:
        raise HTTPException(422, 'Invalid order status')
    if not 1 <= limit <= 100:
        raise HTTPException(422, 'limit must be within 1..100')
    stmt = select(FxTriggerOrder).where(FxTriggerOrder.user_id == user_id)
    if status is not None:
        stmt = stmt.where(FxTriggerOrder.status == status)
    if before_id is not None:
        stmt = stmt.where(FxTriggerOrder.id < before_id)
    rows = (await db.execute(stmt.order_by(FxTriggerOrder.id.desc()).limit(limit))).scalars().all()
    return [FxTriggerOrderRead.model_validate(row) for row in rows]


async def cancel_order_in_session(db: AsyncSession, user_id: int, order_id: int) -> FxTriggerOrderRead:
    await lock_user(db, user_id)
    order = (await db.execute(select(FxTriggerOrder).where(
        FxTriggerOrder.id == order_id, FxTriggerOrder.user_id == user_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if order is None:
        raise HTTPException(404, 'Order not found')
    if order.status not in {'waiting', 'cancelled'}:
        raise HTTPException(409, 'Order is already finished')
    if order.status == 'waiting':
        order.status = 'cancelled'
        order.finished_at = datetime.now(timezone.utc)
        await db.flush()
    result = FxTriggerOrderRead.model_validate(order)
    return result


async def create_order(db, user_id, req):
    from app.services.fx.writer import FX_WRITER
    from app.services.fx.writer_ops import CreateOrder
    command = CreateOrder(int(req.pair_id), int(user_id), req.model_dump_json())
    await db.close()
    return await FX_WRITER.submit(command)


async def _order_pair(db, order_id, user_id=None):
    stmt = select(FxTriggerOrder.pair_id).where(FxTriggerOrder.id == order_id)
    if user_id is not None:
        stmt = stmt.where(FxTriggerOrder.user_id == user_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def cancel_order(db, user_id, order_id):
    from app.services.fx.writer import FX_WRITER
    from app.services.fx.writer_ops import CancelOrder
    pair_id = await _order_pair(db, order_id, user_id)
    await db.close()
    if pair_id is None:
        raise HTTPException(404, 'Order not found')
    return await FX_WRITER.submit(CancelOrder(int(pair_id), int(user_id), int(order_id)))


async def _submit_order(order_id, observed_price=None, *, unavailable=False):
    from app.core.database import async_session_maker
    from app.services.fx.writer import FX_WRITER
    from app.services.fx.writer_ops import AttemptOrder, FailOrder
    async with async_session_maker() as db:
        pair_id = await _order_pair(db, order_id)
    if pair_id is None:
        return None
    command = (FailOrder(int(pair_id), int(order_id)) if unavailable else
               AttemptOrder(int(pair_id), int(order_id), Decimal(observed_price)))
    return await FX_WRITER.submit(command)


async def attempt_order(order_id, observed_price):
    return await _submit_order(order_id, observed_price)


async def fail_unavailable_order(order_id):
    return await _submit_order(order_id, unavailable=True)
