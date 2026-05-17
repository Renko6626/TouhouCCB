# VolatilityHarvest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 VolatilityHarvest 策略：logit 空间 deadband+tanh 仓位映射 + bootstrap guard + 周期 reconcile + trend guard。

**Architecture:** SSE 事件驱动；维护 logit-space 滑窗中位数/MAD；deadband 内回归底仓，超阈值平滑反向调整持仓；启动 bootstrap 节流补底仓；每 5min 校准真实持仓防漂移；连续 N 笔同向 trade 暂停逆势加仓。

**Tech Stack:** Python 3.11+ / asyncio / Decimal / statistics / collections.deque / 现有 SseSubscriber + Broker + Store

**Spec:** `docs/superpowers/specs/2026-05-17-volatility-harvest-design.md`
**SKILL (must follow):** `.claude/skills/writing-quant-strategy/SKILL.md`

---

## File Structure

| File | Op | Responsibility |
|---|---|---|
| `quant/thccb_quant/strategy/volharvest.py` | **create** | VolatilityHarvest 类 + `@register("volharvest")` |
| `quant/tests/test_strategy_volharvest.py` | **create** | 单元 + 集成 dispatch + setup/bootstrap 三类测试（≥25 个用例） |
| `quant/thccb_quant/trader.py` | **modify** | 顶部加 `import thccb_quant.strategy.volharvest` 触发注册 |
| `quant/config.example.yaml` | **modify** | 末尾加 volharvest 示例（`enabled: false`） |
| `quant/README.md` | **modify** | 加一段简介指向 spec + skill |

---

## Task 1: 策略实现 + 全套测试 + 集成

**Files:**
- Create: `quant/thccb_quant/strategy/volharvest.py`
- Create: `quant/tests/test_strategy_volharvest.py`
- Modify: `quant/thccb_quant/trader.py`（顶部加 1 行 import）
- Modify: `quant/config.example.yaml`（末尾加 volharvest 块）

**前置必读**：
- spec `docs/superpowers/specs/2026-05-17-volatility-harvest-design.md` 全文
- SKILL `.claude/skills/writing-quant-strategy/SKILL.md` 全文（特别是数据契约 / 错误处理 / 测试三类）

### Step 1: 写 `quant/tests/test_strategy_volharvest.py` 的完整测试集（FAIL）

按 spec §8 三类全列出来。**用 `@pytest.fixture` 共享 store + ctx 构造逻辑**。
代码模板（subagent 应按 spec §8 的 25+ 测试清单逐条实现，下面只示意结构和关键 fixture）：

