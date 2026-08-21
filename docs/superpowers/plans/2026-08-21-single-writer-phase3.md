# 单写者内存状态机 · 阶段 3（客户端计算 + summary 降级）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 spec 阶段 3：估值计算（MTM/LCV/浮盈/净值/rank/成交预览）下放客户端（`utils/lmsr.ts` 闭式公式 + golden case 对拍），`/user/summary` 与 `/user/holdings` 降级为"只返回客户端算不出来的东西"，删掉成交后 4 个 REST 往返与 TradingView 轮询——每次成交后的服务端全仓 LMSR 计算彻底移出请求路径。

**Architecture:** 后端契约先行——`rank.py` 阈值查表化、buy/sell 响应 `new_cash` 升 6dp 全精度（客户端 cash 基线）、summary/holdings 瘦身（margin_status 仍服务端权威，仅 debt>0 时算一次 LCV）。前端新增 `utils/lmsr.ts`（闭式公式单一实现，golden fixture 由后端脚本生成对拍）与 `utils/valuation.ts`（纯函数估值层，vitest 可测），`stores/user.ts` 重写为「summary 新契约 + priceContext（市场价格上下文）+ 派生 getters」，组件从直读 summary 字段迁到 getters；成交后本地 apply，价格由阶段 2 的 tick 帧续写。

**Tech Stack:** FastAPI + SQLAlchemy async（后端，无新依赖）；Vue 3 + Pinia + vitest（新增 devDependency，用户已批准）。

**Spec:** `docs/superpowers/specs/2026-08-21-single-writer-design.md`（本计划实现其 § 6 全部 + § 8 阶段 3）

## Global Constraints

- **前置依赖：阶段 2 已合入本分支**（tick 帧 + build 版本自刷机制）。阶段 3 部署瞬间旧 tab 拿到砍过的 summary 的 NaN 风险由 build 自刷兜底（spec § 8 阶段 2 前置项）。`TradingView.vue` / `stores/market.ts` / `useMarketRealtime.ts` 已被阶段 2 改过——本计划对这三个文件按**符号名**定位（行号会漂移）；其余文件行号基于 `d3f1521`。
- 生产站在跑；全部工作在分支 `perf/2026-08-21-single-writer` 上，**不 push**（push 是 CLAUDE.md 红线，完成后交用户决定）
- **Task 3 起后端契约与前端暂不匹配，直到 Task 8 完成**——中间任务态不可部署；每任务的验证以各自侧的编译/测试为准，前后端联调在 Task 9 收口
- `stores/`、`src/api/` 是 CLAUDE.md 高敏感区——本阶段是 spec 明示的"整个重构改动面最大的一节"（spec § 6.6），已获授权；改动仍须最小化、每步测试兜底
- **精度规则**：`/user/summary` 的 cash/debt/positions 与 buy/sell 响应的 `new_cash` 一律 6dp 全精度 Money（客户端本地 apply 基线，spec § 6.4 配套修正）；响应 `cost` 字段维持 2dp 不变；前端不用 `Number()` 丢精度的场景不存在（全是 float 序列化）
- **不动**：`/market/quote` 端点与 `_QUOTE_CACHE`（删除在阶段 5）；`services/wealth.py` 两个函数（强平 sweep / `/admin/wealth` / 排行榜仍用，spec § 6.4）；`/market/leaderboard` 的服务端 rank；首页三处轮询（spec § 6.7）；`gapToken` reconcile 机制
- 不改 schema（本计划零 DB 变更）；不动 `vite.config.ts`（高敏感）——vitest 用独立 `vitest.config.ts`
- 后端验证（每 task commit 前）：`python -m py_compile $(find app -name '*.py')` + `python -c "import app.main"` + 该 task 的 pytest；前端验证：`npm run type-check` + `npm run lint` + `npm run test:unit`；计划收尾跑双侧全量
- commit 风格：`feat:/fix:/refactor:/test:/docs:` + 中文，一个可独立回滚的改动一条；按文件 `git add <path>`
- 后端命令在 `backend/` 下执行，前端命令在 `thccb-frontend/` 下执行，quant 命令在 `quant/` 下执行

---

### Task 1: rank.py 阈值查表化（summary 下发的数据基础）

**Files:**
- Modify: `backend/app/services/rank.py`（全文件 21 行重写）
- Test: `backend/tests/test_rank_thresholds.py`（新增）

**Interfaces:**
- Produces（Task 3 依赖）:
  - `RANK_THRESHOLDS: list[tuple[Optional[Decimal], str]]`——降序阈值表，最后一项 `(None, "人类灵(已爆仓)")` 是兜底档
  - `rank_title(net_worth: Decimal) -> str`——签名不变，改为查表实现，行为逐字节等价（排行榜等既有调用方零感知）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_rank_thresholds.py`：

```python
"""RANK_THRESHOLDS 查表与 rank_title 行为等价性。"""
from decimal import Decimal

from app.services.rank import RANK_THRESHOLDS, rank_title

D = Decimal


def test_thresholds_table_shape():
    """降序 5 档 + 1 个 None 兜底档；顺序即优先级。"""
    assert len(RANK_THRESHOLDS) == 6
    numeric = [t for t, _ in RANK_THRESHOLDS if t is not None]
    assert numeric == sorted(numeric, reverse=True)
    assert RANK_THRESHOLDS[-1] == (None, "人类灵(已爆仓)")


def test_rank_title_matches_legacy_behavior():
    """与旧 if 链逐界点等价：> 是严格大于，等于阈值落到下一档。"""
    cases = [
        (D("30000.01"), "ZUN"),
        (D("30000"), "炒炒币大亨"),      # 等于阈值不进上档
        (D("10000.01"), "炒炒币大亨"),
        (D("10000"), "妖怪操盘手"),
        (D("3000.01"), "妖怪操盘手"),
        (D("3000"), "天狗交易员"),
        (D("1000.01"), "天狗交易员"),
        (D("1000"), "人里居民"),
        (D("300.01"), "人里居民"),
        (D("300"), "人类灵(已爆仓)"),
        (D("0"), "人类灵(已爆仓)"),
        (D("-500"), "人类灵(已爆仓)"),
    ]
    for nw, expected in cases:
        assert rank_title(nw) == expected, f"net_worth={nw}"
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_rank_thresholds.py -x -q`
Expected: FAIL —— `ImportError: cannot import name 'RANK_THRESHOLDS'`

- [ ] **Step 3: 重写 rank.py**

```python
"""统一的用户称号体系。

全站只按 net_worth 一个口径定称号——排行榜（净值/消费）等服务端场景调
rank_title；/user/summary 把 RANK_THRESHOLDS 表下发给客户端本地映射
（spec 2026-08-21 § 6.4：net_worth 已下放前端算，rank 必须跟着下放，
否则服务端 rank 会和客户端显示的净值对不上）。阈值与文案改动只需改这一张表。

约定：net_worth 含义由调用方决定（个人/财富榜 net_worth = cash - debt
+ 持仓估值；消费榜 = 兑换消费总额 - 当前债务）。负值/0 都落到兜底档。
"""
from decimal import Decimal
from typing import Optional

# 降序阈值表；(None, ...) 是兜底档。判定规则：命中第一个
# 「thr is None 或 net_worth > thr」的条目（> 是严格大于）。
# 客户端（stores/user.ts::rankTitle）用同一规则本地映射。
RANK_THRESHOLDS: list[tuple[Optional[Decimal], str]] = [
    (Decimal("30000"), "ZUN"),
    (Decimal("10000"), "炒炒币大亨"),
    (Decimal("3000"), "妖怪操盘手"),
    (Decimal("1000"), "天狗交易员"),
    (Decimal("300"), "人里居民"),
    (None, "人类灵(已爆仓)"),
]


def rank_title(net_worth: Decimal) -> str:
    for thr, title in RANK_THRESHOLDS:
        if thr is None or net_worth > thr:
            return title
    return RANK_THRESHOLDS[-1][1]  # 不可达，防御
```

- [ ] **Step 4: 跑测确认通过（含既有调用方回归）**

Run: `python -m pytest tests/test_rank_thresholds.py tests/test_leaderboard.py -q && python -m py_compile $(find app -name '*.py')`
Expected: 全 PASS（leaderboard 的 rank 行为逐字节不变）

- [ ] **Step 5: Commit**

```bash
git add app/services/rank.py tests/test_rank_thresholds.py
git commit -m "refactor(rank): 称号阈值查表化——RANK_THRESHOLDS 供 summary 下发客户端本地映射"
```

---

### Task 2: buy/sell 响应 new_cash 升 6dp 全精度

**Files:**
- Modify: `backend/app/schemas/market.py:72-76`（`TradeResponse.new_cash` 改 `Money`）
- Modify: `backend/app/api/v1/market.py:679`（buy 老路径）、`backend/app/api/v1/market.py:852`（sell 老路径）
- Modify: `backend/app/services/writer_ops.py:161`（op_buy）、`backend/app/services/writer_ops.py:291`（op_sell）
- Test: `backend/tests/test_trade_new_cash_precision.py`（新增）；grep 修既有引用

**Interfaces:**
- Produces: `TradeResponse.new_cash: Money`——JSON wire 类型仍是 number，但携带 6dp 全精度（原 2dp）。Task 8 的前端本地 apply 用它做 cash 基线（spec § 6.4："2dp 舍入会每笔累积最多 0.005 的漂移"）
- `cost` 字段维持 2dp float 不变

**背景**：现状两条路径都是 `float(x.quantize(Decimal("0.01")))`。改法遵守既有 Schema Decimal 规则（`docs/schema-conventions.md` / 记忆 `feedback_schema_decimal_money`）：字段类型 `Money`（serialize→float），handler 直接传 Decimal。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_trade_new_cash_precision.py`（`_make_user`/`_seed_market` 模式抄 `tests/test_user_summary_margin.py` 顶部 helper；市场种子参考 `tests/test_writer_buy.py` 的 `_seed_market`）：

```python
"""buy/sell 响应 new_cash 是 6dp 全精度（客户端本地 apply 的 cash 基线，spec §6.4）。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, User


async def _make_user(cash=Decimal("1000")):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                     casdoor_id=f"cd_{suffix}", cash=cash, debt=Decimal("0"))
            s.add(u)
            await s.flush()
            uid = u.id
    token = create_access_token(uid)
    return uid, {"Authorization": f"Bearer {token}"}


async def _seed_market():
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=MarketStatus.TRADING)
        s.add(m)
        await s.flush()
        oids = []
        for label in ("a", "b"):
            o = Outcome(market_id=m.id, label=label, total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        await s.commit()
        return m.id, oids


@pytest.mark.asyncio
async def test_buy_then_sell_new_cash_is_6dp_exact(client):
    """new_cash 与 DB 里 user.cash 的 6dp 值完全一致（非 2dp 舍入）。"""
    uid, h = await _make_user(cash=Decimal("1000"))
    _, oids = await _seed_market()

    r = await client.post("/api/v1/market/buy", headers=h,
                          json={"outcome_id": oids[0], "shares": 7,
                                "accept_any_slippage": True})
    assert r.status_code == 200, r.text
    new_cash_resp = Decimal(str(r.json()["new_cash"]))
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert new_cash_resp == u.cash.quantize(Decimal("0.000001"))
        # 7 份对 b=100 的成本带 6dp 尾数——若响应被 2dp 截断这里必炸
        assert u.cash.quantize(Decimal("0.000001")) != u.cash.quantize(Decimal("0.01"))

    r2 = await client.post("/api/v1/market/sell", headers=h,
                           json={"outcome_id": oids[0], "shares": 3,
                                 "accept_any_slippage": True})
    assert r2.status_code == 200, r2.text
    new_cash_resp2 = Decimal(str(r2.json()["new_cash"]))
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert new_cash_resp2 == u.cash.quantize(Decimal("0.000001"))
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_trade_new_cash_precision.py -x -q`
Expected: FAIL —— `new_cash_resp == u.cash.quantize(6dp)` 断言失败（响应还是 2dp）

