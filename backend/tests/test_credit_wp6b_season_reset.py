"""WP6b：赛季重置对统一信贷状态的处理（新赛季不保留 run / FX，版本推进，冻结清除）。"""
import builtins
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import async_session_maker
from app.models.base import Market, Outcome, Position, SiteConfig, Transaction, User
from app.models.credit import LiquidationAction, LiquidationRun
from app.models.fx import FxPair, FxTreasury, FxWallet
from app.services.credit.version import economic_version_of

pytestmark = pytest.mark.asyncio


def _mod():
    from scripts import season_reset
    return season_reset


async def _count(model) -> int:
    async with async_session_maker() as s:
        return int((await s.execute(select(func.count()).select_from(model))).scalar_one())


async def _seed_state() -> int:
    """真人用户（冻结、版本 5）+ 活动数据 + FX 抵押 + 活跃强平 run。返回 user_id。"""
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="initial_balance", value="500", value_type="decimal"))
            u = User(
                username=f"human_{uuid.uuid4().hex[:6]}",
                casdoor_id=uuid.uuid4().hex,
                cash=Decimal("12"),
                debt=Decimal("30"),
                debt_last_accrued_at=datetime.now(timezone.utc),
                last_liquidated_at=datetime.now(timezone.utc),
                credit_frozen=True,
                economic_version=5,
            )
            s.add(u)
            await s.flush()
            m = Market(title="old", liquidity_b=100.0, tags="")
            s.add(m)
            await s.flush()
            o = Outcome(market_id=m.id, label="A", total_shares=Decimal("5"))
            s.add(o)
            await s.flush()
            s.add(Position(user_id=u.id, outcome_id=o.id, amount=Decimal("5"),
                           cost_basis=Decimal("2")))
            s.add(Transaction(user_id=u.id, outcome_id=o.id, type="buy",
                              shares=Decimal("5"), cost=Decimal("2")))
            pair = FxPair(currency_code=f"FX{uuid.uuid4().hex[:4]}", currency_name="X",
                          status="trading", gold_reserve=Decimal("100"),
                          foreign_reserve=Decimal("100"))
            s.add(pair)
            await s.flush()
            s.add(FxTreasury(pair_id=pair.id, gold_balance=Decimal("100"),
                             foreign_balance=Decimal("100")))
            s.add(FxWallet(user_id=u.id, pair_id=pair.id, foreign_amount=Decimal("3"),
                           cost_basis=Decimal("3")))
            run = LiquidationRun(
                user_id=u.id, status="active", trigger_source="scheduler",
                started_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
                pre_cash=Decimal("12"), pre_debt=Decimal("30"),
                pre_liquidation_equity=Decimal("-1"),
            )
            s.add(run)
            await s.flush()
            s.add(LiquidationAction(
                run_id=run.id, user_id=u.id, round_no=1, kind="blocked",
                blocked_reason="paused_group",
            ))
            return int(u.id)


async def test_dry_run_previews_credit_and_fx_tables_without_writes(capsys):
    uid = await _seed_state()
    assert await _mod().run(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "liquidation_run" in out and "liquidation_action" in out and "fx_pair" in out
    assert "未做任何修改" in out
    async with async_session_maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalars().one()
        assert economic_version_of(u) == 5
        assert u.credit_frozen is True
    assert await _count(LiquidationRun) == 1 and await _count(FxPair) == 1


async def test_reset_clears_runs_fx_and_bumps_economic_version(monkeypatch):
    uid = await _seed_state()
    monkeypatch.setattr(builtins, "input", lambda *_: "RESET")
    assert await _mod().run(dry_run=False) == 0  # 0 = replay 自检通过

    assert await _count(LiquidationRun) == 0
    assert await _count(LiquidationAction) == 0
    assert await _count(FxPair) == 0
    assert await _count(FxTreasury) == 0
    assert await _count(FxWallet) == 0
    assert await _count(Position) == 0
    assert await _count(Transaction) == 0

    async with async_session_maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalars().one()
        assert u.cash == Decimal("500.000000")
        assert u.debt == Decimal("0")
        assert u.credit_frozen is False
        assert u.debt_last_accrued_at is None and u.last_liquidated_at is None
        assert economic_version_of(u) == 6
