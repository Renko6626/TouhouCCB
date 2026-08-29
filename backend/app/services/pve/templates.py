"""PvE 人格模板：数据结构 + 一期模板实现 + 注册表。

**写新模板看 docs/pve.md**——一个 BotTemplate 子类（name + default_params + decide()）
写进本文件即自动注册，不用改任何其他地方。

decide() 是同步纯函数（输入 BotState + MarketView，输出 Action | None），
不碰 DB / 不发请求——所有 IO 由 engine 承担，模板可以直接用夹具单测
（夹具见 tests/pve_helpers.py）。

一期模板（spec §5.2）：liquidity（做市主力）/ grid（网格）/ hodler（定投死拿，兼冒烟测试）。
二期散户行为化模板（chaser/sheep/bottom_fisher/gambler/degen）后续加入。
"""
from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple


# ── 市场快照 ─────────────────────────────────────────────────────────────


@dataclass
class OutcomeView:
    outcome_id: int
    market_id: int
    label: str
    price: float  # LMSR 瞬时价


@dataclass
class MarketBrief:
    market_id: int
    outcome_ids: List[int]  # 升序 = Transaction.market_prices_post 的下标顺序


@dataclass
class TradeBrief:
    ts: datetime
    outcome_id: int
    market_id: int
    side: str  # "buy" | "sell"
    shares: float
    price: float
    market_prices_post: Optional[List[float]]


@dataclass
class MarketView:
    """每轮统一拉取、全体机器人共享的快照。trades 时间降序，窗口约 60min。"""

    now: datetime
    outcomes: Dict[int, OutcomeView]
    markets: Dict[int, MarketBrief]
    trades: List[TradeBrief]

    def _price_at_window_start(self, outcome_id: int, minutes: float) -> Optional[float]:
        ov = self.outcomes.get(outcome_id)
        if ov is None:
            return None
        mb = self.markets.get(ov.market_id)
        if mb is None:
            return None
        cutoff = self.now - timedelta(minutes=minutes)
        oldest: Optional[TradeBrief] = None
        for t in self.trades:  # 降序 → 循环里最后一个命中的即窗口内最老一笔
            if t.market_id == ov.market_id and t.ts >= cutoff:
                oldest = t
        if oldest is None or not oldest.market_prices_post:
            return None
        try:
            idx = mb.outcome_ids.index(outcome_id)
            return float(oldest.market_prices_post[idx])
        except (ValueError, IndexError):
            return None

    def window_change(self, outcome_id: int, minutes: float) -> float:
        """现价 − 窗口起点价（用同市场窗口内最老一笔的 market_prices_post 近似）。
        窗口内无成交 → 0（价格没动）。"""
        then = self._price_at_window_start(outcome_id, minutes)
        ov = self.outcomes.get(outcome_id)
        if then is None or ov is None:
            return 0.0
        return ov.price - then

    def max_abs_change(
        self, minutes: float, outcome_ids: Optional[List[int]] = None
    ) -> Tuple[float, Optional[int]]:
        """窗口内绝对涨跌幅最大的 outcome（供行情推送唤醒）。"""
        best, best_oid = 0.0, None
        for oid in (outcome_ids if outcome_ids is not None else self.outcomes):
            c = abs(self.window_change(oid, minutes))
            if c > best:
                best, best_oid = c, oid
        return best, best_oid

    def trade_count(self, outcome_id: int, minutes: float) -> int:
        cutoff = self.now - timedelta(minutes=minutes)
        return sum(1 for t in self.trades if t.outcome_id == outcome_id and t.ts >= cutoff)

    def net_flow(self, outcome_id: int, minutes: float) -> float:
        """窗口内净流入份额：Σbuy − Σsell。>0 = 人群在买（sheep 跟风的信号源）。"""
        cutoff = self.now - timedelta(minutes=minutes)
        net = 0.0
        for t in self.trades:
            if t.outcome_id == outcome_id and t.ts >= cutoff:
                net += t.shares if t.side == "buy" else -t.shares
        return net


# ── 机器人状态与动作 ─────────────────────────────────────────────────────