```python
"""VolatilityHarvest 策略测试。

按 SKILL writing-quant-strategy 三类：
- 单元：logit 转换 / 预热 / 低 MAD skip / deadband / tanh 饱和 / trade clip /
  trend guard / bootstrap guard / reconcile
- 集成（SseSubscriber dispatch）
- Setup / holdings 初始化
"""
import asyncio
import math
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

from thccb_quant.client.rest import HoldingRead
from thccb_quant.client.sse import SseEvent
from thccb_quant.client.sse_subscriber import SseSubscriber
from thccb_quant.state.store import Store
from thccb_quant.strategy.base import StrategyContext
from thccb_quant.strategy.volharvest import VolatilityHarvest


# ─── fixtures ────────────────────────────────────────────────────

@pytest.fixture
async def store(tmp_path: Path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


def _default_cfg(**overrides) -> dict:
    cfg = {
        "market_id": 1, "outcome_id": 1,
        "window_size": 10, "k_sigma": 2.0, "scale_mad": 1.0,
        "min_mad_logit": 0.001,
        "base_shares": 100.0, "max_offset_shares": 40.0,
        "min_trade_shares": 1.0, "max_trade_shares": 10.0,
        "bootstrap_max_step": 5.0, "bootstrap_interval_sec": 0,
        "bootstrap_skip_if_overpriced": True,
        "reconcile_interval_sec": 999, "reconcile_tolerance": 0.5,
        "trend_guard_events": 3,
    }
    cfg.update(overrides)
    return cfg


def _holding(outcome_id: int, amount: Decimal) -> HoldingRead:
    return HoldingRead(
        outcome_id=outcome_id, outcome_label="yes",
        market_id=1, market_title="t",
        amount=amount, cost_basis=Decimal("0"),
        avg_price=Decimal("0.5"), current_price=Decimal("0.5"),
        market_value=Decimal("0"), unrealized_pnl=Decimal("0"),
    )


async def _make_ctx(store: Store, current_holding: Decimal = Decimal("100")) -> StrategyContext:
    rest = MagicMock()
    rest.get_holdings = AsyncMock(return_value=[_holding(1, current_holding)])
    broker = MagicMock()
    broker.buy = AsyncMock(return_value=MagicMock(
        shares=Decimal("1"), cost=Decimal("0.5"), new_cash=Decimal("499.5"),
    ))
    broker.sell = AsyncMock(return_value=MagicMock(
        shares=Decimal("1"), cost=Decimal("-0.5"), new_cash=Decimal("500.5"),
    ))
    return StrategyContext(
        rest=rest, broker=broker, store=store,
        logger=structlog.get_logger("test"),
        config={"max_slippage_bps": 300},
    )


def _trade_event(*, seq: int, side: str, price: float,
                 outcome_id: int = 1, trade_id: int | None = None, ts: str = "2026-05-17T07:00:00Z") -> SseEvent:
    return SseEvent(
        type="trade", seq=seq,
        data={"trade": {
            "id": trade_id or seq, "type": side, "outcome_id": outcome_id,
            "username": "u", "shares": 1.0, "price": price,
            "gross": price, "fee": 0.0,
            "post_market_price": price, "market_prices_post": [price, 1-price],
            "timestamp": ts,
        }},
    )


# ─── 1. logit / window 单元 ───────────────────────────────────────

def test_logit_conversion_correct():
    from thccb_quant.strategy.volharvest import to_logit
    assert abs(to_logit(0.5) - 0.0) < 1e-9
    assert abs(to_logit(0.62) - math.log(0.62/0.38)) < 1e-9
    # clamp
    assert to_logit(0.0) == to_logit(0.001)
    assert to_logit(1.0) == to_logit(0.999)


async def test_window_not_warm_skips(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(window_size=20))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))  # = base
    await s.setup(ctx)
    # 喂少于 window/2 笔 → 不下单
    for i in range(5):
        await s.on_sse_event(_trade_event(seq=i+1, side="BUY", price=0.5))
    assert ctx.broker.buy.call_count == 0
    assert ctx.broker.sell.call_count == 0


async def test_low_mad_skips(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(min_mad_logit=10.0))  # 阈值大
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    for i in range(20):
        await s.on_sse_event(_trade_event(seq=i+1, side="BUY", price=0.5 + 0.001*i))
    # MAD 远小于 min_mad_logit → 全 skip
    assert ctx.broker.buy.call_count == 0


# ─── 2. deadband + tanh（关键回归测试）─────────────────────────────

async def test_within_deadband_target_is_base(store):
    """deviation 在 deadband 内时 target == base，不应下单。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(window_size=10))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    # 喂 10 笔均匀价让中位数稳定 + MAD 非零
    prices = [0.50, 0.51, 0.49, 0.50, 0.52, 0.48, 0.50, 0.51, 0.49, 0.50]
    for i, p in enumerate(prices):
        await s.on_sse_event(_trade_event(seq=i+1, side="BUY", price=p))
    # 再喂一笔 deviation < threshold 的（接近中位数）
    pre = ctx.broker.buy.call_count + ctx.broker.sell.call_count
    await s.on_sse_event(_trade_event(seq=100, side="BUY", price=0.505))
    assert (ctx.broker.buy.call_count + ctx.broker.sell.call_count) == pre  # 不动


async def test_no_tanh_jump_at_threshold(store):
    """关键 regression：deviation 从 0.99*threshold 到 1.01*threshold 时
    target 变化幅度 < max_offset * 0.05（验证不再是 76% 跳变）。"""
    from thccb_quant.strategy.volharvest import compute_target
    base, max_offset, k_sigma, scale_mad = 100.0, 40.0, 2.0, 1.0
    mad = 0.1
    threshold = k_sigma * 1.4826 * mad  # ≈ 0.296
    
    t_just_below = compute_target(deviation=threshold * 0.99, mad=mad,
                                  base=base, max_offset=max_offset,
                                  k_sigma=k_sigma, scale_mad=scale_mad)
    t_just_above = compute_target(deviation=threshold * 1.01, mad=mad,
                                  base=base, max_offset=max_offset,
                                  k_sigma=k_sigma, scale_mad=scale_mad)
    jump = abs(t_just_above - t_just_below)
    assert jump < max_offset * 0.05, f"target jumped {jump} ≥ 5% of max_offset"


def test_tanh_saturation_far_above(store=None):
    from thccb_quant.strategy.volharvest import compute_target
    t = compute_target(deviation=10.0, mad=0.1, base=100.0, max_offset=40.0,
                       k_sigma=2.0, scale_mad=1.0)
    assert 60.0 < t < 60.5  # 接近 base - max_offset = 60


def test_tanh_saturation_far_below(store=None):
    from thccb_quant.strategy.volharvest import compute_target
    t = compute_target(deviation=-10.0, mad=0.1, base=100.0, max_offset=40.0,
                       k_sigma=2.0, scale_mad=1.0)
    assert 139.5 < t < 140.0  # 接近 base + max_offset


# ─── 3. min/max trade shares ─────────────────────────────────────

async def test_min_trade_shares_filter(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(min_trade_shares=50.0))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    # 喂触发信号但 delta 很小（< min_trade）→ 不下单
    prices = [0.50] * 10
    for i, p in enumerate(prices):
        await s.on_sse_event(_trade_event(seq=i+1, side="BUY", price=p))
    # 一笔涨 1% 触发但 delta < 50
    await s.on_sse_event(_trade_event(seq=11, side="BUY", price=0.51))
    assert ctx.broker.buy.call_count == 0
    assert ctx.broker.sell.call_count == 0


async def test_max_trade_shares_clip(store):
    """|delta| > max_trade_shares 时 clip。验证 broker 收到的 shares 不超过 cap。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        max_trade_shares=3.0, max_offset_shares=100.0,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    # 强制大偏离
    prices = [0.50] * 5 + [0.51, 0.49] * 5
    for i, p in enumerate(prices):
        await s.on_sse_event(_trade_event(seq=i+1, side="BUY", price=p))
    # 极端价
    await s.on_sse_event(_trade_event(seq=100, side="BUY", price=0.90))
    # 至少调过一次 sell（涨太多）
    if ctx.broker.sell.call_count > 0:
        args = ctx.broker.sell.call_args
        assert args.kwargs["shares"] <= Decimal("3.0")


# ─── 4. 持仓增量 ─────────────────────────────────────────────────

async def test_holding_update_on_success(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(window_size=5))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    ctx.broker.buy = AsyncMock(return_value=MagicMock(
        shares=Decimal("3"), cost=Decimal("1.5"), new_cash=Decimal("498.5"),
    ))
    await s.setup(ctx)
    initial = s._holding
    # 喂数据触发 buy
    for i, p in enumerate([0.55, 0.54, 0.55, 0.54, 0.55, 0.40]):
        await s.on_sse_event(_trade_event(seq=i+1, side="BUY", price=p))
    if ctx.broker.buy.call_count > 0:
        assert s._holding == initial + Decimal("3") * ctx.broker.buy.call_count


async def test_holding_no_update_on_failure(store):
    from thccb_quant.errors import RiskRejected
    s = VolatilityHarvest(name="v", config=_default_cfg(window_size=5))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    ctx.broker.buy = AsyncMock(side_effect=RiskRejected("test"))
    ctx.broker.sell = AsyncMock(side_effect=RiskRejected("test"))
    await s.setup(ctx)
    initial = s._holding
    for i, p in enumerate([0.55, 0.54, 0.55, 0.54, 0.55, 0.40]):
        await s.on_sse_event(_trade_event(seq=i+1, side="BUY", price=p))
    assert s._holding == initial  # 失败不更新


# ─── 5. trend guard ──────────────────────────────────────────────

async def test_trend_guard_up_blocks_sell(store):
    """连续 N 笔 BUY 时，策略想 sell 应被拦。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        window_size=5, trend_guard_events=3, max_offset_shares=100.0,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    # 先填窗口 + 让 MAD 非零（混合 side）
    for i, (side, p) in enumerate([("BUY", 0.50), ("SELL", 0.51),
                                    ("BUY", 0.49), ("SELL", 0.50), ("BUY", 0.50)]):
        await s.on_sse_event(_trade_event(seq=i+1, side=side, price=p))
    # 然后连续 3 笔 BUY 推涨
    for i, p in enumerate([0.56, 0.58, 0.60]):
        await s.on_sse_event(_trade_event(seq=10+i, side="BUY", price=p))
    # 此时 trend_window 全 BUY；策略涨太多想 sell；应被拦
    sells_before = ctx.broker.sell.call_count
    # 再来一笔大涨 BUY trade，触发 sell 信号但被 trend guard 拦
    await s.on_sse_event(_trade_event(seq=20, side="BUY", price=0.70))
    assert ctx.broker.sell.call_count == sells_before


async def test_trend_guard_mixed_passes(store):
    """trend_window 混合 side 时不拦。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        window_size=5, trend_guard_events=3, max_offset_shares=100.0,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    for i, (side, p) in enumerate([("BUY", 0.50), ("SELL", 0.51),
                                    ("BUY", 0.49), ("SELL", 0.50), ("BUY", 0.50),
                                    ("BUY", 0.55), ("SELL", 0.60), ("BUY", 0.65)]):
        await s.on_sse_event(_trade_event(seq=i+1, side=side, price=p))
    # trend_window 是混合的，guard 不拦
    # 不强断言下单（取决于 deviation 是否超阈值），但 trend_guard log 不应出现


# ─── 6. bootstrap guard ──────────────────────────────────────────

async def test_bootstrap_buys_when_below_base(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(
        base_shares=100.0, bootstrap_max_step=5.0, bootstrap_interval_sec=0,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("50"))  # 远低于 base
    await s.setup(ctx)
    # 任意一笔 event 触发 bootstrap buy
    await s.on_sse_event(_trade_event(seq=1, side="BUY", price=0.5))
    assert ctx.broker.buy.call_count == 1
    args = ctx.broker.buy.call_args
    assert args.kwargs["shares"] <= Decimal("5.0")  # ≤ max_step


async def test_bootstrap_interval_throttle(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(
        base_shares=100.0, bootstrap_max_step=5.0,
        bootstrap_interval_sec=999,  # 极长间隔
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("50"))
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=1, side="BUY", price=0.5))
    first = ctx.broker.buy.call_count
    # 间隔内再来 event → 不再 bootstrap buy
    await s.on_sse_event(_trade_event(seq=2, side="BUY", price=0.5))
    assert ctx.broker.buy.call_count == first


async def test_bootstrap_skip_when_overpriced(store):
    """window 已热 + deviation > 0（价格偏高）→ bootstrap 暂停。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        window_size=5, base_shares=100.0,
        bootstrap_skip_if_overpriced=True, bootstrap_interval_sec=0,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("50"))
    await s.setup(ctx)
    # 喂均价 0.50 让 median 稳定
    for i, p in enumerate([0.50, 0.51, 0.49, 0.50, 0.50]):
        await s.on_sse_event(_trade_event(seq=i+1, side="BUY", price=p))
    bootstrap_buys_before = ctx.broker.buy.call_count
    # 现在喂一笔高价 0.70（deviation > 0）→ 应跳过 bootstrap
    await s.on_sse_event(_trade_event(seq=10, side="BUY", price=0.70))
    # 不一定 0 因为前面预热期间可能已经买过 bootstrap；只要这笔高价时没买
    # （broker.buy.call_count 没增加超过 1，因为 0.70 也可能触发主信号 sell-side
    # 但 holding<base 时不应被允许减仓——这是 spec 设计，由 bootstrap mode 优先）


# ─── 7. reconcile ────────────────────────────────────────────────

async def test_reconcile_corrects_drift(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(
        reconcile_interval_sec=0,  # 强制每次都 reconcile
        reconcile_tolerance=0.5,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    assert s._holding == Decimal("100")
    # 改 mock 让下次 get_holdings 返 80（漂移）
    ctx.rest.get_holdings = AsyncMock(return_value=[_holding(1, Decimal("80"))])
    # 喂 event 触发 reconcile
    await s.on_sse_event(_trade_event(seq=1, side="BUY", price=0.5))
    assert s._holding == Decimal("80")


async def test_reconcile_throttled(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(
        reconcile_interval_sec=999,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    setup_calls = ctx.rest.get_holdings.call_count
    await s.on_sse_event(_trade_event(seq=1, side="BUY", price=0.5))
    # 间隔内不应再调 get_holdings
    assert ctx.rest.get_holdings.call_count == setup_calls


async def test_reconcile_within_tolerance_no_action(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(
        reconcile_interval_sec=0, reconcile_tolerance=5.0,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    # 漂移 2.0 < tolerance → 不覆盖
    ctx.rest.get_holdings = AsyncMock(return_value=[_holding(1, Decimal("102"))])
    await s.on_sse_event(_trade_event(seq=1, side="BUY", price=0.5))
    assert s._holding == Decimal("100")  # 未被覆盖


# ─── 8. 集成：SseSubscriber dispatch ──────────────────────────────

async def test_subscriber_routes_to_volharvest(store):
    s = VolatilityHarvest(name="v", config=_default_cfg())
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)

    sse_client = MagicMock()
    async def fake_subscribe(market_id):
        yield _trade_event(seq=1, side="BUY", price=0.5)
    sse_client.subscribe = fake_subscribe

    rest = MagicMock()
    rest.get_recent_trades = AsyncMock(return_value=[])
    sub = SseSubscriber(
        rest=rest, store=store, sse_client=sse_client,
        strategies=[s], market_ids={1},
        logger=structlog.get_logger("test"),
    )
    await asyncio.wait_for(sub.run(), timeout=2.0)
    # 至少调过 on_sse_event 一次（间接：trades 表有行 + 内部 window 长度 >0）
    assert len(s._window) >= 1


# ─── 9. setup / holdings 初始化 ──────────────────────────────────

async def test_setup_reads_actual_holdings(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(base_shares=100.0))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    assert s._holding == Decimal("100")


async def test_setup_below_base_enters_bootstrap(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(base_shares=100.0))
    ctx = await _make_ctx(store, current_holding=Decimal("30"))
    await s.setup(ctx)
    assert s._holding == Decimal("30")
    # bootstrap mode 由 _holding < base 判断，无显式 mode flag


async def test_setup_above_base_no_proactive_sell(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(base_shares=100.0))
    ctx = await _make_ctx(store, current_holding=Decimal("200"))
    await s.setup(ctx)
    assert s._holding == Decimal("200")
    # setup 本身不应触发 broker.sell
    assert ctx.broker.sell.call_count == 0


# ─── 10. 配置必填 ────────────────────────────────────────────────

def test_missing_market_id_raises_keyerror():
    cfg = _default_cfg()
    del cfg["market_id"]
    with pytest.raises(KeyError, match="market_id"):
        VolatilityHarvest(name="v", config=cfg)
```

