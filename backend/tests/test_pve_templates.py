"""PvE 模板决策单测：decide() 纯函数 + MarketView 窗口计算，全部夹具驱动，无 DB。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services.pve.templates import (
    BotState,
    GridTemplate,
    HodlerTemplate,
    LiquidityTemplate,
    MarketBrief,
    MarketView,
    OutcomeView,
    TradeBrief,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _view(price_a=0.5, price_b=0.5, trades=None) -> MarketView:
    """单市场双 outcome（id 11/12，market 1）夹具。"""
    return MarketView(
        now=NOW,
        outcomes={
            11: OutcomeView(11, 1, "A", price_a),
            12: OutcomeView(12, 1, "B", price_b),
        },
        markets={1: MarketBrief(1, [11, 12])},
        trades=trades or [],
    )


def _bot(template_cls, cash="100", holdings=None, seed=1, **param_overrides) -> BotState:
    params = dict(template_cls.default_params)
    params.update(param_overrides)
    return BotState(
        user_id=1, profile_id=1, username="bot",
        params=params, market_scope=None,
        cash=Decimal(cash),
        holdings={k: (Decimal(str(a)), Decimal("0")) for k, a in (holdings or {}).items()},
        memory={}, rng=random.Random(seed),
    )


def _trade(minutes_ago: float, outcome_id=11, price=0.5, post=None, side="buy", shares=10.0):
    return TradeBrief(
        ts=NOW - timedelta(minutes=minutes_ago),
        outcome_id=outcome_id, market_id=1, side=side,
        shares=shares, price=price, market_prices_post=post,
    )


# ── MarketView 窗口计算 ─────────────────────────────────────────────────


def test_window_change_no_trades_is_zero():
    assert _view().window_change(11, 10) == 0.0


def test_window_change_uses_oldest_trade_in_window():
    trades = [
        _trade(2, post=[0.55, 0.45]),
        _trade(8, post=[0.40, 0.60]),   # 窗口内最老一笔 → 起点价 0.40
        _trade(30, post=[0.10, 0.90]),  # 窗口外，不用
    ]
    v = _view(price_a=0.5, trades=trades)
    assert abs(v.window_change(11, 10) - (0.5 - 0.40)) < 1e-9
    # 同市场另一 outcome 用同一笔快照的对应下标
    assert abs(v.window_change(12, 10) - (0.5 - 0.60)) < 1e-9


def test_max_abs_change_scoped():
    trades = [_trade(5, post=[0.40, 0.60])]
    v = _view(price_a=0.5, price_b=0.5, trades=trades)
    change, oid = v.max_abs_change(10)
    assert oid in (11, 12) and abs(change - 0.10) < 1e-9
    # 限定 outcome 集合
    change11, oid11 = v.max_abs_change(10, outcome_ids=[11])
    assert oid11 == 11


# ── hodler ──────────────────────────────────────────────────────────────


def test_hodler_buys_home_outcome():
    bot = _bot(HodlerTemplate, skip_prob=0.0, buy_cny_min=5, buy_cny_max=5)
    a = HodlerTemplate().decide(bot, _view())
    assert a is not None and a.side == "buy"
    assert a.outcome_id in (11, 12)
    # ≈5 cny @ 0.5 → ≈10 份
    assert 9 <= float(a.shares) <= 11
    # 主场记住了，下次还是它
    assert bot.memory["home_outcome_id"] == a.outcome_id


def test_hodler_respects_max_price():
    bot = _bot(HodlerTemplate, skip_prob=0.0, max_price=0.9, outcome_id=11)
    assert HodlerTemplate().decide(bot, _view(price_a=0.95)) is None


def test_hodler_respects_cash_reserve():
    bot = _bot(HodlerTemplate, cash="1", skip_prob=0.0, cash_reserve_cny=1.0)
    assert HodlerTemplate().decide(bot, _view()) is None


def test_hodler_skip_prob_one_never_trades():
    bot = _bot(HodlerTemplate, skip_prob=1.0)
    assert HodlerTemplate().decide(bot, _view()) is None


# ── grid ────────────────────────────────────────────────────────────────


def test_grid_buys_low_sells_high():
    # 显式网格 0.3~0.7，5 条线，每格 10 份
    kw = dict(outcome_id=11, price_low=0.3, price_high=0.7, levels=5,
              shares_per_level=10.0, min_trade_shares=1.0, max_trade_shares=100.0)
    # 价格 0.35：上方 4 条线（0.4/0.5/0.6/0.7）→ 目标 40，空仓 → 买
    bot = _bot(GridTemplate, cash="1000", **kw)
    a = GridTemplate().decide(bot, _view(price_a=0.35))
    assert a is not None and a.side == "buy" and abs(float(a.shares) - 40) < 1
    # 价格 0.65：上方 1 条线 → 目标 10，持仓 40 → 卖 30
    bot2 = _bot(GridTemplate, cash="1000", holdings={11: 40}, **kw)
    bot2.memory["grid_lines"] = [0.3, 0.4, 0.5, 0.6, 0.7]
    a2 = GridTemplate().decide(bot2, _view(price_a=0.65))
    assert a2 is not None and a2.side == "sell" and abs(float(a2.shares) - 30) < 1


def test_grid_dead_zone_no_trade():
    kw = dict(outcome_id=11, price_low=0.3, price_high=0.7, levels=5,
              shares_per_level=10.0, min_trade_shares=5.0)
    bot = _bot(GridTemplate, cash="1000", holdings={11: 40}, **kw)
    bot.memory["grid_lines"] = [0.3, 0.4, 0.5, 0.6, 0.7]
    # 价格 0.35 → 目标 40 = 持仓 → 不动
    assert GridTemplate().decide(bot, _view(price_a=0.35)) is None


def test_grid_buy_limited_by_cash():
    kw = dict(outcome_id=11, price_low=0.3, price_high=0.7, levels=5,
              shares_per_level=100.0, min_trade_shares=1.0, max_trade_shares=1000.0)
    bot = _bot(GridTemplate, cash="10", **kw)
    a = GridTemplate().decide(bot, _view(price_a=0.35))
    # 现金 10 × 0.95 / 0.35 ≈ 27 份封顶
    assert a is not None and float(a.shares) <= 28


# ── liquidity ───────────────────────────────────────────────────────────


def test_liquidity_bootstraps_toward_floor():
    kw = dict(outcome_id=11, base_shares=100.0, max_offset_shares=40.0,
              bootstrap_step_shares=15.0)
    bot = _bot(LiquidityTemplate, cash="1000", **kw)  # 空仓，floor=60
    a = LiquidityTemplate().decide(bot, _view())
    assert a is not None and a.side == "buy" and abs(float(a.shares) - 15) < 0.01
    assert "bootstrap" in a.reason


def test_liquidity_bootstrap_paused_when_overpriced():
    kw = dict(outcome_id=11, base_shares=100.0, max_offset_shares=40.0,
              max_bootstrap_price=0.85)
    bot = _bot(LiquidityTemplate, cash="1000", **kw)
    assert LiquidityTemplate().decide(bot, _view(price_a=0.9)) is None


def test_liquidity_sells_when_price_above_mean():
    kw = dict(outcome_id=11, base_shares=100.0, max_offset_shares=40.0,
              scale_price=0.05, min_trade_shares=1.0, max_trade_shares=1000.0)
    # 近期均价 0.4，现价 0.6 → mean-cur=-0.2 → tanh 饱和 → 目标 ≈ 60 → 卖 ≈ 40
    trades = [_trade(m, price=0.4) for m in (5, 10, 15)]
    bot = _bot(LiquidityTemplate, cash="1000", holdings={11: 100}, **kw)
    a = LiquidityTemplate().decide(bot, _view(price_a=0.6, trades=trades))
    assert a is not None and a.side == "sell"
    assert 35 <= float(a.shares) <= 40


def test_liquidity_needs_enough_recent_trades():
    kw = dict(outcome_id=11, base_shares=100.0, max_offset_shares=40.0)
    bot = _bot(LiquidityTemplate, cash="1000", holdings={11: 100}, **kw)
    # 只有 2 笔近期成交 → 信号样本不足 → 不动
    trades = [_trade(5, price=0.4), _trade(10, price=0.4)]
    assert LiquidityTemplate().decide(bot, _view(price_a=0.6, trades=trades)) is None


def test_market_scope_restricts_home_pick():
    bot = _bot(HodlerTemplate, skip_prob=0.0)
    bot.market_scope = [999]  # 快照里没有这个市场
    assert HodlerTemplate().decide(bot, _view()) is None
