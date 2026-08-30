"""PvE 模板测试共享夹具——写新模板的单测直接 import 这里，别复制粘贴。

用法（详见 docs/pve.md「测试」一节）：

    from tests.pve_helpers import NOW, make_bot, make_trade, make_view
    a = MyTemplate().decide(make_bot(MyTemplate, cash="100"), make_view(price_a=0.4))

夹具市场：单市场 market_id=1，双 outcome id=11("A") / 12("B")。
"""
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services.pve.templates import (
    BotState, MarketBrief, MarketView, OutcomeView, TradeBrief,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def make_view(price_a: float = 0.5, price_b: float = 0.5, trades=None, sentiment=None,
              liquidity_b: float = 100.0) -> MarketView:
    """单市场双 outcome（id 11/12，market 1）快照。sentiment={outcome_id: 倾斜幅度}。
    liquidity_b=市场 LMSR 深度，做市类模板按它缩放库存。"""
    return MarketView(
        now=NOW,
        outcomes={
            11: OutcomeView(11, 1, "A", price_a),
            12: OutcomeView(12, 1, "B", price_b),
        },
        markets={1: MarketBrief(1, [11, 12], liquidity_b)},
        trades=trades or [],
        sentiment=sentiment or {},
    )


def make_bot(template_cls, cash="100", holdings=None, seed=1, **param_overrides) -> BotState:
    """按模板默认值 + 覆盖构造 BotState。holdings={outcome_id: amount} 或
    {outcome_id: (amount, cost_basis)}。seed 固定 → 决策可复现。"""
    params = dict(template_cls.default_params)
    params.update(param_overrides)
    parsed = {}
    for k, v in (holdings or {}).items():
        amt, cost = v if isinstance(v, tuple) else (v, 0)
        parsed[k] = (Decimal(str(amt)), Decimal(str(cost)))
    return BotState(
        user_id=1, profile_id=1, username="bot",
        params=params, market_scope=None,
        cash=Decimal(cash),
        holdings=parsed,
        memory={}, rng=random.Random(seed),
    )


def make_trade(minutes_ago: float, outcome_id=11, price=0.5, post=None, side="buy", shares=10.0) -> TradeBrief:
    """构造一笔 NOW 之前 minutes_ago 分钟的成交。post=该笔成交后全市场价格快照
    [outcome11_price, outcome12_price]（window_change 依赖它）。"""
    return TradeBrief(
        ts=NOW - timedelta(minutes=minutes_ago),
        outcome_id=outcome_id, market_id=1, side=side,
        shares=shares, price=price, market_prices_post=post,
    )