- [ ] **Step 3: 改 schema 与四处 handler**

`schemas/market.py:72-76`：

```python
class TradeResponse(BaseModel):
    shares: float
    cost: float
    # 6dp 全精度（Money serialize→float）：阶段 3 起是客户端本地 apply 的
    # cash 基线，2dp 舍入会每笔累积最多 0.005 漂移（spec §6.4 配套修正）
    new_cash: Money
    message: str
```

`market.py:679`（buy 返回 dict 内）：

```python
        "new_cash": quantize_cost(locked_user.cash),
```

`market.py:852`（sell）同改为 `quantize_cost(locked_user.cash)`。

`writer_ops.py:161`（op_buy response）：

```python
            "new_cash": quantize_cost(new_cash),
```

`writer_ops.py:291`（op_sell）同改为 `quantize_cost(new_cash)`。

（`quantize_cost` 两个文件都已 import；`response_model=TradeResponse` 会把 Decimal 经 Money 序列化为 float。）

- [ ] **Step 4: grep 修既有测试引用**

Run: `grep -rn "new_cash" tests/`
对断言 `new_cash == ...quantize(Decimal("0.01"))` 形式的既有测试（候选：`test_writer_buy.py` / `test_writer_sell.py` / `test_writer_e2e.py` 的新旧路径 parity 断言），把期望值改成 `quantize_cost(...)` 6dp。parity 断言（新旧路径响应相等）无需改——两条路径同步变更。

- [ ] **Step 5: 跑测确认通过**

Run: `python -m pytest tests/test_trade_new_cash_precision.py tests/test_writer_buy.py tests/test_writer_sell.py tests/test_writer_e2e.py tests/test_market_slippage_lock.py -q`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add app/schemas/market.py app/api/v1/market.py app/services/writer_ops.py tests/test_trade_new_cash_precision.py
git add tests/test_writer_buy.py tests/test_writer_sell.py tests/test_writer_e2e.py   # 若 Step 4 有改动
git commit -m "feat(trade): new_cash 升 6dp 全精度 Money——阶段 3 客户端本地 apply 的 cash 基线（双路径）"
```

---

### Task 3: /user/summary 新契约 + /user/holdings 瘦身（后端）

**Files:**
- Modify: `backend/app/schemas/user.py`（`UserSummary` / `HoldingRead` 重写，新增 `SummaryPosition` / `RankThresholdItem`）
- Modify: `backend/app/api/v1/user.py:39-207`（两个 endpoint 重写）+ 顶部 import 清理
- Test: `backend/tests/test_user_summary_contract.py`（新增）；`backend/tests/test_user_summary_margin.py`（重写断言）；`backend/tests/test_user_summary_dual_caliber.py`（**删除**——双口径字段已下放前端，服务端双口径语义由 `test_wealth_mtm.py` + 前端 golden 测试覆盖）

**Interfaces:**
- Produces（Task 6 前端依赖的确切 JSON 形状）:
  - `/user/summary` → `{cash, debt, positions: [{outcome_id, market_id, amount, cost_basis}], margin_hard_threshold, margin_soft_threshold, sell_fee_rate, rank_thresholds: [{min_net_worth: number|null, title}], margin_status, liquidation_protected, last_liquidated_at, equipped_title, all_titles}`；cash/debt/amount/cost_basis 6dp
  - **删除**：`holdings_value` / `holdings_value_liquidation` / `net_worth` / `net_worth_liquidation` / `unrealized_pnl` / `unrealized_pnl_liquidation` / `total_cost_basis` / `rank` / `margin_ratio`（margin_ratio 也删——前端用 LCV 本地估算显示，权威判定在 sweep，spec § 6.3）
  - `/user/holdings` → `[{market_id, market_title, outcome_id, outcome_label, amount, cost_basis}]`（6dp；删 avg_price/current_price/market_value/unrealized_pnl/unrealized_pnl_liquidation）
- Consumes: Task 1 `RANK_THRESHOLDS`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_user_summary_contract.py`（`_make_user`/`_seed_liquidation_config` helper 逐字抄 `tests/test_user_summary_margin.py:19-40`，另加建仓 helper）：

```python
"""阶段 3 /user/summary 与 /user/holdings 新契约（spec §6.4）。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, Position, SiteConfig, User

# （此处粘贴 test_user_summary_margin.py 的 _make_user 与 _seed_liquidation_config，内容一致）


async def _seed_position(uid, shares="10", cost="5.5", total_shares="10"):
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0,
                   status=MarketStatus.TRADING)
        s.add(m)
        await s.flush()
        o = Outcome(market_id=m.id, label="a", total_shares=Decimal(total_shares))
        o2 = Outcome(market_id=m.id, label="b", total_shares=Decimal("0"))
        s.add(o); s.add(o2)
        await s.flush()
        s.add(Position(user_id=uid, outcome_id=o.id,
                       amount=Decimal(shares), cost_basis=Decimal(cost)))
        await s.commit()
        return m.id, o.id


REMOVED_FIELDS = [
    "holdings_value", "holdings_value_liquidation", "net_worth",
    "net_worth_liquidation", "unrealized_pnl", "unrealized_pnl_liquidation",
    "total_cost_basis", "rank", "margin_ratio",
]


@pytest.mark.asyncio
async def test_summary_new_contract_shape(client):
    uid, h = await _make_user(cash=Decimal("1000.123456"))
    mid, oid = await _seed_position(uid, shares="10.5", cost="5.123456")
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    for f in REMOVED_FIELDS:
        assert f not in body, f"已删字段泄漏: {f}"

    assert body["cash"] == 1000.123456          # 6dp 全精度，非 2dp
    assert body["debt"] == 0.0
    assert body["positions"] == [{
        "outcome_id": oid, "market_id": mid,
        "amount": 10.5, "cost_basis": 5.123456,
    }]
    assert body["margin_status"] == "healthy"
    assert body["liquidation_protected"] is False
    assert isinstance(body["sell_fee_rate"], (int, float))
    # rank_thresholds：6 条、降序、末条 null 兜底
    rt = body["rank_thresholds"]
    assert [x["title"] for x in rt] == \
        ["ZUN", "炒炒币大亨", "妖怪操盘手", "天狗交易员", "人里居民", "人类灵(已爆仓)"]
    assert rt[0]["min_net_worth"] == 30000.0
    assert rt[-1]["min_net_worth"] is None
    assert "equipped_title" in body and "all_titles" in body


@pytest.mark.asyncio
async def test_summary_margin_status_still_server_side(client):
    """debt>0 才算 LCV：无持仓 cash=400 debt=1000 → ratio=-0.6 → danger。"""
    await _seed_liquidation_config(hard="0.2", soft="0.5")
    _, h = await _make_user(cash=Decimal("400"), debt=Decimal("1000"))
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["margin_status"] == "danger"


@pytest.mark.asyncio
async def test_holdings_slim_contract(client):
    uid, h = await _make_user()
    mid, oid = await _seed_position(uid, shares="10.5", cost="5.123456")
    r = await client.get("/api/v1/user/holdings", headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row == {
        "market_id": mid, "market_title": "t",
        "outcome_id": oid, "outcome_label": "a",
        "amount": 10.5, "cost_basis": 5.123456,
    }
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_user_summary_contract.py -x -q`
Expected: FAIL —— `已删字段泄漏: holdings_value`

- [ ] **Step 3: 重写 schemas/user.py 的 UserSummary / HoldingRead**

替换 `schemas/user.py:11-72`（`HoldingRead` 与 `UserSummary` 两个类；`UserSummaryTitleItem`、`TransactionRead`、`UserSummary.model_rebuild()` 保留）：

```python
class HoldingRead(BaseModel):
    """阶段 3 瘦身（spec §6.4）：只有标签 + 数量/成本（6dp）。
    估值列（现价/市值/浮盈/均价）由客户端 utils/lmsr.ts 本地算。"""
    market_id: int
    market_title: str
    outcome_id: int
    outcome_label: str
    amount: Money        # 6dp（原 2dp 展示口径废弃——是客户端估值输入）
    cost_basis: Money    # 6dp


class SummaryPosition(BaseModel):
    outcome_id: int
    market_id: int
    amount: Money        # 6dp
    cost_basis: Money    # 6dp


class RankThresholdItem(BaseModel):
    """rank 阈值档；min_net_worth=None 是兜底档。客户端判定规则：
    命中第一个「min_net_worth is None 或 net_worth > min_net_worth」的条目。"""
    min_net_worth: Optional[Money] = None
    title: str


class UserSummary(BaseModel):
    """阶段 3 新契约（spec §6.4）：只返回客户端算不出来的东西。

    holdings_value / net_worth / unrealized_pnl / rank / margin_ratio 等
    派生值由前端 utils/lmsr.ts + priceContext 本地算；margin_status 仍是
    服务端权威（LCV 口径，spec §6.3——真正触发强平的是 sweep）。
    cash 是客户端成交后本地 apply 的基线，6dp 全精度。
    """
    cash: Money
    debt: Money
    positions: List[SummaryPosition] = []
    margin_hard_threshold: Money = Decimal("0.2")
    margin_soft_threshold: Money = Decimal("0.5")
    sell_fee_rate: Money = Decimal("0")
    rank_thresholds: List[RankThresholdItem] = []
    margin_status: str = "healthy"
    liquidation_protected: bool = False
    last_liquidated_at: Optional[datetime] = None
    equipped_title: Optional[TitleChipRead] = None
    all_titles: List["UserSummaryTitleItem"] = []
```

- [ ] **Step 4: 重写 user.py 两个 endpoint**

`user.py:39-124` 的 `get_user_summary` 整体替换为：

