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
from app.models.base import LiquidationEvent, Market, Outcome, OutcomeCandle, Position, SiteConfig, Transaction, User
from app.models.bot import BotProfile
from app.models.credit import LiquidationAction, LiquidationRun
from app.models.redemption import RedemptionPartner, RedemptionBatch, RedemptionCode, RedemptionTransaction, DanmukuExchange
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


@pytest.mark.asyncio
async def test_reset_removes_unreferenced_bots_and_disables_engine(monkeypatch):
    async with async_session_maker() as s:
        async with s.begin():
            bot = User(username="old-bot", is_bot=True, cash=Decimal("900"))
            s.add(bot)
            await s.flush()
            s.add(BotProfile(user_id=bot.id, template="random", params={}))
            s.add(SiteConfig(key="pve_enabled", value="true", value_type="bool"))
    monkeypatch.setattr(builtins, "input", lambda *_: "RESET")
    assert await _mod().run(dry_run=False) == 0
    assert await _count(BotProfile) == 0
    assert await _count(User) == 2
    async with async_session_maker() as s:
        assert (await s.execute(select(SiteConfig.value).where(SiteConfig.key == "pve_enabled"))).scalar_one() == "false"
        assert (await s.execute(select(User.cash).where(User.username == "a"))).scalar_one() == Decimal("500")


@pytest.mark.asyncio
async def test_reset_preserves_redemption_ownership_fulfillment_and_audit(monkeypatch):
    async with async_session_maker() as s:
        async with s.begin():
            buyer = (await s.execute(select(User).where(User.username == "b"))).scalar_one()
            bot = User(username="bot-with-history", is_bot=True, cash=Decimal("800"))
            s.add(bot)
            await s.flush()
            bot_id = bot.id
            s.add(BotProfile(user_id=bot.id, template="random", params={}))
            partner = RedemptionPartner(name="keep-partner")
            s.add(partner)
            await s.flush()
            batch = RedemptionBatch(partner_id=partner.id, name="keep-batch", unit_price=Decimal("10"))
            s.add(batch)
            await s.flush()
            when = datetime.now(timezone.utc)
            code = RedemptionCode(batch_id=batch.id, code_string="keep-code", status="sold",
                                  bought_by_user_id=bot.id, bought_at=when,
                                  redeemed_at=when, redeemed_by_admin_id=buyer.id,
                                  redemption_note="实物已发放")
            s.add(code)
            await s.flush()
            code_id = code.id
            kept = audit_service.record(s, "redeem_fulfill", user_id=bot.id,
                                        operator_user_id=buyer.id, ref_table="redemption_code",
                                        ref_id=code.id, payload={"note": "实物已发放"})
            await s.flush()
            event_id = kept.id
    monkeypatch.setattr(builtins, "input", lambda *_: "RESET")
    assert await _mod().run(dry_run=False) == 0
    assert await _count(BotProfile) == 0
    async with async_session_maker() as s:
        code = await s.get(RedemptionCode, code_id)
        assert code.status == "sold" and code.bought_by_user_id == bot_id
        assert code.redeemed_at is not None and code.redemption_note == "实物已发放"
        bot = await s.get(User, bot_id)
        assert bot is not None and bot.is_bot and not bot.is_active
        assert bot.cash == 0 and bot.debt == 0
        kept = await s.get(AuditEvent, event_id)
        assert kept is not None and kept.event_type == "redeem_fulfill"
        assert kept.payload["note"] == "实物已发放"
        registrations = list((await s.execute(select(AuditEvent).where(AuditEvent.event_type == "user_register"))).scalars())
        assert len(registrations) == 3


@pytest.mark.asyncio
async def test_dry_run_leaves_bots_and_redemption_audit_untouched():
    async with async_session_maker() as s:
        async with s.begin():
            bot = User(username="dry-bot", is_bot=True, cash=Decimal("800"))
            s.add(bot)
            await s.flush()
            s.add(BotProfile(user_id=bot.id, template="random", params={}))
            audit_service.record(s, "redeem_fulfill_revoke", user_id=bot.id,
                                 payload={"reason": "keep this reason"})
    assert await _mod().run(dry_run=True) == 0
    assert await _count(BotProfile) == 1
    assert await _count(User) == 3
    assert await _count(AuditEvent) == 2