@dataclass
class BotState:
    """engine 每次唤醒前组装：资金/持仓来自 DB，memory 是模板私有的进程内记忆。"""

    user_id: int
    profile_id: int
    username: str
    params: dict
    market_scope: Optional[List[int]]
    cash: Decimal
    holdings: Dict[int, Tuple[Decimal, Decimal]]  # outcome_id -> (amount, cost_basis)
    memory: dict = field(default_factory=dict)
    rng: random.Random = field(default_factory=random.Random)

    def holding(self, outcome_id: int) -> Decimal:
        return self.holdings.get(outcome_id, (Decimal("0"), Decimal("0")))[0]

    def avg_cost(self, outcome_id: int) -> Optional[float]:
        """该仓位的均价成本（cost_basis / amount）；无仓位返回 None。
        chaser 类模板拿它和现价比来决定止盈/割肉。"""
        amount, cost = self.holdings.get(outcome_id, (Decimal("0"), Decimal("0")))
        if amount <= 0:
            return None
        return float(cost / amount)

    def eligible_outcomes(self, view: MarketView) -> List[OutcomeView]:
        return [
            ov
            for ov in view.outcomes.values()
            if self.market_scope is None or ov.market_id in self.market_scope
        ]


@dataclass
class Action:
    side: str  # "buy" | "sell"
    outcome_id: int
    shares: Decimal
    reason: str


def q_shares(x: float) -> Decimal:
    """份额量化到 2dp（远低于后端 6dp 上限，够人味也够干净）。Action.shares 用它构造。"""
    return Decimal(f"{x:.2f}")


def pick_home_outcome(
    bot: BotState, view: MarketView, prefer_active: bool = False
) -> Optional[OutcomeView]:
    """选定并记住"主场 outcome"。参数 outcome_id 优先；否则随机（或按活跃度）选一个，
    存 memory 复用；主场市场关闭/结算后自动重选。"""
    fixed = bot.params.get("outcome_id")
    if fixed is not None and fixed in view.outcomes:
        return view.outcomes[fixed]
    oid = bot.memory.get("home_outcome_id")
    if oid is not None and oid in view.outcomes:
        return view.outcomes[oid]
    candidates = bot.eligible_outcomes(view)
    if not candidates:
        return None
    if prefer_active:
        chosen = max(candidates, key=lambda ov: (view.trade_count(ov.outcome_id, 60), bot.rng.random()))
    else:
        chosen = bot.rng.choice(candidates)
    bot.memory["home_outcome_id"] = chosen.outcome_id
    return chosen


# ── 模板基类 ─────────────────────────────────────────────────────────────


TEMPLATE_REGISTRY: Dict[str, type["BotTemplate"]] = {}


class BotTemplate(ABC):
    """子类只要定义了 name 就自动进 TEMPLATE_REGISTRY——写完类即注册完毕。
    name 留空的子类视为抽象中间基类，不注册。"""

    name: str = ""
    default_params: dict = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.name:
            existing = TEMPLATE_REGISTRY.get(cls.name)
            if existing is not None and existing is not cls:
                raise ValueError(f"PvE 模板名重复：{cls.name!r}（{existing.__name__} vs {cls.__name__}）")
            TEMPLATE_REGISTRY[cls.name] = cls

    @abstractmethod
    def decide(self, bot: BotState, view: MarketView) -> Optional[Action]: ...


# ── 一期模板 ─────────────────────────────────────────────────────────────


class HodlerTemplate(BotTemplate):
    """定投死拿：认准一个 outcome 定期小额买入，几乎不卖。"""

    name = "hodler"
    default_params = {
        "outcome_id": None,        # 固定主场；None=随机选定后记住
        "buy_cny_min": 3.0,
        "buy_cny_max": 15.0,
        "skip_prob": 0.4,          # 看了盘但什么都不做的概率（人味）
        "max_price": 0.92,         # 价格太满不追
        "sell_prob": 0.0,          # 几乎不卖；>0 时偶发减半仓
        "cash_reserve_cny": 1.0,
        # 注意力（attention.py 消费；散户节奏）
        "check_interval_sec": 3600 * 4,
        "active_preset": "evening",
    }

    def decide(self, bot: BotState, view: MarketView) -> Optional[Action]:
        p, rng = bot.params, bot.rng
        home = pick_home_outcome(bot, view)
        if home is None:
            return None
        if rng.random() < p["skip_prob"]:
            return None
        held = bot.holding(home.outcome_id)
        if p["sell_prob"] > 0 and held > Decimal("1") and rng.random() < p["sell_prob"]:
            return Action("sell", home.outcome_id, q_shares(float(held) / 2), "hodler 偶发减仓")
        if home.price > p["max_price"]:
            return None
        budget = float(bot.cash) - p["cash_reserve_cny"]
        if budget <= 0.5:
            return None
        cny = min(rng.uniform(p["buy_cny_min"], p["buy_cny_max"]), budget)
        shares = cny / max(home.price, 0.01)
        if shares < 0.5:
            return None
        return Action("buy", home.outcome_id, q_shares(shares), f"hodler 定投 ≈{cny:.1f}")