```python
@router.get("/summary", response_model=UserSummary, summary="获取资产概览")
async def get_user_summary(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    """阶段 3 新契约（spec §6.4）：只返回客户端算不出来的东西。

    每次成交后必被调用 × 每次两遍全仓 LMSR 的时代结束：margin_status
    仅在 debt>0 时算一次 LCV，无债用户零 LMSR 开销。调用时机也随之降频
    （登录 / 手动刷新 / gap reconcile，成交后不再调用）。
    """
    pos_rows = (await db.execute(
        select(Position.outcome_id, Outcome.market_id,
               Position.amount, Position.cost_basis)
        .join(Outcome, Outcome.id == Position.outcome_id)
        .where(Position.user_id == user.id, Position.amount > 0)
    )).all()
    positions = [
        {
            "outcome_id": int(r[0]),
            "market_id": int(r[1]),
            "amount": quantize_cost(r[2]),
            "cost_basis": quantize_cost(r[3]),
        }
        for r in pos_rows
    ]

    hard = await _site_config.get_decimal_or(db, "liquidation_hard_threshold", Decimal("0.2"))
    soft = await _site_config.get_decimal_or(db, "liquidation_soft_threshold", Decimal("0.5"))
    sell_fee_rate = await _site_config.get_decimal_or(db, "sell_fee_rate", ZERO)

    # margin_status 服务端权威（保守 LCV 口径，docs/holdings-value-semantics.md）。
    # 只有 debt>0 才需要跑全仓 LMSR。
    margin_status = "healthy"
    if user.debt > ZERO:
        holdings_lcv = (
            await compute_users_holdings_value(db, user_ids=[user.id])
        ).get(user.id, ZERO)
        margin_ratio = ((user.cash - user.debt + holdings_lcv) / user.debt
                        ).quantize(Decimal("0.000001"))
        if margin_ratio < hard:
            margin_status = "danger"
        elif margin_ratio < soft:
            margin_status = "warning"

    # 流动性危机保护标志：语义不变（review I3）
    liquidation_protected = await user_has_halt_holdings(db, user.id)

    from app.services import title_service as _title_service
    equipped_t = await _title_service.get_equipped_chip(db, user.id)
    my_title_rows = await _title_service.list_my_titles(db, user.id)

    return {
        "cash": quantize_cost(user.cash),   # 6dp——客户端 cash 基线
        "debt": quantize_cost(user.debt),
        "positions": positions,
        "margin_hard_threshold": hard.quantize(Decimal("0.0001")),
        "margin_soft_threshold": soft.quantize(Decimal("0.0001")),
        "sell_fee_rate": sell_fee_rate,
        "rank_thresholds": [
            {"min_net_worth": thr, "title": title} for thr, title in RANK_THRESHOLDS
        ],
        "margin_status": margin_status,
        "liquidation_protected": liquidation_protected,
        "last_liquidated_at": user.last_liquidated_at,
        "equipped_title": (
            {"id": equipped_t.id, "name": equipped_t.name,
             "color": equipped_t.color, "icon": equipped_t.icon}
            if equipped_t else None
        ),
        "all_titles": [
            {"id": t.id, "name": t.name, "color": t.color, "icon": t.icon,
             "description": t.description, "sort_order": t.sort_order}
            for _ut, t in my_title_rows
        ],
    }
```

`user.py:127-207` 的 `get_my_holdings` 整体替换为：

```python
@router.get("/holdings", response_model=List[HoldingRead], summary="获取持仓明细")
async def get_my_holdings(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    """阶段 3 瘦身（spec §6.4）：只返回标签 + 数量/成本；估值下放客户端。"""
    stmt = (
        select(Position)
        .where(Position.user_id == user.id, Position.amount > 0)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
        .order_by(Position.id.desc())
    )
    positions: List[Position] = (await db.execute(stmt)).scalars().all()
    return [
        HoldingRead(
            outcome_id=pos.outcome_id,
            outcome_label=pos.outcome.label,
            market_id=pos.outcome.market_id,
            market_title=pos.outcome.market.title,
            amount=quantize_cost(pos.amount),
            cost_basis=quantize_cost(pos.cost_basis),
        )
        for pos in positions
    ]
```

import 清理（`user.py` 顶部）：
- `from app.services.lmsr import ...` 只留 `quantize_cost`（`calculate_lmsr_cost` / `get_current_price` / `quantize_price` 不再用）
- `from app.services.wealth import ...` 删 `compute_users_holdings_value_mtm`（留 `compute_users_holdings_value`、`user_has_halt_holdings`）
- `from app.services.rank import rank_title` 改为 `from app.services.rank import RANK_THRESHOLDS`
- 删 `ONE = Decimal("1")`（唯一使用点在旧 holdings LMSR 块）
- `MarketStatus` import 若无其他引用一并删（grep 确认）

- [ ] **Step 5: 处置旧 summary 测试**

- `git rm tests/test_user_summary_dual_caliber.py`（测的是已删除的双口径响应字段；双口径**语义**仍由 `test_wealth_mtm.py`（服务端）与 Task 5/6 的前端测试覆盖）
- 重写 `tests/test_user_summary_margin.py` 的断言：删掉所有 `margin_ratio` / `net_worth` / `rank` 响应断言，保留并改为只断言 `margin_status` 与 `last_liquidated_at`（该文件的 danger/warning/healthy 与 `liquidation_protected` 场景是 margin 权威判定的回归护栏，逐用例把 `body["margin_ratio"]` 类断言换成对 `body["margin_status"]` 的等价断言；`"net_worth" in body` 类断言直接删）

- [ ] **Step 6: 跑测确认通过**

Run: `python -m pytest tests/test_user_summary_contract.py tests/test_user_summary_margin.py -q && python -m py_compile $(find app -name '*.py') && python -c "import app.main"`
Expected: 全 PASS

- [ ] **Step 7: Commit**

```bash
git add app/schemas/user.py app/api/v1/user.py tests/test_user_summary_contract.py tests/test_user_summary_margin.py
git rm tests/test_user_summary_dual_caliber.py
git commit -m "feat(user): summary/holdings 阶段 3 瘦身——估值下放客户端，margin_status 仍服务端权威（仅 debt>0 算 LCV）"
```

---

### Task 4: 既有测试修复 + quant bot REST 模型放宽 + 文档同步

**Files:**
- Modify: `backend/tests/`（grep 命中的存量测试）
- Modify: `quant/thccb_quant/client/rest.py:68-87`（`HoldingRead` / `UserSummary` 两个 pydantic 模型）
- Modify: `docs/holdings-value-semantics.md`（"谁在算 MTM / LCV"）

**背景**：summary/holdings 契约变更有两类下游：(a) 后端存量测试断言旧字段；(b) **quant bot 的 REST 模型**——`quant/thccb_quant/client/rest.py:82-87` 的 `UserSummary` 把 `holdings_value` / `net_worth` 声明为必填（虽然 bot 实际只用 `cash`，`meanrev.py:97`），`HoldingRead:68-79` 把 `avg_price` / `current_price` / `market_value` / `unrealized_pnl` 声明为必填（bot 只用 `outcome_id` / `amount`）。不放宽的话 bot 一升级主站就 ValidationError 起不来。spec 只保证了 SSE 契约的双发兼容，REST 这条是阶段 3 特有的破坏点。

- [ ] **Step 1: 后端存量测试 grep + 分类修复**

Run: `grep -rln "user/summary\|user/holdings" tests/` 与 `grep -rn "holdings_value\|net_worth\|unrealized_pnl\|total_cost_basis\|margin_ratio" tests/`

分类规则（**只改打 `/api/v1/user/summary` 或 `/api/v1/user/holdings` 的断言**，服务层断言不动）：
- `test_wealth_mtm.py` / `test_wealth_stats.py` / `test_leaderboard.py` / `test_title_leaderboard_chips.py`：命中的是 wealth 服务函数与 leaderboard 响应的 `net_worth`——**语义不变，不改**（跑一遍确认绿）
- `test_loan_api.py` / `test_loan_service.py`：loan 自己的响应字段（借款额度的 net_worth 口径）不动；若有调 `/user/summary` 断言旧字段的用例，改为断言新字段（`cash` / `margin_status`）或直接调 wealth 服务函数验证数值
- `test_liquidation_e2e.py` / `test_liquidation_public.py` / `test_liquidation_schema.py` / `test_liquidation_event_mode_field.py` / `test_liquidation_sweep_perf.py`：命中多为 LiquidationEvent 自身的 `pre_margin_ratio` 等字段——不动；只有借 `/user/summary` 验证用户态的断言需要换成 `margin_status` 或直查 DB
- `test_configurable_economy.py`：若断言 summary 的 `cash`（initial_balance 场景）——`cash` 仍在，仅精度从 2dp 变 6dp，按需放宽为 `pytest.approx`

- [ ] **Step 2: 跑全量后端测试**

Run: `python -m pytest -q`
Expected: 全 PASS（含 Task 1-3 的新测试；有 1 个与本重构无关的已知过期 fail 时按记忆 `feedback_pytest_workflow` 对待，在日志注明）

- [ ] **Step 3: 放宽 quant bot 的 REST 模型**

`quant/thccb_quant/client/rest.py:68-87` 改为（两个模型都 `extra="allow"`，删掉主站已不再返回的必填字段——bot 代码只消费 `cash` 与 `outcome_id`/`amount`/`cost_basis`，grep `\.holdings_value\|\.net_worth\|\.avg_price\|\.current_price\|\.market_value\|\.unrealized_pnl` in `quant/thccb_quant/` 确认无其他引用后执行）：

```python
class HoldingRead(BaseModel):
    model_config = ConfigDict(extra="allow")
    outcome_id: int
    outcome_label: str
    market_id: int
    market_title: str
    amount: Decimal
    cost_basis: Decimal
    # 阶段 3 起主站不再返回估值列（avg_price/current_price/market_value/
    # unrealized_pnl）——bot 从未消费它们，需要现价时用 SSE 价格视图


class UserSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    cash: Decimal
    debt: Decimal
    # 阶段 3 起主站不再返回 holdings_value/net_worth（客户端本地算）；
    # bot 只用 cash（meanrev bootstrap），如需净值自行由持仓+价格推
```

- [ ] **Step 4: 跑 quant 测试**

Run: `cd ../quant && python -m pytest -q`（用 quant 自己的 venv：`.venv/bin/python -m pytest -q`）
Expected: PASS；若有测试构造旧字段的 fixture，同步删除字段

- [ ] **Step 5: 更新 docs/holdings-value-semantics.md**

在"谁在算"相关小节（通读后落点）写明新分工：

- **服务端仍算**：强平 sweep 判定、`/admin/wealth`、`/market/leaderboard` 排序、`/user/summary` 的 `margin_status`（仅 debt>0）——全部 LCV/MTM 权威口径走 `services/wealth.py`，不变
- **客户端现算**（阶段 3 起）：`/user/summary`、`/user/holdings` 不再返回 MTM/LCV/净值/浮盈/rank；前端 `utils/lmsr.ts`（闭式公式）+ `utils/valuation.ts` + `stores/user.ts` 的 priceContext 本地算，HALT 语义与 wealth.py 镜像（MTM 计入 HALT / LCV 不计入且立即变现浮盈 = -cost_basis）
- 客户端估值是**显示口径**（价格可能轻微陈旧、无 6dp 资金量化），权威判定永远在服务端（spec § 6.3）

- [ ] **Step 6: Commit**

```bash
git add tests/ ../quant/thccb_quant/client/rest.py ../docs/holdings-value-semantics.md
git commit -m "test(user): 存量测试适配 summary 新契约 + quant bot REST 模型放宽 + 估值语义文档同步"
```

---

### Task 5: vitest 基建 + utils/lmsr.ts + golden 对拍