@pytest.mark.asyncio
async def test_invalid_initial_balance_leaves_the_season_untouched(monkeypatch):
    async with async_session_maker() as s:
        async with s.begin():
            initial = (await s.execute(select(SiteConfig).where(SiteConfig.key == "initial_balance"))).scalar_one()
            initial.value = "NaN"
    monkeypatch.setattr(builtins, "input", lambda *_: "RESET")
    with pytest.raises(ValueError, match="initial_balance"):
        await _mod().run(dry_run=False)
    assert await _count(Market) == 1
    assert await _count(Position) == 1


@pytest.mark.asyncio
async def test_failed_audit_validation_rolls_back_the_entire_reset(monkeypatch):
    mod = _mod()
    original_record = mod.audit_service.record

    def corrupt_snapshot(*args, **kwargs):
        event = original_record(*args, **kwargs)
        event.user_after = {**event.user_after, "cash": "8888"}
        return event

    monkeypatch.setattr(mod.audit_service, "record", corrupt_snapshot)
    monkeypatch.setattr(builtins, "input", lambda *_: "RESET")
    assert await mod.run(dry_run=False) == 2
    assert await _count(Market) == 1
    assert await _count(Position) == 1
    assert await _count(Transaction) == 1
    assert await _count(AuditEvent) == 1
    async with async_session_maker() as s:
        assert (await s.execute(select(User.cash).where(User.username == "a"))).scalar_one() == Decimal("12")
        assert (await s.execute(select(User.cash).where(User.username == "b"))).scalar_one() == Decimal("999")


@pytest.mark.asyncio
async def test_human_identity_account_flags_and_equipped_title_survive(monkeypatch):
    when = datetime.now(timezone.utc)
    async with async_session_maker() as s:
        async with s.begin():
            user = (await s.execute(select(User).where(User.username == "b"))).scalar_one()
            title = (await s.execute(select(Title).where(Title.name == "VIP"))).scalar_one()
            uid, tid = user.id, title.id
            user.email = "human@example.test"
            user.is_active = False
            user.equipped_title_id = tid
            user.tos_accepted_at = when
    monkeypatch.setattr(builtins, "input", lambda *_: "RESET")
    assert await _mod().run(dry_run=False) == 0
    async with async_session_maker() as s:
        user = await s.get(User, uid)
        assert user.username == "b" and user.casdoor_id == "b"
        assert user.email == "human@example.test" and not user.is_active
        assert user.equipped_title_id == tid and user.tos_accepted_at is not None
        membership = (await s.execute(select(UserTitle).where(UserTitle.user_id == uid))).scalar_one()
        assert membership.title_id == tid


@pytest.mark.asyncio
async def test_bot_referenced_as_title_granter_stays_disabled_without_breaking_title(monkeypatch):
    async with async_session_maker() as s:
        async with s.begin():
            human = (await s.execute(select(User).where(User.username == "a"))).scalar_one()
            title = (await s.execute(select(Title).where(Title.name == "VIP"))).scalar_one()
            bot = User(username="bot-granter", is_bot=True, is_superuser=True, cash=Decimal("800"))
            s.add(bot)
            await s.flush()
            uid, bot_id = human.id, bot.id
            s.add(UserTitle(user_id=human.id, title_id=title.id, source="admin", granted_by_admin_id=bot.id))
            s.add(BotProfile(user_id=bot.id, template="random", params={}))
    monkeypatch.setattr(builtins, "input", lambda *_: "RESET")
    assert await _mod().run(dry_run=False) == 0
    async with async_session_maker() as s:
        bot = await s.get(User, bot_id)
        assert bot is not None and not bot.is_active and not bot.is_superuser and bot.cash == 0
        human = await s.get(User, uid)
        assert human.is_superuser and human.is_active
        title_record = (await s.execute(select(UserTitle).where(UserTitle.user_id == uid))).scalar_one()
        assert title_record.granted_by_admin_id == bot_id


