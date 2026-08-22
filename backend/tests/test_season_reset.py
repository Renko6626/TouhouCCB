"""scripts/season_reset.py：清活动数据、保留用户/配置/称号，现金还原，事件流重新锚定。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import builtins
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.database import async_session_maker
from app.models.audit import AuditEvent
from app.models.base import Market, Outcome, OutcomeCandle, Position, SiteConfig, Transaction, User
from app.models.ledger import LedgerEntry
from app.models.title import Title, UserTitle
from app.services import audit_service



@pytest_asyncio.fixture(autouse=True)
async def _seed(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="initial_balance", value="500", value_type="decimal"))
            u1 = User(username="a", casdoor_id="a", cash=Decimal("12"), debt=Decimal("30"),
                      debt_last_accrued_at=datetime.now(timezone.utc),
                      last_liquidated_at=datetime.now(timezone.utc), is_superuser=True)
            u2 = User(username="b", casdoor_id="b", cash=Decimal("999"))
            m = Market(title="old", liquidity_b=100.0, tags="")
            s.add_all([u1, u2, m]); await s.flush()
            o = Outcome(market_id=m.id, label="A", total_shares=Decimal("5")); s.add(o); await s.flush()
            m.winning_outcome_id = o.id
            s.add(Position(user_id=u1.id, outcome_id=o.id, amount=Decimal("5"), cost_basis=Decimal("2")))
            s.add(Transaction(user_id=u1.id, outcome_id=o.id, type="buy", shares=Decimal("5"), cost=Decimal("2")))
            s.add(OutcomeCandle(outcome_id=o.id, interval="10s",
                                bucket_start=datetime.now(timezone.utc),
                                open_price=Decimal("0.5"), high_price=Decimal("0.5"),
                                low_price=Decimal("0.5"), close_price=Decimal("0.5"),
                                volume_shares=Decimal("5"), n_trades=1))
            s.add(LedgerEntry(user_id=u1.id, entry_type="borrow", cash_delta=Decimal("30"), debt_delta=Decimal("30"),
                              cash_after=Decimal("12"), debt_after=Decimal("30")))
            t = Title(code="vip", name="VIP", color="#000"); s.add(t); await s.flush()
            s.add(UserTitle(user_id=u2.id, title_id=t.id, source="admin"))
            audit_service.record(s, "trade_buy", user_id=u1.id, payload={"shares": "5", "cost": "2"})


def _mod():
    # 延迟 import：在 collection 阶段 import 会让 conftest 的 drop_all/create_all 报 table already exists
    # （脚本模块顶层 import 了全部 model 模块；原因未深究，按模块内 import 规避）
    from scripts import season_reset
    return season_reset


async def _count(model):
    async with async_session_maker() as s:
        return int((await s.execute(select(func.count()).select_from(model))).scalar_one())


@pytest.mark.asyncio
async def test_dry_run_changes_nothing():
    assert await _mod().run(dry_run=True) == 0
    assert await _count(Market) == 1 and await _count(AuditEvent) == 1
    async with async_session_maker() as s:
        assert (await s.execute(select(User.cash).where(User.username == "b"))).scalar_one() == Decimal("999")


@pytest.mark.asyncio
async def test_reset_requires_confirmation(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda *_: "no")
    assert await _mod().run(dry_run=False) == 1
    assert await _count(Market) == 1


@pytest.mark.asyncio
async def test_reset_clears_activity_keeps_users_and_reanchors(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda *_: "RESET")
    assert await _mod().run(dry_run=False) == 0
    for model in (Market, Outcome, Position, Transaction, LedgerEntry, OutcomeCandle):
        assert await _count(model) == 0, model   # OutcomeCandle 复合主键无 id，序列重置须跳过它
    assert await _count(User) == 2 and await _count(Title) == 1 and await _count(UserTitle) == 1
    assert await _count(SiteConfig) >= 1
    async with async_session_maker() as s:
        users = (await s.execute(select(User).order_by(User.id))).scalars().all()
        for u in users:
            assert u.cash == Decimal("500") and u.debt == 0
            assert u.debt_last_accrued_at is None and u.last_liquidated_at is None
        assert users[0].is_superuser is True
        evs = (await s.execute(select(AuditEvent).order_by(AuditEvent.id))).scalars().all()
        assert [e.event_type for e in evs] == ["user_register", "user_register"]
        assert evs[0].id == 1 and evs[0].payload["source"] == "season_reset"
        assert Decimal(evs[1].user_after["cash"]) == Decimal("500")


def test_outcome_candle_excluded_from_sequence_reset():
    """复合主键表 outcome_candle 无 id 列，必须排除在序列重置外（否则 PG 报 UndefinedColumn）。"""
    mod = _mod()
    assert "outcome_candle" in mod._NO_ID_SEQUENCE
    assert "transaction" not in mod._NO_ID_SEQUENCE
    assert "market_required_title" not in mod._NO_ID_SEQUENCE