**Files:**
- Modify: `thccb-frontend/package.json`（devDependency `vitest` + script `test:unit`）
- Create: `thccb-frontend/vitest.config.ts`
- Create: `thccb-frontend/src/utils/lmsr.ts`
- Create: `backend/scripts/gen_lmsr_golden.py`
- Create: `thccb-frontend/src/utils/__tests__/lmsr.golden.json`（脚本生成后 commit）
- Test: `thccb-frontend/src/utils/__tests__/lmsr.spec.ts`

**Interfaces:**
- Produces（Task 6/8 依赖的确切签名，全部纯函数）:
  - `buyCost(p: number, delta: number, b: number): number`——ΔC
  - `sellProceeds(p: number, delta: number, b: number): number`——卖出收入（正数，未扣费）
  - `pricesAfterTrade(prices: number[], idx: number, delta: number, b: number): number[]`——delta>0 买入 / delta<0 卖出
  - `mtmValue(amount: number, p: number): number`
  - `lcvValue(amount: number, p: number, b: number, sellFeeRate: number): number`
- 测试文件放 `src/utils/__tests__/`——`tsconfig.app.json` 已 exclude 该目录，vue-tsc 不扫、vitest 自己转译，JSON import 无需动 tsconfig

- [ ] **Step 1: 装 vitest + 配置**

```bash
npm install -D vitest
```

新建 `thccb-frontend/vitest.config.ts`：

```typescript
// 独立于 vite.config.ts（高敏感文件不动）；只跑纯函数单测，node 环境足够
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    include: ['src/**/__tests__/*.spec.ts'],
    environment: 'node',
  },
})
```

`package.json` scripts 加一行（`"type-check"` 之后）：

```json
    "test:unit": "vitest run",
```

- [ ] **Step 2: 写失败测试（先于实现）**

`thccb-frontend/src/utils/__tests__/lmsr.spec.ts`：

```typescript
// utils/lmsr.ts golden 对拍（spec §6.6 两层，与 §6.2 第 3 条的两层偏差一一对应）：
//   数学层：喂全精度 p，与后端 services/lmsr.py 对拍，相对误差 < 1e-12
//   线上层：喂 8dp 量化 p（真实线上输入），相对误差 < 1e-6
// fixture 由 backend/scripts/gen_lmsr_golden.py 生成（服务端 q 路径权威值）。
import { describe, expect, it } from 'vitest'
import { buyCost, sellProceeds, pricesAfterTrade, mtmValue, lcvValue } from '../lmsr'
import golden from './lmsr.golden.json'

const relErr = (actual: number, expected: number) =>
  Math.abs(actual - expected) / Math.max(Math.abs(expected), 1e-300)

interface TradeCase {
  b: number; idx: number; delta: number; side: string
  p_full: number[]; p_8dp: number[]
  expected_amount: number; expected_prices_after: number[]
}
interface HoldingCase {
  b: number; idx: number; amount: number; sell_fee_rate: number
  p_full: number[]; p_8dp: number[]
  expected_mtm: number; expected_lcv: number
}

const tradeAmount = (c: TradeCase, prices: number[]) =>
  c.side === 'buy'
    ? buyCost(prices[c.idx]!, c.delta, c.b)
    : sellProceeds(prices[c.idx]!, c.delta, c.b)

describe('utils/lmsr golden 对拍', () => {
  for (const [i, c] of (golden.trades as TradeCase[]).entries()) {
    it(`数学层 trade#${i} ${c.side} Δ=${c.delta} b=${c.b}`, () => {
      expect(relErr(tradeAmount(c, c.p_full), c.expected_amount)).toBeLessThan(1e-12)
      const after = pricesAfterTrade(
        c.p_full, c.idx, c.side === 'buy' ? c.delta : -c.delta, c.b)
      for (let j = 0; j < after.length; j++) {
        // 价格用绝对误差（价格 ∈ [0,1]，渐近 case 的 0 价无相对误差可言）
        expect(Math.abs(after[j]! - c.expected_prices_after[j]!)).toBeLessThan(1e-12)
      }
    })
    it(`线上层 trade#${i}（8dp 量化输入）`, () => {
      expect(relErr(tradeAmount(c, c.p_8dp), c.expected_amount)).toBeLessThan(1e-6)
    })
  }

  for (const [i, c] of (golden.holdings as HoldingCase[]).entries()) {
    it(`数学层 holding#${i}`, () => {
      expect(relErr(mtmValue(c.amount, c.p_full[c.idx]!), c.expected_mtm)).toBeLessThan(1e-12)
      expect(relErr(lcvValue(c.amount, c.p_full[c.idx]!, c.b, c.sell_fee_rate),
                    c.expected_lcv)).toBeLessThan(1e-12)
    })
    it(`线上层 holding#${i}（8dp 量化输入）`, () => {
      expect(relErr(lcvValue(c.amount, c.p_8dp[c.idx]!, c.b, c.sell_fee_rate),
                    c.expected_lcv)).toBeLessThan(1e-6)
    })
  }
})

describe('utils/lmsr 边界', () => {
  it('小额交易走 log1p/expm1 不丢有效位：Δ/b=0.01 与 golden 首例覆盖', () => {
    // buyCost 单调性 sanity：同 p 下 Δ 翻倍成本大于 2 倍单价×Δ 的线性差
    const c1 = buyCost(0.5, 1, 100)
    const c2 = buyCost(0.5, 2, 100)
    expect(c2).toBeGreaterThan(2 * c1 * 0.999)
  })
  it('Δ/b > 700 渐近分支不溢出', () => {
    const c = buyCost(0.5, 80000, 100)
    expect(Number.isFinite(c)).toBe(true)
    expect(relErr(c, 80000 + 100 * Math.log(0.5))).toBeLessThan(1e-12)
  })
  it('p≈1 大额卖出不产生 -Infinity（log1p(-1) clamp）', () => {
    expect(Number.isFinite(sellProceeds(0.99999999, 100000, 100))).toBe(true)
  })
  it('非法输入返回 0', () => {
    expect(buyCost(0, 10, 100)).toBe(0)
    expect(sellProceeds(0.5, 0, 100)).toBe(0)
  })
})
```

- [ ] **Step 3: 跑测确认失败**

Run: `npm run test:unit`
Expected: FAIL —— 找不到 `../lmsr` 模块 / 找不到 `lmsr.golden.json`

- [ ] **Step 4: 写 utils/lmsr.ts**

```typescript
/**
 * LMSR 闭式公式 —— 客户端计算契约的单一实现（spec §6.1 / §6.2）。
 *
 * 只需要当前价 p 与流动性 b，不需要 q：
 *   ΔC   = b · log1p( p_i · expm1(Δ/b) )                买入成本
 *   D    = 1 + p_i · expm1(Δ/b)
 *   p'_i = p_i · exp(Δ/b) / D，p'_j = p_j / D (j ≠ i)   成交后价格
 *   卖出把 Δ 换成 −Δ，ΔC 为负，收入 = −ΔC
 *
 * 数值要点（spec §6.2，缺一不可）：
 *   1. 用 log1p/expm1 —— 小额交易 Δ/b 极小时朴素 exp(x)−1 丢 2-3 位有效数字
 *   2. Δ/b > 700 走渐近分支 ΔC → Δ + b·ln(p_i)，否则 expm1 溢出成 Infinity
 *   3. 与服务端偏差分两层（数学层 ~1e-15、线上 8dp 输入层 ~1e-7 相对），
 *      都是已知且接受的——这里算的是预览，成交以 writer 返回为准（§6.3）
 */

const ASYMPTOTIC_X = 700

/** 买入 delta 份的 LMSR 成本（未扣费；买入无费）。非法输入返回 0。 */
export function buyCost(p: number, delta: number, b: number): number {
  if (delta <= 0 || p <= 0 || b <= 0) return 0
  const x = delta / b
  if (x > ASYMPTOTIC_X) return delta + b * Math.log(p)
  return b * Math.log1p(p * Math.expm1(x))
}

/** 卖出 delta 份的 LMSR 收入（正数，未扣 sell_fee）。非法输入返回 0。 */
export function sellProceeds(p: number, delta: number, b: number): number {
  if (delta <= 0 || p <= 0 || b <= 0) return 0
  // p·expm1(−x) ∈ (−p, 0]，数学上恒 > −1；p≈1 且 delta 大时浮点可能贴到 −1，
  // clamp 防 log1p(−1) = −Infinity
  const arg = Math.max(p * Math.expm1(-delta / b), -1 + 1e-15)
  return -b * Math.log1p(arg)
}

/** 成交后的全市场价格向量。delta>0 买入 / delta<0 卖出（idx 为被交易项）。 */
export function pricesAfterTrade(
  prices: number[], idx: number, delta: number, b: number,
): number[] {
  const x = delta / b
  if (x > ASYMPTOTIC_X) {
    // 渐近：被买爆的项价格 → 1，其余 → 0
    return prices.map((_, i) => (i === idx ? 1 : 0))
  }
  const D = 1 + prices[idx]! * Math.expm1(x)
  return prices.map((p, i) => (i === idx ? (p * Math.exp(x)) / D : p / D))
}

/** MTM 账面估值 = 数量 × 瞬时价（不含滑点不扣费，spec §6.1）。 */
export function mtmValue(amount: number, p: number): number {
  return amount * p
}

/** LCV 立即清算价值 = 全卖 LMSR 收入 × (1 − sell_fee_rate)（含滑点，spec §6.1）。 */
export function lcvValue(
  amount: number, p: number, b: number, sellFeeRate: number,
): number {
  return sellProceeds(p, amount, b) * (1 - sellFeeRate)
}
```

- [ ] **Step 5: 写 golden 生成脚本并生成 fixture**

`backend/scripts/gen_lmsr_golden.py`：

```python
"""生成 thccb-frontend/src/utils/__tests__/lmsr.golden.json（spec §6.6）。

用法（backend/ 目录下）：
    python scripts/gen_lmsr_golden.py > ../thccb-frontend/src/utils/__tests__/lmsr.golden.json

expected_* 一律由服务端 q 路径（app/services/lmsr.py，float 内核）算出——
这是「客户端从 p 算 vs 服务端从 q 算」对拍的权威侧。产出两层输入：
  p_full —— 全精度价格（数学层，前端断言相对误差 < 1e-12）
  p_8dp  —— quantize_price 后的 8 位小数价格（线上层，< 1e-6）
注意 expected 是纯数学口径（无 6dp 资金量化）；服务端资金量化粒度属
spec §6.2 层 (b) 之下的已知差异，不进本 fixture。

阈值与用例的边界约束：
  * delta ≥ 1 —— 更小的 delta 会让服务端 cost 差分先发生灾难性抵消，
    数学层 1e-12 就不再是客户端的误差而是服务端的
  * 用例价格 ≥ 0.05 —— 线上层 1e-6 相对误差在超低价上会被 8dp 量化主导
"""
import json
import sys
from decimal import Decimal

from app.services.lmsr import calculate_lmsr_with_prices, quantize_price