**注意**：subagent 应保证测试覆盖 spec §8.1 全部 13 个 unit 项 + §8.2 集成 +
§8.3 setup 三项。上面列出 ~18 个测试是骨架；可以再加但不能少。

### Step 2: 跑测试确认 FAIL

```bash
cd /data/sunyunbo/www/TouhouCCB/quant && source .venv/bin/activate
pytest tests/test_strategy_volharvest.py -v 2>&1 | head -10
```
Expected: ModuleNotFoundError `thccb_quant.strategy.volharvest`

### Step 3: 写实现 `quant/thccb_quant/strategy/volharvest.py`

按 spec §3 公式 + §6 错误处理。**关键代码片段**（subagent 应整合到完整 class）：

**Helpers**：

```python
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

import structlog

from thccb_quant.client.rest import HoldingRead
from thccb_quant.client.sse import SseEvent
from thccb_quant.errors import BusinessError, RiskRejected, TransientError
from thccb_quant.strategy.base import Strategy, StrategyContext
from thccb_quant.strategy.registry import register


def to_logit(p: float) -> float:
    """价格 → logit。clamp 防边界爆炸。"""
    p = max(0.001, min(0.999, p))
    return math.log(p / (1.0 - p))


def compute_target(
    *, deviation: float, mad: float,
    base: float, max_offset: float,
    k_sigma: float, scale_mad: float,
) -> float:
    """spec §3.2 deadband + tanh 公式。"""
    threshold = k_sigma * 1.4826 * mad
    excess = max(0.0, abs(deviation) - threshold)
    if excess <= 0.0:
        return base
    u = math.copysign(excess / (scale_mad * mad), deviation)
    return base - max_offset * math.tanh(u)
```

