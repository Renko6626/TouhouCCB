"""WP1：loan_service.pending_debt（读写同源）与 economic_version / 新模型字段。"""
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.models.base import LiquidationEvent, User
from app.models.credit import ACTION_KINDS, FEE_CURRENCIES, RUN_STATUSES, LiquidationAction, LiquidationRun
from app.models.fx import FxPair
from app.services.credit.version import bump_economic_version, economic_version_of
from app.services.loan_service import accrue_interest, interest_factor, pending_debt

RATE = Decimal("0.01")
T0 = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
Q6 = Decimal("0.000001")


def _user(debt: str = "1000", at: datetime | None = T0) -> User:
    return User(username="pd", cash=Decimal("0"), debt=Decimal(debt), debt_last_accrued_at=at)


def test_pending_debt_noop_paths():
    assert pending_debt(_user(debt="0"), RATE, T0 + timedelta(days=1)) == Decimal("0")
    assert pending_debt(_user(at=None), RATE, T0 + timedelta(days=1)) == Decimal("1000")
    # elapsed <= 0：不倒退
    assert pending_debt(_user(), RATE, T0) == Decimal("1000")
    assert pending_debt(_user(), RATE, T0 - timedelta(seconds=5)) == Decimal("1000")


def test_pending_debt_is_pure_and_matches_accrue():
    """accrue_interest 就是 pending_debt 的写入版：同参数下逐位一致。"""
    pending = _user()
    snapshot = (pending.debt, pending.debt_last_accrued_at)
    now = T0 + timedelta(hours=6)
    value = pending_debt(pending, RATE, now)
    assert (pending.debt, pending.debt_last_accrued_at) == snapshot   # 未修改

    accrued = _user()
    accrue_interest(accrued, RATE, now)
    assert value == accrued.debt
    assert accrued.debt_last_accrued_at == now
    assert value == (Decimal("1000") * interest_factor(RATE, 6 * 3600)).quantize(Q6)


def test_pending_debt_quantizes_to_6dp():
    tiny = _user(debt="1")
    value = pending_debt(tiny, RATE, T0 + timedelta(seconds=1))
    assert value == Decimal("1")
    assert value.as_tuple().exponent >= -6


def test_pending_debt_handles_mixed_timezone_awareness():
    """SQLite 读回的 naive 时间按 UTC 解释；混用不抛 TypeError。"""
    aware = _user()
    naive = _user()
    naive.debt_last_accrued_at = T0.replace(tzinfo=None)
    now_aware = T0 + timedelta(hours=3)
    now_naive = now_aware.replace(tzinfo=None)
    assert pending_debt(aware, RATE, now_aware) == pending_debt(naive, RATE, now_naive)
    # 交叉组合也一致（估值侧传 aware now、库里 naive）
    assert pending_debt(naive, RATE, now_aware) == pending_debt(aware, RATE, now_naive)


def test_accrue_interest_keeps_dust_debt_and_advances_timestamp():
    """量化后无变化时不推进时间戳（原有语义不能在重构中丢）。"""
    u = _user(debt="0.000001")
    later = T0 + timedelta(seconds=10)
    accrue_interest(u, Decimal("0.000001"), later)
    assert u.debt == Decimal("0.000001")
    assert u.debt_last_accrued_at == T0


def test_bump_economic_version_increments_in_place():
    u = User(username="v", cash=Decimal("0"), debt=Decimal("0"))
    assert economic_version_of(u) == 0
    assert u.economic_version == 0
    assert bump_economic_version(u) == 1
    assert bump_economic_version(u) == 2
    assert economic_version_of(u) == 2


def test_new_model_fields_and_constants_are_frozen():
    user_fields = User.model_fields
    assert user_fields["economic_version"].default == 0
    assert user_fields["credit_frozen"].default is False

    fx_fields = FxPair.model_fields
    assert fx_fields["reduce_only"].default is False

    event_fields = LiquidationEvent.model_fields
    assert event_fields["run_id"].default is None
    assert event_fields["product"].default is None
    assert LiquidationEvent.__table__.c.product.type.length == 8
    assert LiquidationEvent.__table__.c.run_id.nullable is True

    run_fields = LiquidationRun.model_fields
    assert set(RUN_STATUSES) == {"active", "recovered", "insolvent", "blocked", "stopped"}
    assert run_fields["status"].default == "active"
    assert run_fields["next_round"].default == 1
    assert run_fields["rounds"].default == 0
    assert LiquidationRun.__tablename__ == "liquidation_run"

    action_fields = LiquidationAction.model_fields
    assert set(ACTION_KINDS) == {"sell_group", "cover_group", "repay_cash", "repay_only", "blocked", "stopped"}
    assert set(FEE_CURRENCIES) == {"gold", "foreign"}
    assert {fk.target_fullname for fk in LiquidationAction.__table__.foreign_keys} == {
        "liquidation_run.id", "user.id",
    }
    assert LiquidationAction.__tablename__ == "liquidation_action"


def test_short_interest_is_pure_and_new_principal_does_not_pay_old_interest():
    from app.models.fx import FxShortPosition
    from app.services.fx.shorts import pending_short_debt, accrue_short_interest
    p = FxShortPosition(user_id=1, pair_id=1, principal_foreign=Decimal('100'), interest_last_accrued_at=T0)
    now = T0 + timedelta(days=1)
    assert pending_short_debt(p, RATE, now) == Decimal('101.000000')
    assert p.interest_foreign == 0
    assert accrue_short_interest(p, RATE, now) == Decimal('1.000000')
    assert p.principal_foreign == Decimal('100')
    p.principal_foreign += Decimal('100')
    assert pending_short_debt(p, RATE, now) == Decimal('201.000000')
    assert pending_short_debt(p, RATE, now + timedelta(days=1)) == Decimal('203.010000')


def test_short_dust_and_mixed_utc_follow_gold_interest_semantics():
    from app.models.fx import FxShortPosition
    from app.services.fx.shorts import pending_short_debt, accrue_short_interest
    p = FxShortPosition(user_id=1, pair_id=1, principal_foreign=Q6, interest_last_accrued_at=T0.replace(tzinfo=None))
    assert accrue_short_interest(p, RATE, T0 + timedelta(seconds=10)) == 0
    assert p.interest_last_accrued_at == T0.replace(tzinfo=None)
    assert pending_short_debt(p, RATE, T0 + timedelta(days=1)) == Q6