# (b, q 向量, 被交易 idx, delta, side)
TRADE_CASES = [
    (100.0, [0.0, 0.0], 0, 1.0, "buy"),                 # Δ/b=0.01：log1p/expm1 必要性
    (100.0, [3.5, 0.0], 0, 10.0, "buy"),
    (100.0, [3.5, 0.0], 1, 10.0, "buy"),
    (100.0, [120.0, 40.0, 77.5], 2, 50.0, "buy"),
    (500.0, [1000.0, 800.0], 1, 250.0, "buy"),
    (50.0, [10.0, 5.0, 0.0, 20.0], 0, 5.0, "buy"),
    (100.0, [0.0, 0.0], 0, 80000.0, "buy"),             # Δ/b=800：前端渐近分支
    (100.0, [50.0, 30.0], 0, 25.0, "sell"),
    (100.0, [120.0, 40.0, 77.5], 0, 120.0, "sell"),     # 全卖到 0
    (500.0, [1000.0, 800.0], 0, 400.0, "sell"),
]

# (b, q, idx, amount, sell_fee_rate) —— MTM/LCV 持仓估值
HOLDING_CASES = [
    (100.0, [50.0, 30.0], 0, 50.0, "0.01"),
    (100.0, [120.0, 40.0, 77.5], 1, 40.0, "0"),
    (500.0, [1000.0, 800.0], 1, 800.0, "0.005"),
]


def trade_case(b, q, idx, delta, side):
    cost0, prices0 = calculate_lmsr_with_prices(list(q), b)
    q2 = list(q)
    q2[idx] += delta if side == "buy" else -delta
    cost1, prices1 = calculate_lmsr_with_prices(q2, b)
    return {
        "b": b, "idx": idx, "delta": delta, "side": side,
        "p_full": prices0,
        "p_8dp": [float(quantize_price(p)) for p in prices0],
        "expected_amount": (cost1 - cost0) if side == "buy" else (cost0 - cost1),
        "expected_prices_after": prices1,
    }


def holding_case(b, q, idx, amount, fee):
    cost0, prices0 = calculate_lmsr_with_prices(list(q), b)
    q2 = list(q)
    q2[idx] -= amount
    cost1, _ = calculate_lmsr_with_prices(q2, b)
    fee_f = float(Decimal(fee))
    return {
        "b": b, "idx": idx, "amount": amount, "sell_fee_rate": fee_f,
        "p_full": prices0,
        "p_8dp": [float(quantize_price(p)) for p in prices0],
        "expected_mtm": amount * prices0[idx],
        "expected_lcv": (cost0 - cost1) * (1 - fee_f),
    }