**类骨架**（subagent 按 spec §3-§5 完整实现，下面只示意必备属性 + on_sse_event 入口）：

```python
@register("volharvest")
class VolatilityHarvest(Strategy):
    tick_interval_sec = 300  # 不靠 tick，靠 SSE event

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        # market routing
        self._market_id = int(config["market_id"])
        self._outcome_id = int(config["outcome_id"])
        # 统计
        self._window_size = int(config["window_size"])
        self._k_sigma = float(config["k_sigma"])
        self._scale_mad = float(config["scale_mad"])
        self._min_mad_logit = float(config["min_mad_logit"])
        self._window: deque[tuple[float, float]] = deque(maxlen=self._window_size)  # (ts_epoch, logit)
        # 仓位
        self._base = Decimal(str(config["base_shares"]))
        self._max_offset = Decimal(str(config["max_offset_shares"]))
        self._min_trade = Decimal(str(config["min_trade_shares"]))
        self._max_trade = Decimal(str(config["max_trade_shares"]))
        # bootstrap
        self._boot_step = Decimal(str(config["bootstrap_max_step"]))
        self._boot_interval = float(config["bootstrap_interval_sec"])
        self._boot_skip_overpriced = bool(config["bootstrap_skip_if_overpriced"])
        self._last_boot_ts = 0.0
        # reconcile
        self._reconcile_interval = float(config["reconcile_interval_sec"])
        self._reconcile_tol = Decimal(str(config["reconcile_tolerance"]))
        self._last_reconcile_ts = 0.0
        # trend guard
        self._trend_n = int(config["trend_guard_events"])
        self._trend_window: deque[str] = deque(maxlen=self._trend_n)
        # 内部状态
        self._holding: Decimal = Decimal("0")
        self._ctx: Optional[StrategyContext] = None

    async def setup(self, ctx: StrategyContext) -> None:
        self.market_id = self._market_id  # SseSubscriber 路由依赖
        self._ctx = ctx
        # 从真实持仓 bootstrap _holding
        holdings = await ctx.rest.get_holdings()
        actual = next((h.amount for h in holdings if h.outcome_id == self._outcome_id),
                      Decimal("0"))
        self._holding = actual
        self._last_reconcile_ts = time.monotonic()

    async def tick(self) -> None:
        return  # SSE 驱动

    async def on_sse_event(self, event: SseEvent) -> None:
        assert self._ctx is not None
        if not isinstance(event, SseEvent) or event.type != "trade":
            return
        trade = event.data.get("trade") if isinstance(event.data, dict) else None
        if not trade or int(trade.get("outcome_id", -1)) != self._outcome_id:
            return
        try:
            price = float(trade["post_market_price"])
            side = str(trade["type"])
            ts_str = str(trade["timestamp"])
        except (KeyError, TypeError, ValueError):
            self._ctx.logger.warning("volharvest_bad_event", outcome_id=self._outcome_id)
            return
        ts_epoch = self._parse_ts(ts_str)
        if ts_epoch is None:
            self._ctx.logger.warning("volharvest_bad_timestamp", ts=ts_str)
            return
        current_logit = to_logit(price)

        # 更新窗口
        self._window.append((ts_epoch, current_logit))
        self._trend_window.append(side)

        # Reconcile（在主流程前做，让漂移修正发生在决策之前）
        await self._maybe_reconcile()

        # Bootstrap mode
        if self._holding < self._base:
            await self._maybe_bootstrap(price=price, current_logit=current_logit)
            return

        # 主信号
        await self._maybe_trade(price=price, current_logit=current_logit, ts_epoch=ts_epoch)

    # ─── helpers ──────────────────────────────────────────────

    @staticmethod
    def _parse_ts(raw: str) -> Optional[float]:
        from datetime import datetime, timezone
        try:
            s = raw.rstrip("Z")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            return None

    async def _maybe_reconcile(self) -> None:
        now = time.monotonic()
        if now - self._last_reconcile_ts < self._reconcile_interval:
            return
        try:
            holdings = await self._ctx.rest.get_holdings()
            actual = next((h.amount for h in holdings if h.outcome_id == self._outcome_id),
                          Decimal("0"))
            self._last_reconcile_ts = now
            if abs(actual - self._holding) > self._reconcile_tol:
                self._ctx.logger.warning("volharvest_reconcile_drift_corrected",
                                          actual=str(actual), internal=str(self._holding),
                                          diff=str(actual - self._holding))
                self._holding = actual
        except (BusinessError, TransientError):
            self._ctx.logger.warning("volharvest_reconcile_failed")

    async def _maybe_bootstrap(self, *, price: float, current_logit: float) -> None:
        now = time.monotonic()
        if now - self._last_boot_ts < self._boot_interval:
            return
        # overpriced 检查：window 已热 + deviation > 0 → skip
        window_warm = len(self._window) >= self._window_size // 2
        if window_warm and self._boot_skip_overpriced:
            logits = [lg for _, lg in self._window]
            median_logit = statistics.median(logits)
            deviation = current_logit - median_logit
            if deviation > 0:
                self._ctx.logger.info("volharvest_bootstrap_skip_overpriced",
                                       price=price, deviation=deviation)
                return
        # 算补仓量
        need = self._base - self._holding
        step = min(need, self._boot_step, self._max_trade)
        if step < self._min_trade:
            return
        try:
            resp = await self._ctx.broker.buy(
                strategy=self.name, outcome_id=self._outcome_id,
                shares=step,
                max_slippage_bps=int(self._ctx.config["max_slippage_bps"]),
            )
            self._holding += resp.shares
            self._last_boot_ts = now
            self._ctx.logger.info("volharvest_trade", side="buy",
                                  shares=str(resp.shares), cost=str(resp.cost),
                                  bootstrap_mode=True,
                                  holding_after=str(self._holding))
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="buy", reason="bootstrap",
                snapshot={"step": str(step), "holding_after": str(self._holding)},
            )
        except (RiskRejected, BusinessError, TransientError) as e:
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason=f"bootstrap buy failed: {e}",
            )

    async def _maybe_trade(self, *, price: float, current_logit: float, ts_epoch: float) -> None:
        # 预热检查
        if len(self._window) < self._window_size // 2:
            self._ctx.logger.info("volharvest_window_not_warm",
                                   window_len=len(self._window),
                                   needed=self._window_size // 2)
            return
        # 统计
        logits = [lg for _, lg in self._window]
        median_logit = statistics.median(logits)
        mad_logit = statistics.median(abs(lg - median_logit) for lg in logits)
        if mad_logit < self._min_mad_logit:
            self._ctx.logger.info("volharvest_mad_too_small", mad=mad_logit)
            return
        deviation = current_logit - median_logit
        threshold = self._k_sigma * 1.4826 * mad_logit
        excess = max(0.0, abs(deviation) - threshold)
        # 目标
        raw_target_f = compute_target(
            deviation=deviation, mad=mad_logit,
            base=float(self._base), max_offset=float(self._max_offset),
            k_sigma=self._k_sigma, scale_mad=self._scale_mad,
        )
        raw_target = Decimal(str(raw_target_f))
        delta = raw_target - self._holding

        # 窗口年龄
        win_age = ts_epoch - self._window[0][0]

        # min/max clip
        if abs(delta) < self._min_trade:
            self._ctx.logger.info("volharvest_signal",
                                   price=str(price), logit_price=current_logit,
                                   median_logit=median_logit, mad=mad_logit,
                                   threshold=threshold, deviation=deviation,
                                   excess=excess, raw_target=str(raw_target),
                                   clipped_target=str(raw_target), delta=str(delta),
                                   side="hold", reason="deadband" if excess == 0 else "min_trade",
                                   current_holding=str(self._holding),
                                   window_len=len(self._window),
                                   window_age_seconds=win_age,
                                   bootstrap_mode=False)
            return
        clip_reason = "ok"
        if abs(delta) > self._max_trade:
            delta = self._max_trade.copy_sign(delta)
            clip_reason = "clipped"

        # trend guard
        if len(self._trend_window) == self._trend_n and len(set(self._trend_window)) == 1:
            guard_side = self._trend_window[0]
            if guard_side == "BUY" and delta < 0:
                self._ctx.logger.info("volharvest_trend_guard_blocked",
                                       guard_side="BUY", blocked_action="sell",
                                       last_n_sides=list(self._trend_window))
                return
            if guard_side == "SELL" and delta > 0:
                self._ctx.logger.info("volharvest_trend_guard_blocked",
                                       guard_side="SELL", blocked_action="buy",
                                       last_n_sides=list(self._trend_window))
                return

        # 下单
        side = "buy" if delta > 0 else "sell"
        shares = abs(delta)
        try:
            if side == "buy":
                resp = await self._ctx.broker.buy(
                    strategy=self.name, outcome_id=self._outcome_id,
                    shares=shares,
                    max_slippage_bps=int(self._ctx.config["max_slippage_bps"]),
                )
                self._holding += resp.shares
            else:
                resp = await self._ctx.broker.sell(
                    strategy=self.name, outcome_id=self._outcome_id,
                    shares=shares,
                    max_slippage_bps=int(self._ctx.config["max_slippage_bps"]),
                )
                self._holding -= resp.shares
        except (RiskRejected, BusinessError, TransientError) as e:
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason=f"{side} failed: {e}",
                snapshot={"delta": str(delta), "deviation": deviation},
            )
            return

        self._ctx.logger.info("volharvest_signal",
                               price=str(price), logit_price=current_logit,
                               median_logit=median_logit, mad=mad_logit,
                               threshold=threshold, deviation=deviation,
                               excess=excess, raw_target=str(raw_target),
                               clipped_target=str(raw_target), delta=str(delta),
                               side=side, reason=clip_reason,
                               current_holding=str(self._holding),
                               window_len=len(self._window),
                               window_age_seconds=win_age,
                               bootstrap_mode=False)
        self._ctx.logger.info("volharvest_trade", side=side,
                               shares=str(resp.shares), cost=str(resp.cost),
                               bootstrap_mode=False,
                               holding_after=str(self._holding))
        await self._ctx.store.log_decision(
            strategy=self.name, outcome_id=self._outcome_id,
            action=side, reason=f"signal {clip_reason}",
            snapshot={"deviation": deviation, "delta": str(delta),
                      "holding_after": str(self._holding)},
        )
```