@pytest.mark.asyncio
async def test_exchange_purchases_and_danmuku_activation_codes_are_preserved(monkeypatch):
    async with async_session_maker() as s:
        async with s.begin():
            user = (await s.execute(select(User).where(User.username == "b"))).scalar_one()
            s.add(RedemptionTransaction(user_id=user.id, batch_name_snapshot="保留购买凭证", amount=Decimal("5")))
            s.add(DanmukuExchange(user_id=user.id, qq_user_id="123456", room_id="room",
                                 yuan=Decimal("1"), huo=Decimal("1"), amount=Decimal("2"),
                                 code_string="keep-danmuku-activation"))
            audit_service.record(s, "redeem_purchase", user_id=user.id,
                                 payload={"amount": "5"}, user_after={"cash": "994", "debt": "0"})
            audit_service.record(s, "danmuku_exchange", user_id=user.id,
                                 payload={"amount": "2"}, user_after={"cash": "992", "debt": "0"})
    monkeypatch.setattr(builtins, "input", lambda *_: "RESET")
    assert await _mod().run(dry_run=False) == 0
    assert await _count(RedemptionTransaction) == 1
    async with async_session_maker() as s:
        activation = (await s.execute(select(DanmukuExchange))).scalar_one()
        assert activation.code_string == "keep-danmuku-activation"
        events = (await s.execute(select(AuditEvent).where(AuditEvent.event_type.in_(("redeem_purchase", "danmuku_exchange"))).order_by(AuditEvent.id))).scalars().all()
        assert [event.payload["amount"] for event in events] == ["5", "2"]
        assert (await s.execute(select(User.cash).where(User.username == "b"))).scalar_one() == Decimal("500")


@pytest.mark.asyncio
async def test_reset_clears_credit_runs_audit_and_unfreezes(monkeypatch):
    """WP1：新表 / 新审计类型纳入 CLEAR_ORDER；坏账冻结清除、经济版本推进；replay 自检通过。"""
    async with async_session_maker() as s:
        async with s.begin():
            human = (await s.execute(select(User).where(User.username == "a"))).scalar_one()
            human.credit_frozen = True
            human.economic_version = 5
            s.add(human)
            await s.flush()
            run = LiquidationRun(
                user_id=human.id, status="active", trigger_source="scheduler",
                started_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
                pre_cash=Decimal("12"), pre_debt=Decimal("30"),
                pre_liquidation_equity=Decimal("-1"),
            )
            s.add(run)
            await s.flush()
            s.add(LiquidationAction(
                run_id=run.id, user_id=human.id, round_no=1, kind="blocked",
                blocked_reason="paused_group",
            ))
            s.add(LiquidationEvent(
                user_id=human.id, triggered_at=datetime.now(timezone.utc),
                pre_cash=Decimal("12"), pre_debt=Decimal("30"), pre_holdings_value=Decimal("1"),
                pre_net_worth=Decimal("-17"), sold_positions_count=0,
                total_proceeds=Decimal("0"), repaid_amount=Decimal("0"),
                remaining_debt=Decimal("30"), post_cash=Decimal("12"),
                trigger_source="scheduler", mode="emergency",
                run_id=run.id, product="lmsr",
            ))
            for event_type in ("liquidation_run_start", "liquidation_action",
                               "liquidation_blocked", "credit_freeze_set"):
                audit_service.record(
                    s, event_type, user_id=human.id, payload={"run_id": run.id},
                    user_after=audit_service.user_snapshot(human),
                )

    monkeypatch.setattr(builtins, "input", lambda *_: "RESET")
    assert await _mod().run(dry_run=False) == 0   # 返回 0 = replay 自检通过

    assert await _count(LiquidationRun) == 0
    assert await _count(LiquidationAction) == 0
    assert await _count(LiquidationEvent) == 0
    async with async_session_maker() as s:
        human = (await s.execute(select(User).where(User.username == "a"))).scalar_one()
        assert human.credit_frozen is False
        assert human.economic_version == 6   # 现金/债务被改写 → 版本必须推进
        anchors = (await s.execute(select(AuditEvent.event_type))).scalars().all()
        assert set(anchors) == {"user_register"} and len(anchors) == 2