def main():
    out = {
        "trades": [trade_case(*c) for c in TRADE_CASES],
        "holdings": [holding_case(*c) for c in HOLDING_CASES],
    }
    json.dump(out, sys.stdout, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
```

Run（backend/ 下）: `python scripts/gen_lmsr_golden.py > ../thccb-frontend/src/utils/__tests__/lmsr.golden.json`

- [ ] **Step 6: 跑测确认通过**

Run（thccb-frontend/ 下）: `npm run test:unit && npm run type-check && npm run lint`
Expected: 全 PASS（golden 两层 + 边界共 30+ 用例）

- [ ] **Step 7: Commit**

```bash
git add package.json package-lock.json vitest.config.ts src/utils/lmsr.ts src/utils/__tests__/lmsr.spec.ts src/utils/__tests__/lmsr.golden.json
git add ../backend/scripts/gen_lmsr_golden.py
git commit -m "feat(front): utils/lmsr.ts 闭式公式 + vitest 基建 + 后端 golden 两层对拍（数学层 1e-12 / 线上层 1e-6）"
```

---

### Task 6: valuation 纯函数层 + stores/user.ts 重写 + types

**Files:**
- Create: `thccb-frontend/src/utils/valuation.ts`
- Modify: `thccb-frontend/src/types/user.ts`（`UserSummary` / `Holding` 重写，新增 `SummaryPosition` / `RankThreshold` / `HoldingSlim` / `MarketPriceCtx`）
- Modify: `thccb-frontend/src/stores/user.ts`（全文件重写）
- Test: `thccb-frontend/src/utils/__tests__/valuation.spec.ts`

**Interfaces:**
- Consumes: Task 3 的 summary/holdings JSON 形状；Task 5 的 `mtmValue` / `lcvValue`
- Produces（Task 7/8 依赖）:
  - `utils/valuation.ts`（纯函数，vitest 直测）：
    - `computeHoldingsValueMtm(positions: SummaryPosition[], ctx: Map<number, MarketPriceCtx>): number`
    - `computeHoldingsValueLcv(positions: SummaryPosition[], ctx: Map<number, MarketPriceCtx>, sellFeeRate: number): number`
    - `enrichHolding(h: HoldingSlim, ctx: Map<number, MarketPriceCtx>, sellFeeRate: number): Holding`
    - `rankFromThresholds(table: RankThreshold[], netWorth: number): string`
    - `applyFillToRows(rows: {amount, cost_basis 可变对象}[]的通用签名见代码, ...)`——买/卖后仓位行更新（与后端 op_buy/op_sell 语义一致）
  - `stores/user.ts` 对外（旧名尽量保留，组件迁移量最小）：
    - state: `summary: UserSummary | null`、`transactions`、`priceContext: Map<number, MarketPriceCtx>`
    - getters: `holdings: ComputedRef<Holding[]>`（**派生**，字段名与旧 API Holding 完全一致——Portfolio 表格 / TradePanel 持仓盒零模板改动）、`holdingsByMarket`、`totalCostBasis`、`holdingsValueMtm`、`holdingsValueLcv`、`netWorth`、`netWorthLcv`、`unrealizedPnl`、`unrealizedPnlLcv`、`rankTitle`、`marginRatioEstimate: ComputedRef<number | null>`
    - actions: `fetchSummary()`（顺带并行 `refreshPriceContext()`）、`fetchHoldings()`、`fetchTransactions()`、`fetchAllUserData()`、`refreshPriceContext()`、`patchMarketPrices(marketId, prices)`、`applyTradeFill(args)`、`getHoldingByOutcome(outcomeId)`（返回派生 Holding）、`clearData()`、`clearError()`
    - 删除：`totalHoldingsValue`（仅 store 内部引用，替换为 `holdingsValueMtm`）

- [ ] **Step 1: 重写 types/user.ts**

替换 `types/user.ts` 的 `UserSummary` / `Holding`（`User` / `Transaction` / `LeaderboardMode` / `LeaderboardItem` 不动），新增：

```typescript
export interface SummaryPosition {
  outcome_id: number
  market_id: number
  amount: number
  cost_basis: number
}

export interface RankThreshold {
  /** null = 兜底档；判定：命中第一个 null 或 netWorth > min_net_worth 的条目 */
  min_net_worth: number | null
  title: string
}

/** 阶段 3 新契约（spec §6.4）：只有客户端算不出来的东西。
 *  估值/净值/浮盈/rank 由 stores/user.ts 的派生 getters 本地算。 */
export interface UserSummary {
  /** 6dp 全精度——成交后本地 apply 的 cash 基线 */
  cash: number
  debt: number
  positions: SummaryPosition[]
  margin_hard_threshold: number
  margin_soft_threshold: number
  sell_fee_rate: number
  rank_thresholds: RankThreshold[]
  /** 服务端权威（LCV 口径）；本地 marginRatioEstimate 只是显示估算 */
  margin_status: 'healthy' | 'warning' | 'danger'
  liquidation_protected: boolean
  last_liquidated_at: string | null
  equipped_title?: TitleChip | null
}

/** /user/holdings 瘦身后的原始行 */
export interface HoldingSlim {
  market_id: number
  market_title: string
  outcome_id: number
  outcome_label: string
  amount: number
  cost_basis: number
}

/** 客户端派生的持仓视图——字段名与旧 API Holding 完全一致，
 *  Portfolio 表格 / TradePanel 持仓盒零模板改动。估值来自 utils/valuation.ts。 */
export interface Holding extends HoldingSlim {
  avg_price: number
  current_price: number
  /** LCV：含滑点 + 扣 sell_fee；非 TRADING 市场 = 0（"现在卖不出去"） */
  market_value: number
  /** MTM 口径浮盈 */
  unrealized_pnl: number
  /** LCV 口径浮盈；非 TRADING 市场 = -cost_basis */
  unrealized_pnl_liquidation: number
}

/** 市场定价上下文：客户端本地估值/预览的价格来源。
 *  fetchSummary 时全量重建；当前市场由 tick 帧经 patchMarketPrices 续写；
 *  非当前市场允许轻微陈旧——显示口径，权威判定在服务端（spec §6.3）。 */
export interface MarketPriceCtx {
  b: number
  status: string
  /** 升序，与 prices 同序（与 tick 帧价格向量的索引契约一致） */
  outcomeIds: number[]
  prices: number[]
}
```

- [ ] **Step 2: 写 valuation.spec.ts 失败测试**

`thccb-frontend/src/utils/__tests__/valuation.spec.ts`：

```typescript
// 估值纯函数层：HALT 语义与后端 wealth.py 镜像；applyFill 与 op_buy/op_sell 一致
import { describe, expect, it } from 'vitest'
import {
  computeHoldingsValueMtm, computeHoldingsValueLcv,
  enrichHolding, rankFromThresholds, applyFillToRows,
} from '../valuation'
import { lcvValue } from '../lmsr'
import type { MarketPriceCtx, RankThreshold, SummaryPosition } from '@/types/user'

const ctx = new Map<number, MarketPriceCtx>([
  [1, { b: 100, status: 'trading', outcomeIds: [11, 12], prices: [0.6, 0.4] }],
  [2, { b: 100, status: 'halt', outcomeIds: [21, 22], prices: [0.7, 0.3] }],
])

const positions: SummaryPosition[] = [
  { outcome_id: 11, market_id: 1, amount: 10, cost_basis: 5 },
  { outcome_id: 21, market_id: 2, amount: 20, cost_basis: 12 },
  { outcome_id: 99, market_id: 9, amount: 7, cost_basis: 3 },  // 无价格上下文
]

describe('holdings 估值口径（与 wealth.py 镜像）', () => {
  it('MTM 计入 HALT，缺上下文跳过', () => {
    expect(computeHoldingsValueMtm(positions, ctx))
      .toBeCloseTo(10 * 0.6 + 20 * 0.7, 12)
  })
  it('LCV 只计 trading 市场', () => {
    const fee = 0.01
    expect(computeHoldingsValueLcv(positions, ctx, fee))
      .toBeCloseTo(lcvValue(10, 0.6, 100, fee), 12)
  })
})

describe('enrichHolding', () => {
  const slim = { market_id: 1, market_title: 't', outcome_id: 11,
                 outcome_label: 'a', amount: 10, cost_basis: 5 }
  it('trading 市场：全字段派生', () => {
    const h = enrichHolding(slim, ctx, 0.01)
    expect(h.avg_price).toBeCloseTo(0.5, 12)
    expect(h.current_price).toBe(0.6)
    expect(h.market_value).toBeCloseTo(lcvValue(10, 0.6, 100, 0.01), 12)
    expect(h.unrealized_pnl).toBeCloseTo(10 * 0.6 - 5, 12)
    expect(h.unrealized_pnl_liquidation).toBeCloseTo(h.market_value - 5, 12)
  })
  it('HALT 市场：market_value=0、LCV 浮盈=-cost_basis、MTM 浮盈正常', () => {
    const h = enrichHolding({ ...slim, market_id: 2, outcome_id: 21,
                              amount: 20, cost_basis: 12 }, ctx, 0.01)
    expect(h.market_value).toBe(0)
    expect(h.unrealized_pnl_liquidation).toBe(-12)
    expect(h.unrealized_pnl).toBeCloseTo(20 * 0.7 - 12, 12)
  })
})

describe('rankFromThresholds（与后端 rank_title 同规则）', () => {
  const table: RankThreshold[] = [
    { min_net_worth: 30000, title: 'ZUN' },
    { min_net_worth: 300, title: '人里居民' },
    { min_net_worth: null, title: '人类灵(已爆仓)' },
  ]
  it('严格大于；等于阈值落下一档；空表返回空串', () => {
    expect(rankFromThresholds(table, 30000.01)).toBe('ZUN')
    expect(rankFromThresholds(table, 30000)).toBe('人里居民')
    expect(rankFromThresholds(table, 300)).toBe('人类灵(已爆仓)')
    expect(rankFromThresholds(table, -1)).toBe('人类灵(已爆仓)')
    expect(rankFromThresholds([], 100)).toBe('')
  })
})

describe('applyFillToRows（与后端 op_buy/op_sell 一致）', () => {
  it('buy：已有仓位累加', () => {
    const rows = [{ outcome_id: 11, amount: 10, cost_basis: 5 }]
    applyFillToRows(rows, { side: 'buy', outcomeId: 11, shares: 3, pay: 2 })
    expect(rows[0]).toMatchObject({ amount: 13, cost_basis: 7 })
  })
  it('buy：新仓位由调用方补行（函数返回 false 表示未命中）', () => {
    const rows: { outcome_id: number; amount: number; cost_basis: number }[] = []
    expect(applyFillToRows(rows, { side: 'buy', outcomeId: 11, shares: 3, pay: 2 }))
      .toBe(false)
  })
  it('sell：先按卖出比例减成本再减数量；清仓移除', () => {
    const rows = [{ outcome_id: 11, amount: 10, cost_basis: 5 }]
    applyFillToRows(rows, { side: 'sell', outcomeId: 11, shares: 4, pay: 3 })
    expect(rows[0]!.amount).toBeCloseTo(6, 12)
    expect(rows[0]!.cost_basis).toBeCloseTo(3, 12)   // 5 × (1 - 4/10)
    applyFillToRows(rows, { side: 'sell', outcomeId: 11, shares: 6, pay: 3 })
    expect(rows.length).toBe(0)
  })
})
```

- [ ] **Step 3: 跑测确认失败**

Run: `npm run test:unit`
Expected: FAIL —— 找不到 `../valuation`

- [ ] **Step 4: 写 utils/valuation.ts**

```typescript
/**
 * 持仓估值纯函数层（阶段 3）。store 只做接线，可测逻辑全在这里。
 * 口径与后端 services/wealth.py 镜像（docs/holdings-value-semantics.md）：
 *   MTM 计入 HALT（账面口径，避免临时 HALT 让账面归零）
 *   LCV 只计 TRADING（立即变现口径，HALT 持仓 market_value=0、浮盈=-cost_basis）
 * 这些是显示口径；margin_status/强平/排行榜的权威判定仍在服务端（spec §6.3）。
 */
import { lcvValue, mtmValue } from './lmsr'
import type { Holding, HoldingSlim, MarketPriceCtx, RankThreshold, SummaryPosition } from '@/types/user'

function priceOf(
  ctx: Map<number, MarketPriceCtx>, marketId: number, outcomeId: number,
): { p: number; b: number; trading: boolean } | null {
  const m = ctx.get(marketId)
  if (!m) return null
  const idx = m.outcomeIds.indexOf(outcomeId)
  if (idx < 0) return null
  return { p: m.prices[idx]!, b: m.b, trading: m.status === 'trading' }
}

export function computeHoldingsValueMtm(
  positions: SummaryPosition[], ctx: Map<number, MarketPriceCtx>,
): number {
  let total = 0
  for (const pos of positions) {
    const c = priceOf(ctx, pos.market_id, pos.outcome_id)
    if (!c) continue
    total += mtmValue(pos.amount, c.p)
  }
  return total
}

export function computeHoldingsValueLcv(
  positions: SummaryPosition[], ctx: Map<number, MarketPriceCtx>, sellFeeRate: number,
): number {
  let total = 0
  for (const pos of positions) {
    const c = priceOf(ctx, pos.market_id, pos.outcome_id)
    if (!c || !c.trading) continue
    total += lcvValue(pos.amount, c.p, c.b, sellFeeRate)
  }
  return total
}

export function enrichHolding(
  h: HoldingSlim, ctx: Map<number, MarketPriceCtx>, sellFeeRate: number,
): Holding {
  const c = priceOf(ctx, h.market_id, h.outcome_id)
  const price = c?.p ?? 0
  const mtm = c ? mtmValue(h.amount, price) : 0
  const marketValue = c && c.trading ? lcvValue(h.amount, price, c.b, sellFeeRate) : 0
  return {
    ...h,
    avg_price: h.amount > 0 ? h.cost_basis / h.amount : 0,
    current_price: price,
    market_value: marketValue,
    unrealized_pnl: mtm - h.cost_basis,
    unrealized_pnl_liquidation: c && c.trading ? marketValue - h.cost_basis : -h.cost_basis,
  }
}

export function rankFromThresholds(table: RankThreshold[], netWorth: number): string {
  for (const t of table) {
    if (t.min_net_worth === null || netWorth > t.min_net_worth) return t.title
  }
  return table.length ? table[table.length - 1]!.title : ''
}

export interface FillArgs {
  side: 'buy' | 'sell'
  outcomeId: number
  shares: number
  /** buy=实付现金；sell=到手净额。调用方由 |旧cash − new_cash| 推导（6dp 精确） */
  pay: number
}

/**
 * 把一笔成交 apply 到仓位行数组（就地修改）。逻辑与后端 op_buy/op_sell 一致：
 * buy 累加；sell 先按卖出比例减 cost_basis 再减 amount，清仓移除整行。
 * 返回 false 表示 buy 未命中已有行（调用方负责 push 新行）。
 */
export function applyFillToRows(
  rows: { outcome_id: number; amount: number; cost_basis: number }[],
  args: FillArgs,
): boolean {
  const i = rows.findIndex(r => r.outcome_id === args.outcomeId)
  if (args.side === 'buy') {
    if (i < 0) return false
    rows[i]!.amount += args.shares
    rows[i]!.cost_basis += args.pay
    return true
  }
  if (i < 0) return true   // sell 无仓位：服务端已拒，本地无事可做
  const row = rows[i]!
  const ratio = args.shares / row.amount
  row.cost_basis -= row.cost_basis * ratio
  row.amount -= args.shares
  if (row.amount <= 1e-9) rows.splice(i, 1)
  return true
}
```

- [ ] **Step 5: 重写 stores/user.ts**

全文件替换：

```typescript
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type {
  Holding, HoldingSlim, MarketPriceCtx, Transaction, UserSummary,
} from '@/types/api'
import { userApi } from '@/api/user'
import { marketApi } from '@/api/market'
import { useAuthStore } from '@/stores/auth'
import {
  applyFillToRows, computeHoldingsValueLcv, computeHoldingsValueMtm,
  enrichHolding, rankFromThresholds,
} from '@/utils/valuation'

export const useUserStore = defineStore('user', () => {
  const summary = ref<UserSummary | null>(null)
  const holdingsRaw = ref<HoldingSlim[]>([])
  const transactions = ref<Transaction[]>([])
  // 市场定价上下文：本地估值/预览的价格来源。fetchSummary 时全量重建，
  // 当前市场由 tick 帧经 patchMarketPrices 续写（TradingView 接线）。
  const priceContext = ref<Map<number, MarketPriceCtx>>(new Map())
  const loading = ref(false)
  const error = ref<string | null>(null)

  // ── 派生估值（原 /user/summary 服务端字段，阶段 3 起全部本地算） ──
  const totalCostBasis = computed(() =>
    (summary.value?.positions ?? []).reduce((s, p) => s + p.cost_basis, 0))
  const holdingsValueMtm = computed(() =>
    computeHoldingsValueMtm(summary.value?.positions ?? [], priceContext.value))
  const holdingsValueLcv = computed(() =>
    computeHoldingsValueLcv(summary.value?.positions ?? [], priceContext.value,
                            summary.value?.sell_fee_rate ?? 0))
  const netWorth = computed(() =>
    summary.value ? summary.value.cash - summary.value.debt + holdingsValueMtm.value : 0)
  const netWorthLcv = computed(() =>
    summary.value ? summary.value.cash - summary.value.debt + holdingsValueLcv.value : 0)
  const unrealizedPnl = computed(() => holdingsValueMtm.value - totalCostBasis.value)
  const unrealizedPnlLcv = computed(() => holdingsValueLcv.value - totalCostBasis.value)
  const rankTitle = computed(() =>
    rankFromThresholds(summary.value?.rank_thresholds ?? [], netWorth.value))
  // 显示用估算；权威 margin_status 仍来自 summary（服务端 LCV 口径）
  const marginRatioEstimate = computed<number | null>(() => {
    const s = summary.value
    if (!s || s.debt <= 0) return null
    return netWorthLcv.value / s.debt
  })

  // 派生持仓视图——字段名与旧 API Holding 一致，表格/持仓盒模板零改动
  const holdings = computed<Holding[]>(() => {
    const fee = summary.value?.sell_fee_rate ?? 0
    return holdingsRaw.value.map(h => enrichHolding(h, priceContext.value, fee))
  })

  const holdingsByMarket = computed(() => {
    const map = new Map<number, Holding[]>()
    holdings.value.forEach(h => {
      if (!map.has(h.market_id)) map.set(h.market_id, [])
      map.get(h.market_id)!.push(h)
    })
    return map
  })

  // ── priceContext 维护 ──
  const refreshPriceContext = async () => {
    const markets = await marketApi.getMarkets({ include_halt: true })
    const next = new Map<number, MarketPriceCtx>()
    for (const m of markets) {
      const sorted = [...m.outcomes].sort((a, b) => a.id - b.id)
      next.set(m.id, {
        b: m.liquidity_b,
        status: m.status,
        outcomeIds: sorted.map(o => o.id),
        prices: sorted.map(o => o.current_price),
      })
    }
    priceContext.value = next
  }

  /** tick 帧续写当前市场价格（prices 按 outcome.id 升序，与帧契约一致） */
  const patchMarketPrices = (marketId: number, prices: number[]) => {
    const ctx = priceContext.value.get(marketId)
    if (!ctx || ctx.prices.length !== prices.length) return
    ctx.prices = [...prices]
    priceContext.value = new Map(priceContext.value)  // 换引用触发 computed
  }

  // ── fetch actions（manageLoading 语义与旧版一致） ──
  const fetchSummary = async (manageLoading = true) => {
    const authStore = useAuthStore()
    if (!authStore.isAuthenticated) return null
    if (manageLoading) { loading.value = true; error.value = null }
    try {
      // priceContext 与 summary 并行刷新；价格上下文失败不阻断 summary
      const [s] = await Promise.all([
        userApi.getSummary(),
        refreshPriceContext().catch(err =>
          console.error('刷新价格上下文失败:', err)),
      ])
      summary.value = s
      return s
    } catch (err: any) {
      error.value = err.message || '获取资产概览失败'
      console.error('获取资产概览失败:', err)
      return null
    } finally {
      if (manageLoading) loading.value = false
    }
  }

  const fetchHoldings = async (manageLoading = true) => {
    const authStore = useAuthStore()
    if (!authStore.isAuthenticated) return []
    if (manageLoading) { loading.value = true; error.value = null }
    try {
      holdingsRaw.value = await userApi.getHoldings()
      return holdingsRaw.value
    } catch (err: any) {
      error.value = err.message || '获取持仓明细失败'
      console.error('获取持仓明细失败:', err)
      return []
    } finally {
      if (manageLoading) loading.value = false
    }
  }

  const fetchTransactions = async (limit = 100, manageLoading = true) => {
    const authStore = useAuthStore()
    if (!authStore.isAuthenticated) return []
    if (manageLoading) { loading.value = true; error.value = null }
    try {
      transactions.value = await userApi.getTransactions(limit)
      return transactions.value
    } catch (err: any) {
      error.value = err.message || '获取交易历史失败'
      console.error('获取交易历史失败:', err)
      return []
    } finally {
      if (manageLoading) loading.value = false
    }
  }

  const fetchAllUserData = async () => {
    const authStore = useAuthStore()
    if (!authStore.isAuthenticated) return { success: false, error: '用户未认证' }
    loading.value = true
    error.value = null
    try {
      await Promise.all([
        fetchSummary(false),
        fetchHoldings(false),
        fetchTransactions(100, false),
      ])
      return { success: true }
    } catch (err: any) {
      error.value = err.message || '获取用户数据失败'
      console.error('获取用户数据失败:', err)
      return { success: false, error: error.value }
    } finally {
      loading.value = false
    }
  }

  // ── 成交后本地 apply（spec §6.4：成交后不再调 summary/holdings） ──
  const applyTradeFill = (args: {
    side: 'buy' | 'sell'
    outcomeId: number
    marketId: number
    shares: number
    /** buy=实付 / sell=到手净额；调用方用 |旧 cash − new_cash| 推导（6dp 精确） */
    pay: number
    newCash: number
    outcomeLabel?: string
    marketTitle?: string
  }) => {
    const s = summary.value
    if (!s) return
    s.cash = args.newCash
    const fill = { side: args.side, outcomeId: args.outcomeId,
                   shares: args.shares, pay: args.pay }
    if (!applyFillToRows(s.positions, fill)) {
      s.positions.push({ outcome_id: args.outcomeId, market_id: args.marketId,
                         amount: args.shares, cost_basis: args.pay })
    }
    if (!applyFillToRows(holdingsRaw.value, fill)
        && args.outcomeLabel !== undefined && args.marketTitle !== undefined) {
      holdingsRaw.value.push({
        market_id: args.marketId, market_title: args.marketTitle,
        outcome_id: args.outcomeId, outcome_label: args.outcomeLabel,
        amount: args.shares, cost_basis: args.pay,
      })
    }
  }

  const getHoldingByOutcome = (outcomeId: number) =>
    holdings.value.find(h => h.outcome_id === outcomeId)

  const getHoldingsByMarket = (marketId: number) =>
    holdings.value.filter(h => h.market_id === marketId)

  const clearData = () => {
    summary.value = null
    holdingsRaw.value = []
    transactions.value = []
    priceContext.value = new Map()
    error.value = null
  }

  const clearError = () => { error.value = null }

  return {
    summary, holdings, transactions, loading, error, priceContext,

    totalCostBasis, holdingsValueMtm, holdingsValueLcv,
    netWorth, netWorthLcv, unrealizedPnl, unrealizedPnlLcv,
    rankTitle, marginRatioEstimate, holdingsByMarket,

    fetchSummary, fetchHoldings, fetchTransactions, fetchAllUserData,
    refreshPriceContext, patchMarketPrices, applyTradeFill,
    getHoldingByOutcome, getHoldingsByMarket, clearData, clearError,
  }
})
```

- [ ] **Step 6: 跑测；type-check 摸底**

Run: `npm run test:unit`
Expected: valuation + lmsr 测试全 PASS

Run: `npm run type-check`
Expected: **组件侧报错**（Home/Portfolio/TradePanel/MarginStatusCard/MarginCallBanner/TradingView 还在读已删字段）——这是 Task 7/8 的工作清单，把报错文件列表记入执行日志；本 task 只要求 stores/types/utils 自身无错

- [ ] **Step 7: Commit**

```bash
git add src/types/user.ts src/utils/valuation.ts src/utils/__tests__/valuation.spec.ts src/stores/user.ts
git commit -m "feat(front): 估值下放客户端——valuation 纯函数层 + user store 重写（priceContext + 派生 getters + 本地 apply）"
```

---

### Task 7: 组件迁移到派生 getters

**Files:**
- Modify: `thccb-frontend/src/pages/home/Home.vue:57-118`
- Modify: `thccb-frontend/src/pages/user/Portfolio.vue:206-260`
- Modify: `thccb-frontend/src/components/user/MarginStatusCard.vue:13-27`
- Modify: `thccb-frontend/src/components/market/MarginCallBanner.vue:8`
- Modify: `thccb-frontend/src/components/market/TradePanel.vue:151-223`
- Verify only: `thccb-frontend/src/components/layout/AppHeader.vue`、`src/pages/danmuku/DanmukuExchange.vue`（只读 `summary.debt` / `equipped_title` / `summary.cash`——字段保留，type-check 过即可）

**Interfaces:**
- Consumes: Task 6 getters。所有替换是「同语义换数据源」：`summary.X` → `userStore.Y`，模板逻辑不动

- [ ] **Step 1: Home.vue**

script 区（`:57` 与 `:66-68`）：

```typescript
const pnl = computed(() => userStore.unrealizedPnl)
```

```typescript
const pnlPercent = computed(() => {
  const cost = userStore.totalCostBasis
  if (cost <= 0) return null
  return (pnl.value / cost) * 100
})
```

模板区逐处替换：
- `:90` `userStore.summary!.rank` → `userStore.rankTitle`
- `:97` `userStore.summary!.total_cost_basis.toFixed(2)` → `userStore.totalCostBasis.toFixed(2)`
- `:110` `userStore.summary!.holdings_value.toFixed(2)` → `userStore.holdingsValueMtm.toFixed(2)`
- `:118` `userStore.summary!.net_worth.toFixed(2)` → `userStore.netWorth.toFixed(2)`
- `:106` cash 与 `:112-114` debt 不动（字段保留）

- [ ] **Step 2: Portfolio.vue**

模板区（`:206-260`）逐处替换：
- `:214` `userStore.summary.total_cost_basis.toFixed(2)` → `userStore.totalCostBasis.toFixed(2)`
- `:218` `userStore.summary.holdings_value.toFixed(2)` → `userStore.holdingsValueMtm.toFixed(2)`
- `:222-223` `userStore.summary.unrealized_pnl` 两处 → `userStore.unrealizedPnl`
- `:226-230` `unrealized_pnl_liquidation` 三处 → `userStore.unrealizedPnlLcv`
- `:245` `userStore.summary.net_worth.toFixed(2)` → `userStore.netWorth.toFixed(2)`
- `:260` `userStore.summary.rank` → `userStore.rankTitle`
- 持仓表格与 `holdingsByMarketArray` **零改动**（`userStore.holdingsByMarket` / 行字段名全部保留，估值已在 getter 内派生）；`loadData` 里的 `fetchMarkets` 保留（marketById 状态 chip 还在用）

- [ ] **Step 3: MarginStatusCard.vue**

`:13` `const ratio = computed(() => summary.value?.margin_ratio ?? null)` → `const ratio = computed(() => userStore.marginRatioEstimate)`
`:23` `netWorth` → `computed(() => userStore.netWorth)`
`:24` `netWorthLcv` → `computed(() => userStore.netWorthLcv)`
（`margin_status` / 阈值 / debt / `liquidation_protected` / `last_liquidated_at` 直读 summary 不动——它们仍在新契约里）

- [ ] **Step 4: MarginCallBanner.vue**

`:8` `const ratio = computed(() => userStore.summary?.margin_ratio)` → `const ratio = computed(() => userStore.marginRatioEstimate)`

- [ ] **Step 5: TradePanel.vue**

script 区（`:151-161`）：

```typescript
const summaryPnlDirection = computed<'up' | 'down' | 'flat'>(() => {
  const v = userStore.unrealizedPnl
  if (v > 0) return 'up'
  if (v < 0) return 'down'
  return 'flat'
})

const summaryPnlSign = computed(() => {
  const v = userStore.unrealizedPnl
  return v > 0 ? '+' : v < 0 ? '−' : ''
})
```

模板区（asset bar `:202-225`）：
- `:209` `userStore.summary.holdings_value.toFixed(2)` → `userStore.holdingsValueMtm.toFixed(2)`
- `:214` `Math.abs(userStore.summary.unrealized_pnl).toFixed(2)` → `Math.abs(userStore.unrealizedPnl).toFixed(2)`
- `:223` `userStore.summary.net_worth.toFixed(2)` → `userStore.netWorth.toFixed(2)`
- cash / debt / `buyDisabledByDanger`（`margin_status`）不动；持仓盒（`props.userHolding`）不动——Task 8 传入的派生 Holding 字段名一致

- [ ] **Step 6: type-check 收敛确认**

Run: `npm run type-check`
Expected: 只剩 `TradingView.vue` / `stores/market.ts` 的报错（Task 8 范围：quote 调用与旧 Holding 引用）；本任务范围文件零报错。`grep -rn "summary.\(holdings_value\|net_worth\|unrealized_pnl\|total_cost_basis\|margin_ratio\|rank\b\)" src/` 除 TradingView 外零命中

- [ ] **Step 7: Commit**

```bash
git add src/pages/home/Home.vue src/pages/user/Portfolio.vue src/components/user/MarginStatusCard.vue src/components/market/MarginCallBanner.vue src/components/market/TradePanel.vue
git commit -m "refactor(front): 组件迁移到派生 getters——净值/浮盈/rank/保证金率显示全部本地算"
```

---

### Task 8: TradingView 本地报价 + 成交本地 apply + 轮询删除

**Files:**
- Modify: `thccb-frontend/src/pages/market/TradingView.vue`（按符号定位——阶段 2 已改过此文件）
- Modify: `thccb-frontend/src/stores/market.ts`（删 `getQuote`；`buyShares`/`sellShares` 删成交后 refetch）
- Modify: `thccb-frontend/src/api/market.ts:65-67`（删 `quote` 方法）
- Modify: `thccb-frontend/src/types/trade.ts`（`QuoteResponse.after_prices` 改可选）

**Interfaces:**
- Consumes: Task 5 `buyCost`/`sellProceeds`、Task 6 `applyTradeFill`/`patchMarketPrices`、Task 2 的 6dp `new_cash`
- Produces: `/market/quote` 前端零调用（端点保留给 bot，spec § 6.5）；成交后零 REST 往返（spec § 6.7 表格前两行）

- [ ] **Step 1: types/trade.ts 调整**

`QuoteResponse.after_prices: Outcome[]` → `after_prices?: Outcome[]`，并在字段上方加注释：

```typescript
  // 阶段 3 起 QuoteResponse 由前端本地构造（utils/lmsr 预览），不再来自
  // /market/quote；after_prices 本地不算，置空。后端端点保留给 bot。
```

- [ ] **Step 2: stores/market.ts 删 getQuote 与成交后 refetch**

- 删除 `getQuote` action（`:175-187` 一带，按符号定位）及 return 对象里的 `getQuote`
- `buyShares` / `sellShares` 内删掉成交成功后的：

```typescript
      // 如果当前有市场详情，重新获取以更新价格
      if (currentMarket.value) {
        await fetchMarketDetail(currentMarket.value.id)
      }
```

（价格由 tick 帧续写、用户资产由 `applyTradeFill` 本地 apply——spec § 6.7"成交后 4 个 REST 往返"删除的一半在这，另一半在 TradingView 的 `loadMarketData/loadUserData`。）

- [ ] **Step 3: api/market.ts 删 quote 方法**

删除 `quote(request: QuoteRequest)` 方法与 `QuoteRequest` import（`QuoteResponse` import 若仍被别处引用则保留，grep 确认）。

- [ ] **Step 4: TradingView.vue 改造（按符号定位）**

**删除**：
- `const quoteResult = ref<QuoteResponse | null>(null)`
- `getQuote` 异步函数、`quoteTimer` / `debouncedGetQuote`、`watch([tradeType, selectedOutcomeId, shares], ...)`（防抖报价触发器）与 `onBeforeUnmount` 里的 `quoteTimer` 清理
- `maybeSanityRefresh` / `SANITY_REFRESH_INTERVAL_MS` / `lastSanityRefreshAt` 及 tick/trade watcher 里对 `maybeSanityRefresh()` 的调用（spec § 6.7：60s sanity refresh 删，tick 帧取代）
- `executeTrade` 成交成功后的 `await Promise.all([loadMarketData(), loadUserData()])`

**新增** `quoteResult` 同步 computed（替代 ref，TradePanel props 形状不变）：

```typescript
// 本地报价预览（spec §6.1/§6.3）：闭式公式 + 当前价 + b + sell_fee_rate。
// 永远是预览，成交以 writer 返回为准；滑点保护仍由服务端执行。
const quoteResult = computed<QuoteResponse | null>(() => {
  const oid = selectedOutcomeId.value
  const mkt = marketStore.currentMarket
  const n = shares.value
  if (!oid || !mkt || n <= 0) return null
  const outcome = mkt.outcomes.find(o => o.id === oid)
  if (!outcome) return null
  // 优先 SSE 实时价（tick 帧续写），回退 REST 详情价
  const p = realtime.pricesByOutcome.value.get(oid) ?? outcome.current_price
  if (p <= 0) return null
  const b = mkt.liquidity_b
  if (tradeType.value === 'buy') {
    const gross = buyCost(p, n, b)
    if (gross <= 0) return null
    return { outcome_id: oid, side: 'buy', shares: n,
             avg_price: gross / n, gross, fee: 0, net: gross }
  }
  const gross = sellProceeds(p, n, b)
  if (gross <= 0) return null
  const feeRate = userStore.summary?.sell_fee_rate ?? 0
  const fee = gross * feeRate
  return { outcome_id: oid, side: 'sell', shares: n,
           avg_price: gross / n, gross, fee, net: gross - fee }
})
```

（顶部加 `import { buyCost, sellProceeds } from '@/utils/lmsr'`；删掉不再用的 `QuoteResponse` 相关旧引用后按 type-check 清 import。）

**改** `executeTrade`（整函数替换）：

```typescript
// 执行交易：成交后本地 apply（spec §6.4），不再 refetch 市场/用户数据。
// pay 从现金差推导——new_cash 是 6dp 全精度，比 2dp 的 cost 字段精确。
const executeTrade = async () => {
  if (!selectedOutcomeId.value || shares.value <= 0) return

  const acceptAnySlippage = maxSlippageBps.value === -1
  const effectiveBps = acceptAnySlippage ? undefined : maxSlippageBps.value
  const prevCash = userStore.summary?.cash ?? null

  try {
    const result = tradeType.value === 'buy'
      ? await marketStore.buyShares(selectedOutcomeId.value, shares.value, effectiveBps, acceptAnySlippage)
      : await marketStore.sellShares(selectedOutcomeId.value, shares.value, effectiveBps, acceptAnySlippage)

    if (!result.success) {
      message.error(result.error || '交易失败，请重试')
      return
    }
    message.success(`${tradeType.value === 'buy' ? '买入' : '卖出'}成功`)

    if (result.data && prevCash !== null && marketStore.currentMarket) {
      userStore.applyTradeFill({
        side: tradeType.value,
        outcomeId: selectedOutcomeId.value,
        marketId: marketStore.currentMarket.id,
        shares: result.data.shares,
        pay: Math.abs(prevCash - result.data.new_cash),
        newCash: result.data.new_cash,
        outcomeLabel: selectedOutcome.value?.label,
        marketTitle: marketStore.currentMarket.title,
      })
    }

    // 重置表单（quoteResult 是 computed，自动跟随）
    shares.value = 1
  } catch (err: any) {
    message.error(err?.message || '交易失败，请重试')
  }
}
```

**接线两处 watcher**（阶段 2 已有的 watcher 内各加一行）：
- tick 帧 watcher（阶段 2 改造后的 `realtime.latestTick` / 等价物）：帧 apply 后加 `userStore.patchMarketPrices(marketId.value, <帧的 prices 数组>)`——保持本地估值的当前市场价格新鲜
- `watch(realtime.gapToken, ...)`：在既有 reconcile 逻辑（refetch trades）后加 `userStore.fetchSummary().catch(() => {})`（spec § 6.4 调用时机：登录 / 手动刷新 / gap reconcile）

**保留**：`loadUserData()`（onMounted 初始化，不是轮询）、`scheduleRealtimeRefresh`（market_status 变化重拉 detail）、首页三处轮询（不在本文件）。

- [ ] **Step 5: 验证**

Run: `npm run type-check && npm run lint && npm run test:unit`
Expected: 全 PASS，全仓 `grep -rn "market/quote\|getQuote" src/` 零命中（注释除外）

- [ ] **Step 6: Commit**

```bash
git add src/pages/market/TradingView.vue src/stores/market.ts src/api/market.ts src/types/trade.ts
git commit -m "feat(front): 本地报价预览 + 成交本地 apply——删 quote 调用/成交后 4 个 REST 往返/60s sanity 轮询"
```

---

### Task 9: 收尾——双侧全量验证 + 浏览器实测

**Files:** 无新改动（只验证；发现问题回对应 task 修）

- [ ] **Step 1: 后端全量**

Run（backend/）: `python -m py_compile $(find app -name '*.py') && python -c "import app.main" && python -m pytest -q`
Expected: 全绿（约 12s，独立测试 DB；已知 1 个过期 fail 按记忆 `feedback_pytest_workflow` 处理并在日志注明）

- [ ] **Step 2: 前端全量**

Run（thccb-frontend/）: `npm run type-check && npm run lint && npm run test:unit && npm run build`
Expected: 全 PASS（本阶段动了依赖，build 必跑）

- [ ] **Step 3: quant 全量**

Run（quant/）: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 4: 浏览器实测（CLAUDE.md：UI 改动必须实测；起不来就在日志写「未实测 UI」，不得谎称通过）**

后端起 dev 环境 + 前端 `npm run dev`，逐项核对：

| # | 场景 | 断言 |
|---|---|---|
| 1 | 登录 → 首页 | 浮盈 hero 的 浮盈/成本/持仓/净值/称号 数字合理（与改造前同一账号数量级一致），无 NaN |
| 2 | Portfolio | 资产 6 格 + 保证金卡 + 持仓表格（均价/卖出均价/成本/市值/浮盈亏）全有值；HALT 市场持仓市值=0、浮盈 tooltip 双口径 |
| 3 | TradingView 买入 | 改份额 → 报价即时更新（无网络请求，DevTools Network 里无 /quote）；成交 → 顶部资产条 cash/持仓/浮盈立即变化，**无** summary/holdings/detail/trades 四个请求 |
| 4 | TradingView 卖出 + 一键平仓 | 卖出后持仓盒数字正确；清仓后持仓盒消失、卖出按钮禁用 |
| 5 | 滑点档位 | 预估滑点显示、超档位红字警告仍工作（本地 quote 口径） |
| 6 | 有债账号 | MarginStatusCard 比率≈服务端口径（旧版数字对照允许 <1% 偏差）、margin_status 徽章正确；MarginCallBanner 显隐正确 |
| 7 | 未登录 | TradingView 可浏览；买入报价预览可用（不依赖 summary，fee=0）；卖出不可用（无持仓，既有行为）；无控制台报错 |
| 8 | 断线重连 | 断网 10s 恢复 → gap reconcile 触发一次 summary 拉取（Network 可见），数字自愈 |
| 9 | 移动端宽度 | 交易面板 / 资产条 / Portfolio 网格排版无溢出 |
| 10 | 双 tab 成交联动 | tab A 成交，tab B 的价格/图表随 tick 更新；tab B 自己的资产数字在下次 summary 拉取前允许陈旧（预期行为） |

- [ ] **Step 5: 结束语（CLAUDE.md 要求）**

在执行日志写明：改了什么 / 分支 `perf/2026-08-21-single-writer` / 双侧验证结果与实测表 / 未决风险（阶段 5 携带项：quote 端点与 `_QUOTE_CACHE` 未删、`legacy_trade_events` 未关）。

---

## 附：spec § 6 覆盖对照（Self-Review 用）

| spec 条目 | 落点 |
|---|---|
| § 6.1 闭式公式 | Task 5 `utils/lmsr.ts` |
| § 6.2 数值细节 1/2/3 | Task 5 实现 + 边界测试 + golden 两层容差 |
| § 6.3 权威边界（预览/滑点设施沿用/margin 权威在服务端） | Task 8 quoteResult 注释与实现；Task 3 margin_status；Task 6 marginRatioEstimate 仅显示 |
| § 6.4 summary 新契约 + 删除字段 + rank_thresholds | Task 1 + Task 3 |
| § 6.4 holdings 瘦身 | Task 3 |
| § 6.4 调用时机（登录/手动刷新/gap reconcile，成交后不调） | Task 8（executeTrade 本地 apply + gapToken 接线） |
| § 6.4 配套修正 1（new_cash 6dp Money） | Task 2 |
| § 6.4 配套修正 2（adjust-cash 陈旧已知可接受） | 无代码——语义随 summary 降频自然成立，文档见 Task 4 Step 5 |
| § 6.4 wealth.py 保留 + holdings-value-semantics.md 更新 | Task 4 |
| § 6.5 quote 保留但前端不调（缓存删除在阶段 5） | Task 8（后端零改动） |
| § 6.6 utils/lmsr.ts + golden 两层 | Task 5 |
| § 6.6 受影响文件清单 | Task 6/7/8 覆盖其列出的全部 8 处 + Home.vue（spec 清单遗漏，grep 补全） |
| § 6.7 轮询处置表 | Task 8（删两处）+ Global Constraints（保留三处 + gapToken） |