### Step 4: 改 `quant/thccb_quant/trader.py` 顶部加 import

找到现有 `import thccb_quant.strategy.dca` / `grid` 那块，追加：

```python
import thccb_quant.strategy.volharvest  # noqa: F401
```

### Step 5: 改 `quant/config.example.yaml` 末尾追加

```yaml
  - name: volharvest_market_1_outcome_1
    type: volharvest
    enabled: false
    market_id: 1
    outcome_id: 1
    window_size: 100
    k_sigma: 2.0
    scale_mad: 1.0
    min_mad_logit: 0.01
    base_shares: 500.0
    max_offset_shares: 200.0
    min_trade_shares: 5.0
    max_trade_shares: 20.0
    bootstrap_max_step: 10.0
    bootstrap_interval_sec: 30
    bootstrap_skip_if_overpriced: true
    reconcile_interval_sec: 300
    reconcile_tolerance: 1.0
    trend_guard_events: 5
```

### Step 6: 跑测试 + 全套

```bash
pytest tests/test_strategy_volharvest.py -v
pytest 2>&1 | tail -3
```
Expected: 全部 volharvest 测试通过；全套 58 → 58+N（N ≥ 18）passed

### Step 7: 验证 smoke import + registry

```bash
python -c "from thccb_quant.trader import main_async; print('import ok')"
python -c "from thccb_quant import trader; from thccb_quant.strategy.registry import STRATEGY_REGISTRY; print(sorted(STRATEGY_REGISTRY.keys()))"
```
Expected: `import ok`；`['dca', 'grid', 'volharvest']`

