"""WP5：统一 LMSR 组强平 `op_liquidate_group` 的语义、幂等与审计一致性。

覆盖：
- 同事务还债（cash 先还债、debt_after/cash_after 是提交后权威快照、LIQUIDATE +
  liquidation_repay + liquidation_action 同一事务）。
- `(run_id, round_no)` 业务幂等：重放不二次卖/扣/还；**卖出前**预检（预置 repay 动作
  的轮次不会再卖仓）。
- 失败整批回滚（record_action 抛错 → 现金/债务/持仓/镜像/交易/动作全部不变）。
- 市场不可交易 → blocked_reason 且零写入。
- commit 后 feed_prices（改价无成交事件）、不写 candle。
- unified 开关开启后 legacy 强平入口硬拒绝；非法命令参数 422。
- audit_replay.fold(check=True) + compare_with_live 全绿（资金事件先于记录型事件）。

运行：
    DATABASE_URL=sqlite+aiosqlite:////dev/shm/credit-wp5.db \
        python -m pytest -q tests/test_credit_liquidate_group.py
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, update as sa_update

from app.core.database import async_session_maker
from app.models.audit import AuditEvent
from app.models.base import (
    Market, MarketStatus, Outcome, Position, Transaction, TransactionType, User,
)
from app.models.credit import LiquidationAction, LiquidationRun
from app.services import audit_replay, audit_service
from app.services.candle_flusher import CANDLE_FLUSHER
from app.services.credit.flags import CreditFlags, clear_flags, set_flags
from app.services.credit.runs import get_or_create_active_run
from app.services.lmsr import calculate_lmsr_with_prices
from app.services.market_writer import MarketState, WRITER
from app.services.tick_broadcaster import TICK_BROADCASTER
from app.services.writer_ops import BuyCmd, LiquidateGroupCmd, LiquidateMarketCmd

ZERO = Decimal("0")


@pytest_asyncio.fixture(autouse=True)
async def _stop_writer():
    yield
    await WRITER.stop()


async def _seed_market(shares=("0", "0"), *, status=MarketStatus.TRADING):
    async with async_session_maker() as s:
        async with s.begin():
            m = Market(title="wp5group", description="", liquidity_b=100.0,
                       status=status, tags="")
            s.add(m)
            await s.flush()
            oids = []
            for i, value in enumerate(shares):
                o = Outcome(market_id=m.id, label=f"o{i}", total_shares=Decimal(value))
                s.add(o)
                await s.flush()
                oids.append(int(o.id))
            return int(m.id), oids


async def _seed_user(*, cash="0", debt="50", username="wp5group",
                     register=False, debt_accrued=None):
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=username, casdoor_id=f"cas_{username}",
                     cash=Decimal(cash), debt=Decimal(debt), is_active=True,
                     debt_last_accrued_at=debt_accrued)
            s.add(u)
            await s.flush()
            uid = int(u.id)
            if register:
                audit_service.record(
                    s, "user_register", user_id=uid, payload={"username": username},
                    user_after=audit_service.user_snapshot(u))
            return uid


async def _give_position(uid: int, oid: int, amount: str, cost: str) -> None:
    async with async_session_maker() as s:
        async with s.begin():
            s.add(Position(user_id=uid, outcome_id=oid,
                           amount=Decimal(amount), cost_basis=Decimal(cost)))
            await s.flush()
            await s.execute(
                sa_update(Outcome).where(Outcome.id == oid)
                .values(total_shares=Outcome.total_shares + Decimal(amount)))


async def _new_run(uid: int) -> int:
    async with async_session_maker() as s:
        async with s.begin():
            run = await get_or_create_active_run(
                s, user_id=uid, trigger_source="test",
                now=datetime.now(timezone.utc))
            return int(run.id)


def _cmd(mid, uid, run_id, *, mode="full", partial_pct=Decimal("1"),
         fee_rate=ZERO, daily_rate=ZERO, round_no=1):
    return LiquidateGroupCmd(
        market_id=mid, user_id=uid, run_id=run_id, round_no=round_no, mode=mode,
        partial_pct=partial_pct, fee_rate=fee_rate, daily_rate=daily_rate,
        trigger_source="test")


async def _snapshot(uid: int, oids: list[int]):
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        positions = [
            (int(p.outcome_id), p.amount, p.cost_basis)
            for p in (await s.execute(
                select(Position).where(Position.user_id == uid).order_by(Position.id)
            )).scalars().all()
        ]
        q = [
            (int(o.id), o.total_shares)
            for o in (await s.execute(
                select(Outcome).where(Outcome.id.in_(oids)).order_by(Outcome.id)
            )).scalars().all()
        ]
        liq_txs = len((await s.execute(
            select(Transaction).where(Transaction.user_id == uid,
                                      Transaction.type == TransactionType.LIQUIDATE)
        )).scalars().all())
        actions = (await s.execute(select(LiquidationAction))).scalars().all()
        return (u.cash, u.debt, positions, q, liq_txs,
                [(a.run_id, a.round_no, a.kind) for a in actions])


@pytest.mark.asyncio
async def test_group_liquidation_repays_debt_in_same_transaction():
    mid, oids = await _seed_market(("0", "0"))
    uid = await _seed_user(cash="0", debt="5000", username="repay_user",
                           debt_accrued=datetime.now(timezone.utc))
    await _give_position(uid, oids[0], "20", "10")
    await WRITER.start()
    run_id = await _new_run(uid)

    res = await WRITER.submit(_cmd(
        mid, uid, run_id, mode="full", fee_rate=Decimal("0.01"),
        daily_rate=Decimal("0.001")))

    assert res["sold_count"] == 1
    assert res["repaid"] > ZERO
    assert res["cash_after"] == ZERO, "回款必须同事务全部用于还债"
    assert res["debt_after"] < Decimal("5000")

    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.cash == res["cash_after"]
        assert u.debt == res["debt_after"]
        action = (await s.execute(select(LiquidationAction))).scalars().one()
        assert action.kind == "sell_group"
        assert action.product == "lmsr" and action.group_id == mid
        assert action.mode == "full" and action.fee_currency == "gold"
        assert action.proceeds == res["gross"] and action.fee == res["fee"]
        assert action.repaid == res["repaid"]
        assert action.debt_after == res["debt_after"]
        assert action.cash_after == res["cash_after"]
        assert action.economic_version_after == 1
        assert u.economic_version == 1
        # 资金事件先于记录型事件（audit_replay 锚点顺序）
        events = (await s.execute(
            select(AuditEvent.event_type)
            .where(AuditEvent.user_id == uid)
            .order_by(AuditEvent.id)
        )).scalars().all()
    assert events[-1] == "liquidation_action"
    assert "liquidation_repay" in events
    assert events.index("liquidation_repay") < events.index("liquidation_action")
    # 还债事件快照必须是提交后的债
    async with async_session_maker() as s:
        repay_ev = (await s.execute(
            select(AuditEvent).where(AuditEvent.event_type == "liquidation_repay")
        )).scalars().one()
    assert Decimal(repay_ev.user_after["debt"]) == res["debt_after"]


@pytest.mark.asyncio
async def test_replay_same_run_round_does_not_sell_twice():
    mid, oids = await _seed_market(("0", "0"))
    uid = await _seed_user(cash="0", debt="50", username="replay_user")
    await _give_position(uid, oids[0], "20", "10")
    await WRITER.start()
    run_id = await _new_run(uid)
    cmd = _cmd(mid, uid, run_id, mode="full", fee_rate=Decimal("0.01"))

    first = await WRITER.submit(cmd)
    assert first["replayed"] is False
    snap_first = await _snapshot(uid, oids)

    second = await WRITER.submit(_cmd(mid, uid, run_id, mode="full",
                                      fee_rate=Decimal("0.01")))
    assert second["replayed"] is True
    assert second["sold_count"] == first["sold_count"]
    assert second["gross"] == first["gross"]
    assert second["fee"] == first["fee"]
    assert second["net"] == first["net"]
    assert second["repaid"] == first["repaid"]
    assert second["debt_after"] == first["debt_after"]
    assert second["cash_after"] == first["cash_after"]
    assert await _snapshot(uid, oids) == snap_first, "重放不得产生任何第二次卖出/扣款/还债"

    async with async_session_maker() as s:
        run = await s.get(LiquidationRun, run_id)
        assert run.rounds == 1 and run.next_round == 2
        assert run.total_proceeds == first["gross"]
        assert run.total_repaid == first["repaid"]
        assert run.total_fee == first["fee"]


@pytest.mark.asyncio
async def test_existing_action_for_round_prevents_sale():
    """幂等键必须在卖出之前检查：本轮已被 repay 动作占用 → 绝不再卖仓。"""
    mid, oids = await _seed_market(("0", "0"))
    uid = await _seed_user(cash="0", debt="50", username="occupied_user")
    await _give_position(uid, oids[0], "20", "10")
    await WRITER.start()
    run_id = await _new_run(uid)
    async with async_session_maker() as s:
        async with s.begin():
            s.add(LiquidationAction(
                run_id=run_id, user_id=uid, round_no=1, kind="repay_cash",
                proceeds=Decimal("7"), fee=ZERO, repaid=Decimal("7"),
                debt_after=Decimal("43"), cash_after=ZERO,
                executed={"sold_count": 0}, economic_version_after=1))
    before = await _snapshot(uid, oids)

    res = await WRITER.submit(_cmd(mid, uid, run_id, mode="full"))

    assert res["replayed"] is True
    assert res["sold_count"] == 0
    assert res["gross"] == Decimal("7.000000")
    assert res["repaid"] == Decimal("7.000000")
    assert await _snapshot(uid, oids) == before


@pytest.mark.asyncio
async def test_no_partial_state_when_action_recording_fails(monkeypatch):
    """卖出 + 还债 + 动作记录必须同一事务：动作写失败 → 全部回滚。"""
    from app.services import writer_ops

    mid, oids = await _seed_market(("0", "0"))
    uid = await _seed_user(cash="0", debt="50", username="rollback_user")
    await _give_position(uid, oids[0], "20", "10")
    await WRITER.start()
    run_id = await _new_run(uid)
    state = WRITER.get_state(mid)
    before = await _snapshot(uid, oids)
    before_q = list(state.q_dec)

    async def _boom(*args, **kwargs):
        raise RuntimeError("record_action failed")

    monkeypatch.setattr(writer_ops, "record_action", _boom)
    with pytest.raises(RuntimeError):
        await writer_ops.op_liquidate_group(state, _cmd(mid, uid, run_id, mode="full"))

    assert await _snapshot(uid, oids) == before, "失败必须整批回滚，不得留下部分状态"
    assert state.q_dec == before_q and state.q_dec == [Decimal("20.000000"), ZERO]


@pytest.mark.asyncio
async def test_market_not_open_blocked_without_writes():
    mid, oids = await _seed_market(("0", "0"), status=MarketStatus.HALT)
    uid = await _seed_user(cash="0", debt="50", username="halt_user")
    await _give_position(uid, oids[0], "20", "10")
    await WRITER.start()
    run_id = await _new_run(uid)
    before = await _snapshot(uid, oids)

    res = await WRITER.submit(_cmd(mid, uid, run_id, mode="full"))

    assert res["sold_count"] == 0
    assert res["blocked_reason"] == "market_not_open"
    assert await _snapshot(uid, oids) == before


@pytest.mark.asyncio
async def test_feed_prices_after_commit_and_no_candle(monkeypatch):
    mid, oids = await _seed_market(("0", "0"))
    uid = await _seed_user(cash="0", debt="0", username="tick_user")
    await _give_position(uid, oids[0], "20", "10")
    await WRITER.start()
    run_id = await _new_run(uid)

    calls: list = []
    monkeypatch.setattr(TICK_BROADCASTER, "feed_prices",
                        lambda *a, **k: calls.append(("prices", a, k)))
    monkeypatch.setattr(TICK_BROADCASTER, "feed_trade",
                        lambda *a, **k: calls.append(("trade", a, k)))

    res = await WRITER.submit(_cmd(mid, uid, run_id, mode="full"))

    assert res["sold_count"] == 1
    assert [c[0] for c in calls] == ["prices"], "强平只推空 trades 价格帧，不发成交事件"
    assert calls[0][1][0] == mid
    assert calls[0][1][1] == [float(p) for p in WRITER.get_state(mid).prices]
    assert not CANDLE_FLUSHER._pending, "LIQUIDATE 不写 candle"


@pytest.mark.asyncio
async def test_closed_run_rejected_before_any_sale():
    """终态 run 的轮次不得再执行：409 + 零写入（卖出发生在 run 校验之后）。"""
    from app.services.credit.runs import close_run

    mid, oids = await _seed_market(("0", "0"))
    uid = await _seed_user(cash="0", debt="50", username="closed_run_user")
    await _give_position(uid, oids[0], "20", "10")
    await WRITER.start()
    run_id = await _new_run(uid)
    async with async_session_maker() as s:
        async with s.begin():
            run = await s.get(LiquidationRun, run_id)
            await close_run(s, run=run, status="recovered",
                            now=datetime.now(timezone.utc))
    before = await _snapshot(uid, oids)

    with pytest.raises(HTTPException) as exc_info:
        await WRITER.submit(_cmd(mid, uid, run_id, mode="full"))

    assert exc_info.value.status_code == 409
    assert await _snapshot(uid, oids) == before


@pytest.mark.asyncio
async def test_foreign_run_id_rejected_without_replay_leak():
    """别人的 run_id 不得回放别人的动作，也不得卖自己的仓。"""
    mid, oids = await _seed_market(("0", "0"))
    uid_a = await _seed_user(cash="0", debt="50", username="foreign_a")
    uid_b = await _seed_user(cash="0", debt="50", username="foreign_b")
    await _give_position(uid_a, oids[0], "20", "10")
    await WRITER.start()
    run_b = await _new_run(uid_b)
    async with async_session_maker() as s:
        async with s.begin():
            s.add(LiquidationAction(
                run_id=run_b, user_id=uid_b, round_no=1, kind="repay_cash",
                proceeds=Decimal("7"), fee=ZERO, repaid=Decimal("7"),
                debt_after=Decimal("43"), cash_after=ZERO, executed={"sold_count": 0}))
    before = await _snapshot(uid_a, oids)

    with pytest.raises(HTTPException) as exc_info:
        await WRITER.submit(_cmd(mid, uid_a, run_b, mode="full"))

    assert exc_info.value.status_code == 409
    assert await _snapshot(uid_a, oids) == before


@pytest.mark.asyncio
async def test_replay_after_run_closed_still_returns_recorded_action():
    """崩溃恢复：已提交轮次在 run 关闭后重放仍返回既有动作，不会二次卖仓。"""
    from app.services.credit.runs import close_run

    mid, oids = await _seed_market(("0", "0"))
    uid = await _seed_user(cash="0", debt="50", username="replay_closed")
    await _give_position(uid, oids[0], "20", "10")
    await WRITER.start()
    run_id = await _new_run(uid)
    first = await WRITER.submit(_cmd(mid, uid, run_id, mode="full"))
    snap_first = await _snapshot(uid, oids)
    async with async_session_maker() as s:
        async with s.begin():
            run = await s.get(LiquidationRun, run_id)
            await close_run(s, run=run, status="recovered",
                            now=datetime.now(timezone.utc))

    second = await WRITER.submit(_cmd(mid, uid, run_id, mode="full"))

    assert second["replayed"] is True
    assert second["gross"] == first["gross"] and second["repaid"] == first["repaid"]
    assert await _snapshot(uid, oids) == snap_first


@pytest.mark.asyncio
async def test_audit_replay_fold_and_live_consistency():
    """完整链路（真实 buy → 统一组强平）通过 audit_replay 增量自检与 live 比对。"""
    mid, oids = await _seed_market(("0", "0"))
    async with async_session_maker() as s:
        async with s.begin():
            _, prices = calculate_lmsr_with_prices([0.0, 0.0], 100.0)
            audit_service.record(
                s, "market_create", market_id=mid, payload={"title": "wp5group"},
                market_after=audit_service.market_snapshot(
                    outcome_ids=oids, q=[ZERO, ZERO], b=100.0, prices=prices,
                    status="trading"))
    uid = await _seed_user(cash="1000", debt="5000", username="fold_user",
                           register=True, debt_accrued=datetime.now(timezone.utc))
    await WRITER.start()
    await WRITER.submit(BuyCmd(
        market_id=mid, outcome_id=oids[0], user_id=uid, username="fold_user",
        shares=Decimal("20"), max_cost=None, max_slippage_bps=None,
        accept_any_slippage=True))
    await WRITER.submit(BuyCmd(
        market_id=mid, outcome_id=oids[1], user_id=uid, username="fold_user",
        shares=Decimal("10"), max_cost=None, max_slippage_bps=None,
        accept_any_slippage=True))
    run_id = await _new_run(uid)

    res = await WRITER.submit(_cmd(
        mid, uid, run_id, mode="partial", partial_pct=Decimal("0.5"),
        fee_rate=Decimal("0.02"), daily_rate=Decimal("0.001")))

    assert res["sold_count"] == 2
    async with async_session_maker() as s:
        events = await audit_replay.load_events(s)
        snap, mismatches = audit_replay.fold(events, check=True)
        live_mismatches = await audit_replay.compare_with_live(s, snap)
    assert mismatches == []
    assert live_mismatches == []
    assert snap.users[uid].cash == res["cash_after"]
    assert snap.users[uid].debt == res["debt_after"]


@pytest.mark.asyncio
async def test_unified_flag_disables_legacy_liquidation_entries():
    from app.services import liquidation_service

    mid, oids = await _seed_market(("0", "0"))
    uid = await _seed_user(cash="0", debt="50", username="flag_user")
    await _give_position(uid, oids[0], "20", "10")
    await WRITER.start()
    before = await _snapshot(uid, oids)

    set_flags(CreditFlags(unified_credit_enabled=True))
    try:
        with pytest.raises(HTTPException) as exc_info:
            await WRITER.submit(LiquidateMarketCmd(
                market_id=mid, user_id=uid, mode="emergency", partial_pct=Decimal("1")))
        assert exc_info.value.status_code == 409

        with pytest.raises(RuntimeError):
            await liquidation_service.liquidate_user_split(
                uid, daily_rate=ZERO, trigger_source="test", partial_pct=Decimal("1"),
                target_margin=Decimal("0.5"), emergency_threshold=Decimal("0.1"),
                hard_threshold=Decimal("1.0"))

        async with async_session_maker() as s:
            async with s.begin():
                u = await s.get(User, uid)
                with pytest.raises(RuntimeError):
                    await liquidation_service.liquidate_user(
                        s, u, daily_rate=ZERO, trigger_source="test",
                        partial_pct=Decimal("1"), target_margin=Decimal("0.5"),
                        emergency_threshold=Decimal("0.1"))
    finally:
        clear_flags()

    assert await _snapshot(uid, oids) == before, "开关开启时 legacy 入口不得再卖仓"


@pytest.mark.asyncio
async def test_invalid_group_cmd_rejected_before_db():
    state = MarketState(
        market_id=1, b=100.0, outcome_ids=[1, 2], outcome_labels=["a", "b"],
        q_dec=[ZERO, ZERO], q=[0.0, 0.0], prices=[0.5, 0.5],
        status=MarketStatus.TRADING, closes_at=None)
    base = dict(market_id=1, user_id=1, run_id=1, round_no=1, mode="full",
                partial_pct=Decimal("1"), fee_rate=ZERO)

    from app.services.writer_ops import op_liquidate_group

    for overrides in (
        {"mode": "emergency"},
        {"run_id": 0},
        {"round_no": 0},
        {"fee_rate": Decimal("1")},
        {"fee_rate": Decimal("-0.01")},
        {"mode": "partial", "partial_pct": Decimal("0")},
        {"trigger_source": ""},
    ):
        with pytest.raises(HTTPException) as exc_info:
            await op_liquidate_group(state, LiquidateGroupCmd(**{**base, **overrides}))
        assert exc_info.value.status_code == 422, overrides
