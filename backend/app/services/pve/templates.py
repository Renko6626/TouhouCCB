"""PvE 人格模板：数据结构 + 一期模板实现 + 注册表。

**写新模板看 docs/pve.md**——一个 BotTemplate 子类（name + default_params + decide()）
写进本文件即自动注册，不用改任何其他地方。

decide() 是同步纯函数（输入 BotState + MarketView，输出 Action | None），
不碰 DB / 不发请求——所有 IO 由 engine 承担，模板可以直接用夹具单测
（夹具见 tests/pve_helpers.py）。

一期模板（spec §5.2）：liquidity（做市主力）/ grid（网格）/ hodler（定投死拿，兼冒烟测试）。
二期：believer 信念驱动散户家族（fan/swinger/chaser/sheep/bottom_fisher 是同一模型的
参数预设，见 BelieverTemplate docstring 与 docs/pve.md）。
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
    """每轮统一拉取、全体机器人共享的快照。trades 时间降序，窗口约 60min。
    sentiment：管理员风向注入（site_config `pve_sentiment`），outcome_id → 倾斜幅度
    （价格空间，可为负），believer 系模板把它加进长线 edge。"""

    now: datetime
    outcomes: Dict[int, OutcomeView]
    markets: Dict[int, MarketBrief]
    trades: List[TradeBrief]
    sentiment: Dict[int, float] = field(default_factory=dict)

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


# ── 二期：信念驱动散户（believer 家族）─────────────────────────────────


def _renorm(beliefs: Dict[int, float]) -> Dict[int, float]:
    """主观概率钳到 (0.02, 0.98) 后归一化——保持 LMSR 概率语义，防止极端化锁死。"""
    clamped = {oid: min(0.98, max(0.02, b)) for oid, b in beliefs.items()}
    total = sum(clamped.values())
    return {oid: b / total for oid, b in clamped.items()}


class BelieverTemplate(BotTemplate):
    """信念驱动散户：内心维护一份主观概率（对每个 outcome「我觉得它会赢」），
    交易动机 = 长线信念 edge 与短线动量 edge 的加权混合（w_swing 是刻度：
    0=信仰党拿到结算，1=波段客只吃短线）。追涨/跟风/抄底/铁杆粉全是本模型的
    参数点位（见下方薄子类预设），不是独立脚本——没有可被玩家试探的固定行为指纹。
    """

    name = "believer"
    default_params = {
        # 信念层
        "conviction": 0.12,       # 生成信念时本命 outcome 的上倾幅度（概率空间）
        "herd_coef": 0.15,        # 每次看盘信念被市场带偏的比例；负=逆势党
        "herd_signal": "price",   # price=跟着价格信（图表党）/ flow=跟着人群信（从众党）
        "flow_scale": 60.0,       # flow 模式：净流入多少份算「人群明显在买」（tanh 尺度）
        "flow_step": 0.08,        # flow 模式：单次看盘信念最大被带偏量（价格空间）
        "shock_prob": 0.06,       # 观点冲击概率（模拟看到消息/风向变了）
        "shock_scale": 0.08,      # 冲击幅度（价格空间）
        "sentiment_gain": 1.0,    # 对管理员风向注入（view.sentiment）的易感度
        # 短线层
        "w_swing": 0.35,          # 短线动机权重 0~1
        "trend_coef": 0.6,        # 动量外推系数；正=觉得涨了还涨，负=觉得要回调
        "lookback_min": 30,
        "take_profit": 0.15,      # 浮盈比例止盈（按 w_swing 概率执行——波段客勤快）
        "stop_loss": 0.25,        # 浮亏割肉线（信念也不再支持时才割）
        # 执行
        "act_threshold": 0.04,    # |edge| 行动阈值（每次带 ±30% 随机抖动）
        "aggressiveness": 0.12,   # 单次下注 ≈ 现金 × 本系数 ×（edge 强度）
        "yolo_prob": 0.04,        # 上头概率：下注 ×3
        "max_bet_cny": 40.0,
        "skip_prob": 0.3,
        "cash_reserve_cny": 1.0,
        # 注意力
        "check_interval_sec": 3600 * 2,
        "active_preset": "evening",
    }

    def decide(self, bot: BotState, view: MarketView) -> Optional[Action]:
        p, rng = bot.params, bot.rng
        if rng.random() < p["skip_prob"]:
            return None
        home = pick_home_outcome(bot, view)
        if home is None:
            return None
        oids = [
            oid for oid in view.markets[home.market_id].outcome_ids if oid in view.outcomes
        ]
        beliefs = bot.memory.get("beliefs")
        if not beliefs or set(beliefs) != set(oids):  # 首次/主场市场变更 → 重建信念
            beliefs = {oid: view.outcomes[oid].price for oid in oids}
            beliefs[home.outcome_id] += p["conviction"]
            beliefs = _renorm(beliefs)
        else:
            # 从众项：每次看盘信念被市场带偏（负 herd_coef=逆势党越看越反着信）。
            # price 模式跟价格信（图表党）；flow 模式跟人群净流入信（从众党）
            for oid in oids:
                if p["herd_signal"] == "flow":
                    sig = p["flow_step"] * math.tanh(
                        view.net_flow(oid, p["lookback_min"]) / max(p["flow_scale"], 1e-6)
                    )
                else:
                    sig = view.outcomes[oid].price - beliefs[oid]
                beliefs[oid] += p["herd_coef"] * sig
            # 观点冲击：随机重估某个 outcome（模拟看到消息/风向变了）
            if rng.random() < p["shock_prob"]:
                beliefs[rng.choice(oids)] += rng.uniform(-1, 1) * p["shock_scale"]
            beliefs = _renorm(beliefs)
        bot.memory["beliefs"] = beliefs

        # score = 长线信念 edge 与短线动量 edge 按 w_swing 加权（生成扰动可能越界，钳回）
        w = min(1.0, max(0.0, p["w_swing"]))
        scores = {}
        for oid in oids:
            edge_long = (
                beliefs[oid]
                + p["sentiment_gain"] * view.sentiment.get(oid, 0.0)
                - view.outcomes[oid].price
            )
            edge_short = p["trend_coef"] * view.window_change(oid, p["lookback_min"])
            scores[oid] = (1 - w) * edge_long + w * edge_short

        # 短线退出：浮盈落袋 / 浮亏割肉，都按 w 概率执行——波段客勤快、信仰党拿着不动
        for oid in oids:
            held, cost = float(bot.holding(oid)), bot.avg_cost(oid)
            if held < 1 or not cost or cost < 1e-6:
                continue
            pnl = (view.outcomes[oid].price - cost) / cost
            if pnl >= p["take_profit"] and rng.random() < w:
                frac = rng.uniform(0.5, 1.0)
                return Action("sell", oid, q_shares(held * frac), f"止盈落袋（浮盈 {pnl:+.0%}）")
            if pnl <= -p["stop_loss"] and scores[oid] <= 0 and rng.random() < w:
                return Action("sell", oid, q_shares(held), f"扛不住割肉（浮亏 {pnl:+.0%}）")

        # 按 |edge| 从大到小找第一个可执行的动作
        budget = float(bot.cash) - p["cash_reserve_cny"]
        for oid in sorted(scores, key=lambda o: abs(scores[o]), reverse=True):
            score = scores[oid]
            if abs(score) < p["act_threshold"] * rng.uniform(0.7, 1.3):
                return None  # 最大的都不够阈值，后面更小
            ov = view.outcomes[oid]
            if score > 0:
                if budget <= 0.5:
                    continue
                heat = min(abs(score) / 0.10, 1.5)
                cny = p["aggressiveness"] * float(bot.cash) * heat * rng.uniform(0.6, 1.4)
                if rng.random() < p["yolo_prob"]:
                    cny *= 3  # 上头
                cny = min(cny, p["max_bet_cny"], budget)
                shares = cny / max(ov.price, 0.01)
                if shares < 0.5:
                    continue
                return Action("buy", oid, q_shares(shares), f"看好 {ov.label}（edge {score:+.2f}）")
            held = float(bot.holding(oid))
            if held < 1:
                continue
            frac = min(abs(score) / 0.10, 1.0) * rng.uniform(0.4, 1.0)
            shares = min(held, max(held * frac, 1.0))
            return Action("sell", oid, q_shares(shares), f"觉得 {ov.label} 高估（edge {score:+.2f}）减仓")
        return None


# ── believer 人格预设：同一模型的参数点位（管理页下拉里逐个可选）──────────


class FanTemplate(BelieverTemplate):
    """铁杆粉：本命信念又高又硬，跌了反而补仓，几乎不止盈——拿到结算。"""

    name = "fan"
    default_params = {
        **BelieverTemplate.default_params,
        "conviction": 0.3, "herd_coef": 0.03, "w_swing": 0.1,
        "take_profit": 0.6, "stop_loss": 0.7, "shock_prob": 0.02,
        "check_interval_sec": 3600 * 4,
    }


class SwingerTemplate(BelieverTemplate):
    """波段客：没什么立场，哪里有波动去哪里，止盈勤快、割肉果断。"""

    name = "swinger"
    default_params = {
        **BelieverTemplate.default_params,
        "conviction": 0.04, "herd_coef": 0.1, "w_swing": 0.8, "trend_coef": 0.7,
        "take_profit": 0.1, "stop_loss": 0.15, "check_interval_sec": 1200,
    }


class ChaserTemplate(BelieverTemplate):
    """追涨杀跌：看图表信动量，涨了觉得还会涨；被套后割肉也快。"""

    name = "chaser"
    default_params = {
        **BelieverTemplate.default_params,
        "conviction": 0.05, "herd_coef": 0.3, "w_swing": 0.7, "trend_coef": 1.2,
        "take_profit": 0.2, "stop_loss": 0.12, "check_interval_sec": 1800,
    }


class SheepTemplate(BelieverTemplate):
    """跟风羊：不看价格看人群（net_flow），大家买它才信，总慢半拍。"""

    name = "sheep"
    default_params = {
        **BelieverTemplate.default_params,
        "herd_signal": "flow", "herd_coef": 0.5, "flow_step": 0.1,
        "conviction": 0.08, "w_swing": 0.4,
    }


class BottomFisherTemplate(BelieverTemplate):
    """抄底侠：大跌进场接飞刀等反弹，赚一点就跑。"""

    name = "bottom_fisher"
    default_params = {
        **BelieverTemplate.default_params,
        "conviction": 0.05, "herd_coef": 0.05, "w_swing": 0.75, "trend_coef": -1.0,
        "take_profit": 0.1,
    }