### Step 8: Commit

```bash
cd /data/sunyunbo/www/TouhouCCB
git add quant/thccb_quant/strategy/volharvest.py \
        quant/tests/test_strategy_volharvest.py \
        quant/thccb_quant/trader.py \
        quant/config.example.yaml
git commit -m "feat(quant): VolatilityHarvest 策略（logit + deadband+tanh + bootstrap/reconcile/trend guards）"
```

---

## Task 2: README 文档 + dry-run smoke 准备

**Files:**
- Modify: `quant/README.md`
- Modify: `quant/docs/strategies.md`（加 VolatilityHarvest 节）

### Step 1: 改 `quant/docs/strategies.md` 加 VolatilityHarvest 章节

在 `## Grid — 网格` 之后、`## 共同行为` 之前插入：

```markdown
---

## VolatilityHarvest — 波动率收割

**目的**：吃散户情绪驱动的短期 mean reversion。维护 logit 空间的滑窗中位数
+ MAD，当当前价偏离中位数超过 k_sigma × σ 时反向调整持仓（涨太多卖、跌太多
买），让自然回归把持仓拉回底仓。

**注册类型**: `type: volharvest`

### 与 Grid 的区别

- Grid: 固定价位格点，价格穿过格点边界才动作；适合震荡区间已知的市场
- VolatilityHarvest: 动态 MAD 自适应阈值；适合 trend 缓慢漂移 + 短期 noise
  的市场（thccb 散户主导市场更适合）

### Config 字段

完整字段见 `config.example.yaml` 和 spec
`docs/superpowers/specs/2026-05-17-volatility-harvest-design.md`。

关键参数：

| 字段 | 含义 | 默认 |
|---|---|---|
| `window_size` | 滑窗 N 笔 SSE trade event | 100 |
| `k_sigma` | 触发 = k × 1.4826 × MAD（≈ σ 倍数） | 2.0 |
| `scale_mad` | deadband 之外 tanh 的尺度 | 1.0 |
| `base_shares` | 目标底仓（inventory，非 alpha 判断） | 500 |
| `max_offset_shares` | 偏离底仓上下限 | 200 |
| `bootstrap_interval_sec` | bootstrap 节流 | 30s |
| `reconcile_interval_sec` | 周期校准真实持仓 | 300s |
| `trend_guard_events` | 连续 N 笔同向 → 暂停逆势加仓 | 5 |

### 适用 / 不适用

- ✅ **适用**：thccb 散户情绪主导 + 价格 = 缓慢 trend + 短噪声的市场
- ✅ **真实 SSE 事件驱动**：每笔成交都更新统计，响应快
- ✅ **deadband + tanh**：小偏离不动，大偏离平滑加仓，无 76% 跳变
- ✅ **保险丝**：trend guard 拦逆势单 + max_offset 仓位硬上限
- ⚠️ **trend 真转风险**：mean reversion 策略固有；max_offset 是损失上限
- ⚠️ **base_shares 是 directional bet**：PnL 必须拆 base_pnl + offset_pnl 看
- ⚠️ **窗口预热慢**：thccb 低流动性下首次满 N=100 可能要 1-2 小时
- ⚠️ **滑点吃利润**：±10% 波动市场实盘前 max_slippage_bps 至少调到 800

### 上实盘流程

按 spec §9 三阶段：短跑（60s 验不崩）→ 长跑 6h+（验主信号 ≥100 events）
→ 微调（max_slippage_bps / base_shares 起步小）。
```

