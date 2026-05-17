import time
from decimal import Decimal
from pathlib import Path

import pytest

from thccb_quant.broker.risk import RiskConfig, RiskGuard
from thccb_quant.errors import RiskRejected


@pytest.fixture
def cfg() -> RiskConfig:
    return RiskConfig(
        single_order_cap_cny=Decimal("50"),
        daily_loss_cap_cny=Decimal("100"),
        daily_turnover_cap_cny=Decimal("2000"),
        max_slippage_bps=300,
        min_seconds_between_orders=3,
    )


@pytest.fixture
def state_dir(tmp_path: Path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def test_pass_normal(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    g.check(outcome_id=1, side="buy", cost=Decimal("10"),
            max_slippage_bps=200, turnover_today=Decimal("0"),
            net_pnl_today=Decimal("0"))


def test_kill_switch_blocks(cfg, state_dir):
    (state_dir / "KILL").touch()
    g = RiskGuard(cfg, state_dir)
    with pytest.raises(RiskRejected, match="kill"):
        g.check(outcome_id=1, side="buy", cost=Decimal("10"),
                max_slippage_bps=200, turnover_today=Decimal("0"),
                net_pnl_today=Decimal("0"))


def test_single_cap(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    with pytest.raises(RiskRejected, match="single"):
        g.check(outcome_id=1, side="buy", cost=Decimal("51"),
                max_slippage_bps=200, turnover_today=Decimal("0"),
                net_pnl_today=Decimal("0"))


def test_slippage_over_config(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    with pytest.raises(RiskRejected, match="slippage"):
        g.check(outcome_id=1, side="buy", cost=Decimal("10"),
                max_slippage_bps=400, turnover_today=Decimal("0"),
                net_pnl_today=Decimal("0"))


def test_daily_loss_cap(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    with pytest.raises(RiskRejected, match="loss"):
        g.check(outcome_id=1, side="buy", cost=Decimal("10"),
                max_slippage_bps=200, turnover_today=Decimal("0"),
                net_pnl_today=Decimal("-100"))


def test_daily_turnover_cap(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    with pytest.raises(RiskRejected, match="turnover"):
        g.check(outcome_id=1, side="buy", cost=Decimal("10"),
                max_slippage_bps=200, turnover_today=Decimal("1999"),
                net_pnl_today=Decimal("0"))


def test_cooldown(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    g.mark_order(outcome_id=1)
    with pytest.raises(RiskRejected, match="cooldown"):
        g.check(outcome_id=1, side="buy", cost=Decimal("10"),
                max_slippage_bps=200, turnover_today=Decimal("0"),
                net_pnl_today=Decimal("0"))
    time.sleep(3.1)
    g.check(outcome_id=1, side="buy", cost=Decimal("10"),
            max_slippage_bps=200, turnover_today=Decimal("0"),
            net_pnl_today=Decimal("0"))


def test_cooldown_per_outcome(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    g.mark_order(outcome_id=1)
    # 其他 outcome 不受冷却限制
    g.check(outcome_id=2, side="buy", cost=Decimal("10"),
            max_slippage_bps=200, turnover_today=Decimal("0"),
            net_pnl_today=Decimal("0"))
