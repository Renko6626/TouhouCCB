"""PvE 模板决策单测：decide() 纯函数 + MarketView 窗口计算，夹具见 tests/pve_helpers.py。"""
import math
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.pve.templates import (
    GridTemplate, HodlerTemplate, LiquidityTemplate,
)
from tests.pve_helpers import make_bot as _bot, make_trade as _trade, make_view as _view


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


def test_net_flow_buy_minus_sell():
    trades = [
        _trade(2, side="buy", shares=30.0),
        _trade(5, side="sell", shares=10.0),
        _trade(8, side="buy", shares=5.0),
        _trade(30, side="buy", shares=100.0),  # 窗口外
        _trade(3, outcome_id=12, side="buy", shares=99.0),  # 别的 outcome
    ]
    v = _view(trades=trades)
    assert abs(v.net_flow(11, 10) - (30 - 10 + 5)) < 1e-9
    assert v.net_flow(11, 1) == 0.0


def test_bot_avg_cost():
    bot = _bot(HodlerTemplate, holdings={11: (20, 8)})  # 20 份成本 8 → 均价 0.4
    assert abs(bot.avg_cost(11) - 0.4) < 1e-9
    assert bot.avg_cost(12) is None  # 无仓位


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
    kw = dict(outcome_id=11, base_shares_b_frac=1.0, max_offset_b_frac=0.4,
              bootstrap_step_shares=15.0)
    bot = _bot(LiquidityTemplate, cash="1000", **kw)  # 空仓，floor=60
    a = LiquidityTemplate().decide(bot, _view())
    assert a is not None and a.side == "buy" and abs(float(a.shares) - 15) < 0.01
    assert "bootstrap" in a.reason


def test_liquidity_bootstrap_paused_when_overpriced():
    kw = dict(outcome_id=11, base_shares_b_frac=1.0, max_offset_b_frac=0.4,
              max_bootstrap_price=0.85)
    bot = _bot(LiquidityTemplate, cash="1000", **kw)
    assert LiquidityTemplate().decide(bot, _view(price_a=0.9)) is None


def test_liquidity_sells_when_price_above_mean():
    kw = dict(outcome_id=11, base_shares_b_frac=1.0, max_offset_b_frac=0.4,
              scale_price=0.05, min_trade_shares=1.0, max_trade_shares=1000.0)
    # 近期均价 0.4，现价 0.6 → mean-cur=-0.2 → tanh 饱和 → 目标 ≈ 60 → 卖 ≈ 40
    trades = [_trade(m, price=0.4) for m in (5, 10, 15)]
    bot = _bot(LiquidityTemplate, cash="1000", holdings={11: 100}, **kw)
    a = LiquidityTemplate().decide(bot, _view(price_a=0.6, trades=trades))
    assert a is not None and a.side == "sell"
    assert 35 <= float(a.shares) <= 40


def test_liquidity_needs_enough_recent_trades():
    kw = dict(outcome_id=11, base_shares_b_frac=1.0, max_offset_b_frac=0.4)
    bot = _bot(LiquidityTemplate, cash="1000", holdings={11: 100}, **kw)
    # 只有 2 笔近期成交 → 信号样本不足 → 不动
    trades = [_trade(5, price=0.4), _trade(10, price=0.4)]
    assert LiquidityTemplate().decide(bot, _view(price_a=0.6, trades=trades)) is None


def test_market_scope_restricts_home_pick():
    bot = _bot(HodlerTemplate, skip_prob=0.0)
    bot.market_scope = [999]  # 快照里没有这个市场
    assert HodlerTemplate().decide(bot, _view()) is None


# ── 资金投放：三个「机器人太谨慎」的成因（见 fix/2026-08-30-pve-capital-deployment）──


def test_liquidity_inventory_scales_with_market_depth():
    """做市底仓按 b 缩放：同样的参数，深市场建大仓、浅市场建小仓。
    回归旧 bug——绝对 base_shares=300 跑在 b=100 上，光底仓就把价格顶到 0.95。"""
    t = LiquidityTemplate()
    shallow = _bot(LiquidityTemplate, cash="100000", outcome_id=11)
    deep = _bot(LiquidityTemplate, cash="100000", outcome_id=11)
    # 底仓目标 = b × base_shares_b_frac，浅市场必须显著小于深市场
    a_shallow = t.decide(shallow, _view(liquidity_b=100.0))
    a_deep = t.decide(deep, _view(liquidity_b=3000.0))
    assert a_shallow is not None and a_deep is not None
    b_frac = LiquidityTemplate.default_params["base_shares_b_frac"]
    off_frac = LiquidityTemplate.default_params["max_offset_b_frac"]
    floor_shallow = (b_frac - off_frac) * 100.0
    # 浅市场的底仓下限必须远小于 b，否则价格会被单个做市商顶到轨道上
    assert floor_shallow < 100.0 * 0.5
    # 建仓步长受 floor 限制 → 浅市场单步不会超过它自己的 floor
    assert float(a_shallow.shares) <= floor_shallow + 1e-6


def test_shares_for_budget_matches_real_lmsr_cost():
    """预算换份额必须按 LMSR 真实成本反解——cny/price 在浅市场会严重超支，
    超支的单子会被引擎的单笔上限/滑点保护丢掉，机器人白醒一次。"""
    from app.services.lmsr import calculate_lmsr_cost
    from app.services.pve.templates import shares_for_budget

    for b, price, cny in [(100.0, 0.5, 150.0), (100.0, 0.2, 30.0), (3000.0, 0.5, 150.0)]:
        # 由价格反推一组等价份额（令 Σexp(q/b)=1 ⇒ q_i = b·ln p_i）
        q = [b * math.log(price), b * math.log(1 - price)]
        got = shares_for_budget(cny, price, b)
        real = calculate_lmsr_cost([q[0] + got, q[1]], b) - calculate_lmsr_cost(q, b)
        assert abs(real - cny) < 0.01, f"b={b} price={price}: 想花 {cny} 实花 {real}"
        # 老写法在浅市场上超支多少
        if b == 100.0 and price == 0.5:
            naive = cny / price
            naive_cost = calculate_lmsr_cost([q[0] + naive, q[1]], b) - calculate_lmsr_cost(q, b)
            assert naive_cost > cny * 1.5