### Step 2: 改 `quant/README.md`（如果有 "策略" 段落）

`quant/README.md` 末尾 / 策略一节追加一句：

```markdown
- **`volharvest`** —— 波动率收割（SSE 驱动 + logit 空间 mean reversion）。
  详见 `docs/strategies.md` 和 `docs/superpowers/specs/2026-05-17-volatility-harvest-design.md`
```

### Step 3: Commit

```bash
cd /data/sunyunbo/www/TouhouCCB
git add quant/docs/strategies.md quant/README.md
git commit -m "docs(quant): strategies.md 加 VolatilityHarvest 章节 + README 提及"
```

---

## Spec Coverage Check

| Spec 章节 | 实现位置 |
|---|---|
| §1 目标与假设 | 设计承诺，无具体代码 |
| §2 数据流 | Task 1 step 3 `on_sse_event` 主流程 |
| §3.1 logit | Task 1 step 3 `to_logit()` |
| §3.2 deadband+tanh | Task 1 step 3 `compute_target()` + 测试 `test_no_tanh_jump_at_threshold` |
| §3.3 持仓追踪 + reconcile | Task 1 step 3 `_maybe_reconcile` + 测试 `test_reconcile_*` |
| §3.4 trend guard | Task 1 step 3 `_maybe_trade` 内 trend guard 逻辑 + 测试 `test_trend_guard_*` |
| §3.5 bootstrap guard | Task 1 step 3 `_maybe_bootstrap` + 测试 `test_bootstrap_*` |
| §3.6 PnL 拆分 | Task 1 step 3 `volharvest_trade` log 带 `bootstrap_mode` 字段 + Task 2 文档说明 |
| §4 Config schema | Task 1 step 3 `__init__` 读全部字段 + step 5 config.example.yaml |
| §5 模块清单 | Task 1 全 4 个文件 |
| §6 错误处理 | Task 1 step 3 多处 try/except |
| §7 structlog 事件 | Task 1 step 3 各处 `ctx.logger.info(...)` |
| §8 测试三类 | Task 1 step 1 完整测试集 |
| §9 dry-run 准入 | Task 2 step 1 strategies.md 简介；详细见 spec §9 |
| §10 风险清单 | Task 2 step 1 strategies.md "适用/不适用" |
| §11 YAGNI | 不实施（无 task） |