class GridTemplate(BotTemplate):
    """网格：首次见价时围绕锚定价拉网，价格穿越网格线时向目标持仓靠拢。
    持仓每次从 DB 真实读取（bot.holdings），无内部账本漂移。"""

    name = "grid"
    default_params = {
        "outcome_id": None,
        "price_low": None,         # 显式区间；None=锚定价 ±band_pct
        "price_high": None,
        "band_pct": 0.3,
        "levels": 6,
        "shares_per_level": 8.0,
        "min_trade_shares": 2.0,
        "max_trade_shares": 30.0,
        "check_interval_sec": 180,
        "active_preset": "always",
    }

    def decide(self, bot: BotState, view: MarketView) -> Optional[Action]:
        p = bot.params
        home = pick_home_outcome(bot, view)
        if home is None:
            return None
        lines = bot.memory.get("grid_lines")
        if not lines:
            low, high = p["price_low"], p["price_high"]
            if low is None or high is None:
                low = max(0.02, home.price * (1 - p["band_pct"]))
                high = min(0.98, home.price * (1 + p["band_pct"]))
            n = max(2, int(p["levels"]))
            lines = [low + (high - low) * i / (n - 1) for i in range(n)]
            bot.memory["grid_lines"] = lines
        # 价格越低，压在价格上方的线越多 → 目标持仓越大（低吸高抛）
        target = p["shares_per_level"] * sum(1 for ln in lines if ln >= home.price)
        held = float(bot.holding(home.outcome_id))
        delta = target - held
        if abs(delta) < p["min_trade_shares"]:
            return None
        delta = max(-p["max_trade_shares"], min(p["max_trade_shares"], delta))
        if delta > 0:
            affordable = (float(bot.cash) * 0.95) / max(home.price, 0.01)
            delta = min(delta, affordable)
            if delta < p["min_trade_shares"]:
                return None
            return Action("buy", home.outcome_id, q_shares(delta), f"grid 补至目标 {target:.0f}")
        sell = min(-delta, held)
        if sell < p["min_trade_shares"]:
            return None
        return Action("sell", home.outcome_id, q_shares(sell), f"grid 降至目标 {target:.0f}")


class LiquidityTemplate(BotTemplate):
    """做市/库存平衡（thccb-quant volharvest 简化版）：先 bootstrap 建底仓，
    然后围绕底仓按"近期均价 − 现价"的 tanh 信号调整持仓——涨了卖一点、跌了买一点。
    造流动性目标的主力模板。"""

    name = "liquidity"
    default_params = {
        "outcome_id": None,
        "base_shares": 300.0,
        "max_offset_shares": 120.0,
        "scale_price": 0.05,        # tanh 尺度（价格空间）
        "lookback_min": 30,
        "min_trade_shares": 5.0,
        "max_trade_shares": 25.0,
        "bootstrap_step_shares": 15.0,
        "max_bootstrap_price": 0.85,  # 价格偏满时暂停 bootstrap
        "check_interval_sec": 120,
        "active_preset": "always",
    }

    def decide(self, bot: BotState, view: MarketView) -> Optional[Action]:
        p = bot.params
        home = pick_home_outcome(bot, view, prefer_active=True)
        if home is None:
            return None
        oid = home.outcome_id
        held = float(bot.holding(oid))
        floor = p["base_shares"] - p["max_offset_shares"]
        if held < floor:
            if home.price > p["max_bootstrap_price"]:
                return None
            step = min(p["bootstrap_step_shares"], floor - held)
            affordable = (float(bot.cash) * 0.95) / max(home.price, 0.01)
            step = min(step, affordable)
            if step < 1:
                return None
            return Action("buy", oid, q_shares(step), f"liquidity bootstrap 建仓（{held:.0f}→{floor:.0f}）")
        cutoff = view.now - timedelta(minutes=p["lookback_min"])
        prices = [t.price for t in view.trades if t.outcome_id == oid and t.ts >= cutoff]
        if len(prices) < 3:
            return None
        mean = sum(prices) / len(prices)
        offset = math.tanh((mean - home.price) / p["scale_price"]) * p["max_offset_shares"]
        target = p["base_shares"] + offset
        delta = target - held
        if abs(delta) < p["min_trade_shares"]:
            return None
        delta = max(-p["max_trade_shares"], min(p["max_trade_shares"], delta))
        if delta > 0:
            affordable = (float(bot.cash) * 0.95) / max(home.price, 0.01)
            delta = min(delta, affordable)
            if delta < p["min_trade_shares"]:
                return None
            return Action("buy", oid, q_shares(delta), f"liquidity 回补至 {target:.0f}")
        sell = min(-delta, held)
        return Action("sell", oid, q_shares(sell), f"liquidity 减至 {target:.0f}")


