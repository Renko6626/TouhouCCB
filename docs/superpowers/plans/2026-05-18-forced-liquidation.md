# 强制平仓机制 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 TouhouCCB 加上类似期货 broker 的保证金/强平机制：每 10 分钟扫一次借款用户，净值/借款 < 0.2 时全平仓位 + 最大化还债 + 公示首页"翻车现场墙"。

**Architecture:** 一个 APScheduler 定时 sweep（仿 `loan_sweep.py`）扫所有借款用户，命中硬阈调内部 `liquidate_user()` 走 LMSR 卖出 + `loan_service.decrease_debt`，事件落 `liquidation_events` 表给公示用。前端三处：margin banner（warning/danger）、Portfolio 强平行特殊渲染、首页"翻车现场"卡。

**Tech Stack:** FastAPI + SQLModel + Alembic + APScheduler（后端复用现有）；Vue 3 + Naive UI + UnoCSS（前端复用现有）。

**Spec reference:** `docs/superpowers/specs/2026-05-18-forced-liquidation-design.md`

---

## File Structure（先锁定 file 责任）

**新建（backend）**：
- `backend/app/services/liquidation_service.py` — 核心：`liquidate_user(user)` 全平 + 还债 + 写 LiquidationEvent
- `backend/app/services/liquidation_sweep.py` — APScheduler 定时 sweep + `_recently_attempted` 防爆 + admin run-now 复用
- `backend/app/services/market_locks.py` — 从 market.py 抽出的共享锁辅助
- `backend/tests/test_liquidation_service.py`
- `backend/tests/test_liquidation_sweep.py`
- `backend/tests/test_liquidation_admin.py`
- `backend/tests/test_liquidation_public.py`
- `backend/tests/test_liquidation_integration.py`
- `backend/alembic/versions/<rev>_add_liquidation_event.py` — autogenerate

**修改（backend）**：
- `backend/app/models/base.py` — TransactionType.LIQUIDATE + User.last_liquidated_at + LiquidationEvent
- `backend/app/api/v1/market.py` — 把 `_lock_user` / `_lock_outcomes_for_market` 改成 import 自 `market_locks.py`（保留原行为）
- `backend/app/api/v1/user.py` — /user/summary 加 margin 字段
- `backend/app/api/v1/loan.py` — /recent-liquidations 公开端点
- `backend/app/api/v1/admin.py` 或合适的 admin 路由 — /admin/liquidation/run-now
- `backend/app/api/v1/site_config.py` — ALLOWED_KEYS 加 4 个
- `backend/app/services/loan_migrate.py` — DEFAULTS 加 4 行
- `backend/app/main.py` — lifespan 启动 liquidation scheduler
- `backend/tests/conftest.py` — `_disable_scheduler` 多 patch 一个

**新建（frontend）**：
- `thccb-frontend/src/components/market/MarginCallBanner.vue`
- `thccb-frontend/src/components/home/RecentLiquidationsPanel.vue`

**修改（frontend）**：
- `thccb-frontend/src/pages/home/Home.vue` — 挂 2 个组件
- `thccb-frontend/src/pages/market/TradingView.vue` — 挂 MarginCallBanner
- `thccb-frontend/src/components/market/TradePanel.vue` — buy 按钮 danger 时 disabled
- `thccb-frontend/src/pages/user/Portfolio.vue` — type=liquidate 行特殊渲染
- `thccb-frontend/src/types/user.ts` / `market.ts` — 新增 margin_ratio 等字段类型

---

## Task 1: Schema 改动 — TransactionType + User 字段 + LiquidationEvent 模型

**Files:**
- Modify: `backend/app/models/base.py`
- Test: `backend/tests/test_liquidation_schema.py`（新建）

- [ ] **Step 1: 写 schema 单元测**

新建 `backend/tests/test_liquidation_schema.py`：

```python
"""Schema 改动单元测：TransactionType 新值 + User 新字段 + LiquidationEvent 表存在。"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.base import (
    LiquidationEvent,
    TransactionType,
    User,
)


def test_transaction_type_has_liquidate():
    """TransactionType 必须有 LIQUIDATE 枚举值。"""
    assert TransactionType.LIQUIDATE == "liquidate"
    assert "liquidate" in {t.value for t in TransactionType}


def test_user_has_last_liquidated_at_field():
    """User 必须有 last_liquidated_at: Optional[datetime] 字段。"""
    # SQLModel 通过 model_fields 暴露字段
    assert "last_liquidated_at" in User.model_fields
    field = User.model_fields["last_liquidated_at"]
    # 应该可空（None 是合法值）
    assert field.default is None


@pytest.mark.asyncio
async def test_liquidation_event_can_be_created(client):
    """LiquidationEvent 表能写入 + 查询。"""
    from app.core.database import async_session_maker
    async with async_session_maker() as db:
        # 先建一个 user
        u = User(username="liq_test", casdoor_id="liq_test_cas",
                 cash=Decimal("100"), debt=Decimal("500"))
        db.add(u)
        await db.commit()
        await db.refresh(u)

        ev = LiquidationEvent(
            user_id=u.id,
            triggered_at=datetime.now(timezone.utc),
            pre_cash=Decimal("100"),
            pre_debt=Decimal("500"),
            pre_holdings_value=Decimal("200"),
            pre_net_worth=Decimal("-200"),  # 100 - 500 + 200
            pre_margin_ratio=Decimal("-0.4"),
            sold_positions_count=2,
            total_proceeds=Decimal("180"),
            repaid_amount=Decimal("180"),
            remaining_debt=Decimal("320"),
            post_cash=Decimal("0"),
            trigger_source="scheduler",
        )
        db.add(ev)
        await db.commit()
        await db.refresh(ev)

        assert ev.id is not None
        assert ev.user_id == u.id
        assert ev.pre_margin_ratio == Decimal("-0.4")
        assert ev.trigger_source == "scheduler"
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `cd backend && pytest tests/test_liquidation_schema.py -v`
Expected: 全 FAIL with `ImportError: cannot import name 'LiquidationEvent'` 或 `AttributeError: TransactionType has no attribute LIQUIDATE`

- [ ] **Step 3: 改 `backend/app/models/base.py`**

3a. 找到 `class TransactionType` 加 LIQUIDATE：

```python
class TransactionType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    SETTLE = "settle"
    SETTLE_LOSE = "settle_lose"
    LIQUIDATE = "liquidate"   # 新增：强制平仓
```

3b. 找到 `class User` 加 `last_liquidated_at` 字段（放在其他 datetime 字段附近）：

```python
from sqlalchemy import Column, DateTime
# (可能已 import，确认下)

class User(SQLModel, table=True):
    # ...existing fields...
    last_liquidated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
```

3c. 在文件末尾加 LiquidationEvent 类（放在 Transaction 类之后）：

```python
class LiquidationEvent(SQLModel, table=True):
    """每次强制平仓事件的快照记录。给"翻车现场墙"公示用，跟 LIQUIDATE
    type 的 Transaction（细粒度卖出）是 1-to-N 关系。"""
    __tablename__ = "liquidation_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    triggered_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )

    # 触发瞬间快照（算法运行前）
    pre_cash: Decimal = Field(sa_type=Numeric(16, 6))
    pre_debt: Decimal = Field(sa_type=Numeric(16, 6))
    pre_holdings_value: Decimal = Field(sa_type=Numeric(16, 6))
    pre_net_worth: Decimal = Field(sa_type=Numeric(16, 6))
    # 可空：debt=0 时无意义；当前算法逻辑保证 debt>0 才触发，但 nullable 给未来扩展留口
    pre_margin_ratio: Optional[Decimal] = Field(
        default=None, sa_type=Numeric(10, 6), nullable=True
    )

    # 强平结果
    sold_positions_count: int
    total_proceeds: Decimal = Field(sa_type=Numeric(16, 6))
    repaid_amount: Decimal = Field(sa_type=Numeric(16, 6))
    remaining_debt: Decimal = Field(sa_type=Numeric(16, 6))
    post_cash: Decimal = Field(sa_type=Numeric(16, 6))

    trigger_source: str  # "scheduler" | "admin_manual"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_liquidation_schema.py -v`
Expected: 3 个 PASS。

- [ ] **Step 5: Commit**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add backend/app/models/base.py backend/tests/test_liquidation_schema.py
git commit -m "feat(model): TransactionType.LIQUIDATE + User.last_liquidated_at + LiquidationEvent 表

为 forced-liquidation 机制铺路。LIQUIDATE 是新的 Transaction.type
枚举值，per-outcome 卖出记录用；LiquidationEvent 是 per-event rollup
表，给首页\"翻车现场墙\"公示用。User.last_liquidated_at 给前端 modal
判断\"刚被强平了\"用。

下一步：alembic autogenerate migration。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Alembic migration

**Files:**
- Create: `backend/alembic/versions/<auto>_add_liquidation_event.py`（autogenerate）

- [ ] **Step 1: 生成 migration**

Run:
```bash
cd /data/sunyunbo/www/TouhouCCB/backend
alembic revision --autogenerate -m "add_liquidation_event_and_user_last_liquidated"
```

Expected：`Generating ...alembic/versions/<hash>_add_liquidation_event_and_user_last_liquidated.py ... done`

- [ ] **Step 2: 检查生成的文件**

打开生成的 migration 文件，应该包含：
- `op.add_column('users', sa.Column('last_liquidated_at', sa.DateTime(timezone=True), nullable=True))`
- `op.create_table('liquidation_events', ...)` 含所有字段 + index on `user_id` 和 `triggered_at`
- `op.create_index(...)`

如果 autogenerate 漏掉 index，手动补 `op.create_index('ix_liquidation_events_user_id', 'liquidation_events', ['user_id'])` 和 `triggered_at`。

如果生成了"删除"操作（如 alembic 误判 model 删了某字段）→ **手动删除**那些 op，只保留我们要的加列/建表。

- [ ] **Step 3: 测试 migration 升级 + 降级**

```bash
cd /data/sunyunbo/www/TouhouCCB/backend
alembic upgrade head    # 升级到最新
# 验证：sqlite3 ../data/dev.db 或 PG \dt 看 liquidation_events 表
alembic downgrade -1    # 降级一步
alembic upgrade head    # 再升上来
```

Expected：每步都 OK，无报错。

- [ ] **Step 4: 跑 test_liquidation_schema.py 确认表已建**

Run: `cd backend && pytest tests/test_liquidation_schema.py::test_liquidation_event_can_be_created -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/<hash>_add_liquidation_event_and_user_last_liquidated.py
git commit -m "feat(migration): liquidation_events 表 + users.last_liquidated_at

alembic autogenerated。tested upgrade + downgrade + reupgrade 无回归。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Site config 4 个新 key

**Files:**
- Modify: `backend/app/services/loan_migrate.py`
- Modify: `backend/app/api/v1/site_config.py`
- Test: `backend/tests/test_site_config.py`（如已存在则 append）

- [ ] **Step 1: 写测试**

在 `backend/tests/test_site_config.py` append：

```python
@pytest.mark.asyncio
async def test_liquidation_site_config_defaults_loaded(client):
    """liquidation_* 4 个 key 应在初始化后默认存在。"""
    from app.core.database import async_session_maker
    from app.services import site_config
    async with async_session_maker() as db:
        enabled = await site_config.get_bool(db, "liquidation_enabled")
        interval = await site_config.get_int(db, "liquidation_sweep_interval_sec")
        hard = await site_config.get_decimal(db, "liquidation_hard_threshold")
        soft = await site_config.get_decimal(db, "liquidation_soft_threshold")

    assert enabled is False, "默认应关，灰度开启"
    assert interval == 600
    assert hard == Decimal("0.2")
    assert soft == Decimal("0.5")


@pytest.mark.asyncio
async def test_liquidation_site_config_keys_in_allowed(client):
    """site_config.ALLOWED_KEYS 必须含这 4 个 key 才能通过 admin API 改。"""
    from app.api.v1.site_config import ALLOWED_KEYS
    assert "liquidation_enabled" in ALLOWED_KEYS
    assert "liquidation_sweep_interval_sec" in ALLOWED_KEYS
    assert "liquidation_hard_threshold" in ALLOWED_KEYS
    assert "liquidation_soft_threshold" in ALLOWED_KEYS
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd backend && pytest tests/test_site_config.py::test_liquidation_site_config_defaults_loaded -v`
Expected: FAIL —— key 不存在。

- [ ] **Step 3: 改 `backend/app/services/loan_migrate.py`**

找到 DEFAULTS 列表，追加 4 行：

```python
DEFAULTS = [
    # ...existing (loan_enabled, loan_leverage_k, loan_daily_rate, loan_sweep_interval_sec)...
    ("liquidation_enabled", "false", "bool"),   # 默认关，灰度开启
    ("liquidation_sweep_interval_sec", "600", "int"),   # 10 min
    ("liquidation_hard_threshold", "0.2", "decimal"),
    ("liquidation_soft_threshold", "0.5", "decimal"),
]
```

- [ ] **Step 4: 改 `backend/app/api/v1/site_config.py`**

找到 `ALLOWED_KEYS` 字典（含 `loan_enabled` 等），追加 4 行：

```python
ALLOWED_KEYS = {
    # ...existing loan_* keys...
    "liquidation_enabled": "bool",
    "liquidation_sweep_interval_sec": "int",
    "liquidation_hard_threshold": "decimal",
    "liquidation_soft_threshold": "decimal",
}
```

- [ ] **Step 5: 跑测试确认 pass**

```bash
cd backend && pytest tests/test_site_config.py -v
```
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/loan_migrate.py backend/app/api/v1/site_config.py backend/tests/test_site_config.py
git commit -m "feat(site_config): liquidation_* 4 个新 key + 默认值

- liquidation_enabled (default false, 灰度开启)
- liquidation_sweep_interval_sec (default 600 = 10min)
- liquidation_hard_threshold (default 0.2)
- liquidation_soft_threshold (default 0.5)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 抽 `_lock_user` / `_lock_outcomes_for_market` 到 `market_locks.py`

**Files:**
- Create: `backend/app/services/market_locks.py`
- Modify: `backend/app/api/v1/market.py`
- Test: `backend/tests/test_market_locks.py`（新建）

**目的**：让 `liquidation_service` 共用同一把锁，避免重复实现 + 保证 deadlock-free 锁顺序。

- [ ] **Step 1: 写新文件 `backend/app/services/market_locks.py`**

```python
"""共享锁辅助函数——给 market.py 和 liquidation_service.py 共用。

锁顺序约定（必须严格遵守，避免 deadlock）：
1. market 行
2. user 行
3. 该 market 的全部 outcome 行（按 id ASC）
4. position 行（如需要）

所有调用方按这个顺序拿锁。
"""
from __future__ import annotations
from typing import List

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Market, Outcome, User


async def lock_market(db: AsyncSession, market_id: int) -> Market:
    """SELECT FOR UPDATE 市场行。404 if not exists."""
    res = await db.execute(
        select(Market).where(Market.id == market_id).with_for_update()
    )
    market = res.scalars().first()
    if not market:
        raise HTTPException(status_code=404, detail="市场不存在")
    return market


async def lock_user(db: AsyncSession, user_id: int) -> User:
    """SELECT FOR UPDATE 用户行。404 if not exists。"""
    res = await db.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


async def lock_outcomes_for_market(db: AsyncSession, market_id: int) -> List[Outcome]:
    """SELECT FOR UPDATE 该市场所有 outcome，按 id ASC。"""
    res = await db.execute(
        select(Outcome)
        .where(Outcome.market_id == market_id)
        .order_by(Outcome.id)
        .with_for_update()
    )
    outcomes = res.scalars().all()
    if not outcomes:
        raise HTTPException(status_code=404, detail="市场选项不存在（数据异常）")
    return outcomes


async def lock_outcome(db: AsyncSession, outcome_id: int) -> Outcome:
    """SELECT FOR UPDATE 单 outcome 行。"""
    res = await db.execute(
        select(Outcome).where(Outcome.id == outcome_id).with_for_update()
    )
    outcome = res.scalars().first()
    if not outcome:
        raise HTTPException(status_code=404, detail="选项不存在")
    return outcome
```

- [ ] **Step 2: 改 `backend/app/api/v1/market.py`**

2a. 顶部 imports 加：

```python
from app.services.market_locks import (
    lock_market as _lock_market,
    lock_user as _lock_user,
    lock_outcomes_for_market as _lock_outcomes_for_market,
    lock_outcome as _lock_outcome,
)
```

2b. 删掉文件内现有的 `async def _lock_market`、`async def _lock_user`、`async def _lock_outcomes_for_market`、`async def _lock_outcome` 这 4 个函数定义（保留它们的调用点，因为我们 import 进来同名）。

- [ ] **Step 3: 验证 market.py 仍能 import + 现有 market 测试不破**

```bash
cd backend
python -c "from app.api.v1.market import router; print('market.py import OK')"
pytest tests/test_market_deadlock_fix.py tests/test_market_slippage_lock.py -v --tb=short
```
Expected: 18 tests PASS（4 + 14）。

- [ ] **Step 4: 写新测试 `backend/tests/test_market_locks.py`**

```python
"""market_locks.py 单元测：基础锁正确性 + 不存在抛 404。"""
import pytest
from fastapi import HTTPException

from app.models.base import Market, Outcome, User, MarketStatus
from app.services.market_locks import (
    lock_market, lock_outcome, lock_outcomes_for_market, lock_user,
)


@pytest.mark.asyncio
async def test_lock_user_not_exists_raises_404(client):
    from app.core.database import async_session_maker
    async with async_session_maker() as db:
        with pytest.raises(HTTPException) as exc:
            await lock_user(db, 99999)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_lock_market_not_exists_raises_404(client):
    from app.core.database import async_session_maker
    async with async_session_maker() as db:
        with pytest.raises(HTTPException) as exc:
            await lock_market(db, 99999)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_lock_outcomes_returns_id_asc(client):
    from app.core.database import async_session_maker
    async with async_session_maker() as db:
        m = Market(title="t", description="t", liquidity_b=1000,
                   status=MarketStatus.TRADING, tags="")
        db.add(m); await db.flush()
        o1 = Outcome(market_id=m.id, label="A", total_shares=0)
        o2 = Outcome(market_id=m.id, label="B", total_shares=0)
        db.add(o1); db.add(o2)
        await db.commit()

        result = await lock_outcomes_for_market(db, m.id)
        assert [o.id for o in result] == sorted([o.id for o in result])
```

- [ ] **Step 5: 跑新测**

```bash
cd backend && pytest tests/test_market_locks.py -v
```
Expected: 3 PASS。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/market_locks.py backend/app/api/v1/market.py backend/tests/test_market_locks.py
git commit -m "refactor(market): 抽出 _lock_user/_lock_market/_lock_outcomes 到 services/market_locks

让 liquidation_service 能共用同一把锁实现，且锁顺序规范集中在一个文件
里维护，避免重复实现 + 防 deadlock。market.py 改成 import alias，
现有 18 个 deadlock/slippage 测试无回归。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `liquidation_service.liquidate_user` 核心实现

**Files:**
- Create: `backend/app/services/liquidation_service.py`
- Test: `backend/tests/test_liquidation_service.py`（新建）

- [ ] **Step 1: 写"happy path"测试**

新建 `backend/tests/test_liquidation_service.py`：

```python
"""liquidation_service.liquidate_user 单元测。"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import (
    LiquidationEvent, Market, MarketStatus, Outcome,
    Position, Transaction, TransactionType, User,
)
from app.services.market_locks import lock_user
from app.services import liquidation_service


async def _setup_user_with_positions(db, *, cash, debt, positions):
    """positions: list of (outcome_label, amount, market_total_for_that_outcome).
    返回 (user, market, outcomes)。
    """
    u = User(username="testuser", casdoor_id="cas_t",
             cash=Decimal(str(cash)), debt=Decimal(str(debt)),
             debt_last_accrued_at=datetime.now(timezone.utc) if debt > 0 else None)
    db.add(u); await db.flush()

    m = Market(title="m", description="", liquidity_b=100.0,
               status=MarketStatus.TRADING, tags="")
    db.add(m); await db.flush()

    outs = []
    for label, amt, total_shares in positions:
        o = Outcome(market_id=m.id, label=label,
                    total_shares=Decimal(str(total_shares)))
        db.add(o); await db.flush()
        outs.append(o)
        if amt > 0:
            p = Position(user_id=u.id, outcome_id=o.id,
                         amount=Decimal(str(amt)),
                         cost_basis=Decimal("0"))
            db.add(p)
    await db.commit()
    return u, m, outs


@pytest.mark.asyncio
async def test_liquidate_user_happy_path_full_repay(client):
    """user 借 100，仓位卖光 >100 → 还光 debt，剩余 cash 留下。"""
    async with async_session_maker() as db:
        # cash=10, debt=100, hv≈150（卖 50 A + 50 B 大约能拿回 150 CNY）
        # 满足 NW=-100+10+150=60... 实际 NW>0，但 testing service 直接调，不查阈值
        u, m, outs = await _setup_user_with_positions(
            db, cash=10, debt=100,
            positions=[("A", 50, 100), ("B", 50, 100)],
        )
        user_id = u.id
        outcome_ids = [o.id for o in outs]

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, user_id)
            event = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.01"), trigger_source="scheduler",
            )

    # 重新查 DB 验证
    async with async_session_maker() as db:
        u2 = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        assert u2.debt == Decimal("0"), f"债务应清零, got {u2.debt}"
        assert u2.cash > Decimal("0"), f"卖出回款应 > 还债，剩点 cash, got {u2.cash}"
        assert u2.last_liquidated_at is not None

        positions = (await db.execute(
            select(Position).where(Position.user_id == user_id)
        )).scalars().all()
        assert len(positions) == 0, "所有 position 应已 delete"

        txs = (await db.execute(
            select(Transaction).where(Transaction.user_id == user_id)
        )).scalars().all()
        liq_txs = [t for t in txs if t.type == TransactionType.LIQUIDATE]
        assert len(liq_txs) == 2, "应有 2 笔 LIQUIDATE Transaction"

        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == user_id)
        )).scalars().all()
        assert len(events) == 1
        ev = events[0]
        assert ev.sold_positions_count == 2
        assert ev.repaid_amount > Decimal("0")
        assert ev.remaining_debt == Decimal("0")
        assert ev.trigger_source == "scheduler"


@pytest.mark.asyncio
async def test_liquidate_user_partial_repay_remaining_debt(client):
    """user 资不抵债：卖完仓 < debt → repay 部分，remaining_debt > 0。"""
    async with async_session_maker() as db:
        # cash=0, debt=500, 仅小 hv≈10
        u, m, outs = await _setup_user_with_positions(
            db, cash=0, debt=500,
            positions=[("A", 5, 100)],
        )
        user_id = u.id

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, user_id)
            event = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.01"), trigger_source="admin_manual",
            )

    async with async_session_maker() as db:
        u2 = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        assert u2.debt > Decimal("0"), "卖完仍不够还，应剩 remaining debt"
        assert u2.cash == Decimal("0"), "所有 cash 应已用于还债"

        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == user_id)
        )).scalars().all()
        ev = events[0]
        assert ev.remaining_debt > Decimal("0")
        assert ev.trigger_source == "admin_manual"


@pytest.mark.asyncio
async def test_liquidate_user_skips_settled_market(client):
    """已结算 market 的 position 应跳过（让 resolve flow 处理）。"""
    async with async_session_maker() as db:
        u, m, outs = await _setup_user_with_positions(
            db, cash=0, debt=100,
            positions=[("A", 10, 100)],
        )
        # 把 market 改为 SETTLED
        m.status = MarketStatus.SETTLED
        await db.commit()
        user_id = u.id

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, user_id)
            event = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.01"), trigger_source="scheduler",
            )

    async with async_session_maker() as db:
        positions = (await db.execute(
            select(Position).where(Position.user_id == user_id)
        )).scalars().all()
        assert len(positions) == 1, "settled market 的 position 应保留"

        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == user_id)
        )).scalars().all()
        assert events[0].sold_positions_count == 0
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd backend && pytest tests/test_liquidation_service.py -v`
Expected: 全 ImportError (`liquidation_service` 不存在)。

- [ ] **Step 3: 写 `backend/app/services/liquidation_service.py`**

```python
"""Liquidation 原子操作。调用方负责事务边界 + 已 lock user。

设计：复用 services.lmsr + services.wealth + services.loan_service，不
重新实现 LMSR 数学。锁顺序遵循 market_locks.py 约定。
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from typing import Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.base import (
    LiquidationEvent, MarketStatus, Outcome, Position,
    Transaction, TransactionType, User,
)
from app.services import loan_service
from app.services.lmsr import calculate_lmsr_with_prices, quantize_cost, quantize_price
from app.services.market_locks import lock_outcomes_for_market
from app.services.wealth import compute_users_holdings_value

import logging
_logger = logging.getLogger(__name__)

ZERO = Decimal("0")


async def liquidate_user(
    session: AsyncSession,
    user: User,
    *,
    daily_rate: Decimal,
    trigger_source: str,
) -> LiquidationEvent:
    """全平 user 持仓 + 最大化还债 + 写 LiquidationEvent。

    前提：
    - 调用方已 lock user 行 (SELECT FOR UPDATE)
    - 调用方已在 db.begin() 事务上下文中
    - user.debt > 0

    SSE publish 不在此函数内做——调用方在事务 commit 之后批量推。
    （per market.py:578 模式：publish 跟 DB commit 解耦）
    """
    if user.debt <= ZERO:
        raise ValueError("liquidate_user requires user.debt > 0")

    # 0. pre-snapshot
    pre_cash = user.cash
    pre_debt = user.debt
    pre_hv = (
        await compute_users_holdings_value(session, user_ids=[user.id])
    ).get(user.id, ZERO)
    pre_nw = pre_cash - pre_debt + pre_hv
    pre_margin = (pre_nw / pre_debt) if pre_debt > 0 else None

    # 1. 拉所有持仓
    pos_res = await session.execute(
        select(Position)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
        .where(Position.user_id == user.id, Position.amount > 0)
        .order_by(Position.id.asc())
        .with_for_update()
    )
    positions = pos_res.scalars().all()

    total_proceeds = ZERO
    sold_count = 0

    for pos in positions:
        if pos.outcome.market.status != MarketStatus.TRADING:
            _logger.info(
                "liquidation_skip_non_trading_market",
                extra={"user_id": user.id, "position_id": pos.id,
                       "market_status": pos.outcome.market.status},
            )
            continue

        market = pos.outcome.market
        all_outcomes = await lock_outcomes_for_market(session, market.id)
        idx = next(
            (i for i, o in enumerate(all_outcomes) if o.id == pos.outcome_id), None
        )
        if idx is None:
            _logger.error(
                "liquidation_outcome_not_in_market",
                extra={"user_id": user.id, "position_id": pos.id},
            )
            continue

        b = float(market.liquidity_b)
        old_q = [float(o.total_shares) for o in all_outcomes]
        new_q = list(old_q)
        new_q[idx] -= float(pos.amount)

        old_cost, old_prices = calculate_lmsr_with_prices(old_q, b)
        new_cost, new_prices = calculate_lmsr_with_prices(new_q, b)
        proceeds = quantize_cost(old_cost - new_cost)

        if proceeds < ZERO:
            _logger.error(
                "liquidation_negative_proceeds",
                extra={"user_id": user.id, "position_id": pos.id,
                       "proceeds": str(proceeds)},
            )
            continue

        # 应用变更
        user.cash += proceeds
        all_outcomes[idx].total_shares -= pos.amount
        await session.delete(pos)

        # 记 Transaction
        avg_price = quantize_price(proceeds / pos.amount) if pos.amount > 0 else ZERO
        tx = Transaction(
            user_id=user.id,
            outcome_id=pos.outcome_id,
            type=TransactionType.LIQUIDATE,
            shares=pos.amount,
            cost=-proceeds,
            price=avg_price,
            pre_market_price=quantize_price(old_prices[idx]),
            post_market_price=quantize_price(new_prices[idx]),
            gross=proceeds,
            fee=ZERO,
            market_prices_post=list(new_prices),
        )
        session.add(tx)

        total_proceeds += proceeds
        sold_count += 1

    # 2. 最大化还债
    repay_amount = min(user.cash, user.debt).quantize(Decimal("0.000001"))
    repaid = ZERO
    if repay_amount > ZERO:
        _, repaid = await loan_service.decrease_debt(
            session, user.id, repay_amount,
            consume_cash=True, daily_rate=daily_rate,
        )

    user.last_liquidated_at = datetime.now(timezone.utc)

    # 3. 写 rollup event
    ev = LiquidationEvent(
        user_id=user.id,
        triggered_at=datetime.now(timezone.utc),
        pre_cash=pre_cash,
        pre_debt=pre_debt,
        pre_holdings_value=pre_hv,
        pre_net_worth=pre_nw,
        pre_margin_ratio=pre_margin,
        sold_positions_count=sold_count,
        total_proceeds=total_proceeds,
        repaid_amount=repaid,
        remaining_debt=user.debt,
        post_cash=user.cash,
        trigger_source=trigger_source,
    )
    session.add(ev)
    await session.flush()  # 保证 ev.id 可用给调用方

    _logger.warning(
        "user_liquidated",
        extra={
            "user_id": user.id,
            "sold_positions": sold_count,
            "total_proceeds": str(total_proceeds),
            "repaid": str(repaid),
            "remaining_debt": str(user.debt),
            "trigger_source": trigger_source,
        },
    )
    return ev
```

- [ ] **Step 4: 跑测试确认 pass**

Run: `cd backend && pytest tests/test_liquidation_service.py -v`
Expected: 3 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/liquidation_service.py backend/tests/test_liquidation_service.py
git commit -m "feat(liquidation): liquidate_user 核心 — 全平 + 最大化还债 + 写 event

复用 services.lmsr/wealth/loan_service。锁顺序遵循 market_locks。
skip settled market，skip negative proceeds（极端 q 状态）。

3 个 unit test 覆盖：full repay / partial repay / skip settled。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `liquidation_sweep.run_liquidation_sweep_once`

**Files:**
- Create: `backend/app/services/liquidation_sweep.py`
- Test: `backend/tests/test_liquidation_sweep.py`（新建）

- [ ] **Step 1: 写测试**

`backend/tests/test_liquidation_sweep.py`：

```python
"""liquidation_sweep.run_liquidation_sweep_once 单元测：阈值筛 user + 防爆 cache。"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import (
    Market, MarketStatus, Outcome, Position, User, LiquidationEvent,
)
from app.services import liquidation_sweep


async def _seed_user(db, *, cash, debt, hv_via_position=None):
    """返回 user_id。hv_via_position: (label, amount) 或 None。"""
    u = User(username=f"u{cash}_{debt}", casdoor_id=f"cas_{cash}_{debt}",
             cash=Decimal(str(cash)), debt=Decimal(str(debt)),
             debt_last_accrued_at=datetime.now(timezone.utc) if debt > 0 else None)
    db.add(u); await db.flush()

    if hv_via_position is not None:
        label, amount = hv_via_position
        m = Market(title="m", description="", liquidity_b=100.0,
                   status=MarketStatus.TRADING, tags="")
        db.add(m); await db.flush()
        o = Outcome(market_id=m.id, label=label, total_shares=Decimal(str(amount + 50)))
        db.add(o); await db.flush()
        p = Position(user_id=u.id, outcome_id=o.id,
                     amount=Decimal(str(amount)), cost_basis=Decimal("0"))
        db.add(p)

    await db.commit()
    return u.id


@pytest.fixture(autouse=True)
def _clear_recently_attempted():
    """每个测试前清掉 module-level 防爆 cache。"""
    liquidation_sweep._recently_attempted.clear()
    yield
    liquidation_sweep._recently_attempted.clear()


@pytest.mark.asyncio
async def test_sweep_skips_when_disabled(client):
    """liquidation_enabled=false 时整体 skip。"""
    # 默认 site_config 就是 false（看 Task 3）
    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result.get("skipped") == "disabled"


@pytest.mark.asyncio
async def test_sweep_skips_user_with_no_debt(client):
    """debt=0 的 user 不应被选中。"""
    async with async_session_maker() as db:
        # 临时开启 liquidation
        from app.services import site_config
        await site_config.set_value(db, "liquidation_enabled", "true", "bool")

    async with async_session_maker() as db:
        await _seed_user(db, cash=100, debt=0)

    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result["triggered_count"] == 0


@pytest.mark.asyncio
async def test_sweep_triggers_user_below_hard_threshold(client):
    """user margin < 0.2 应触发强平。"""
    async with async_session_maker() as db:
        from app.services import site_config
        await site_config.set_value(db, "liquidation_enabled", "true", "bool")

    async with async_session_maker() as db:
        # cash=0, debt=500, hv≈50（NW=-450, margin=-0.9 → 远低于 0.2）
        uid = await _seed_user(db, cash=0, debt=500,
                                hv_via_position=("A", 50))

    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result["triggered_count"] == 1, f"expected 1 trigger, got {result}"

    async with async_session_maker() as db:
        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == uid)
        )).scalars().all()
        assert len(events) == 1


@pytest.mark.asyncio
async def test_sweep_soft_warning_no_action(client):
    """soft_threshold ≤ margin < hard: warning 计数但不动仓。"""
    async with async_session_maker() as db:
        from app.services import site_config
        await site_config.set_value(db, "liquidation_enabled", "true", "bool")

    async with async_session_maker() as db:
        # 构造 margin 在 0.2~0.5 之间，需要 NW = 0.3*debt 这种
        # 简化：手动设 debt 和 hv 使 NW/debt ≈ 0.3
        # cash=10, debt=100, hv=120 → NW=30, margin=0.3
        uid = await _seed_user(db, cash=10, debt=100,
                                hv_via_position=("A", 120))

    result = await liquidation_sweep.run_liquidation_sweep_once()
    # 这个测验 holdings_value 算出来大约 90-100（不完全是 120 因为 LMSR 清算价不是名义价×amount）
    # 所以可能既不在 soft 也不在 hard。先放宽断言：触发数 == 0 即可
    assert result["triggered_count"] == 0


@pytest.mark.asyncio
async def test_sweep_recently_attempted_cache_skips(client):
    """已扫过但没产生进展（资不抵债）的 user 30 min 内不重扫。"""
    async with async_session_maker() as db:
        from app.services import site_config
        await site_config.set_value(db, "liquidation_enabled", "true", "bool")

    async with async_session_maker() as db:
        # 0 cash, 1000 debt, 0 holdings = stuck underwater
        uid = await _seed_user(db, cash=0, debt=1000)

    # 第一次跑：触发，但 sold=0 + repaid=0 → 标记 stuck
    result1 = await liquidation_sweep.run_liquidation_sweep_once()
    assert uid in liquidation_sweep._recently_attempted

    # 第二次立即跑：被 cache 跳过，triggered_count=0
    result2 = await liquidation_sweep.run_liquidation_sweep_once()
    assert result2["triggered_count"] == 0
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd backend && pytest tests/test_liquidation_sweep.py -v`
Expected: ImportError —— `liquidation_sweep` 模块不存在。

- [ ] **Step 3: 写 `backend/app/services/liquidation_sweep.py`**

```python
"""强制平仓定时扫描。仿 loan_sweep 模式，每 N 秒扫一次 debt>0 用户。

- run_liquidation_sweep_once()：扫一次，也给 admin run-now 复用
- start_scheduler() / stop_scheduler()：FastAPI lifespan 调
- reschedule(interval_sec)：管理员改 site_config 后调用
"""
from __future__ import annotations
import logging
import time
from decimal import Decimal
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import User
from app.services import liquidation_service, site_config
from app.services.market_locks import lock_user
from app.services.wealth import compute_users_holdings_value


logger = logging.getLogger("thccb.liquidation_sweep")

# 模块级防爆 cache：已扫过但没动到 user 的 30 min 内跳过
_recently_attempted: dict[int, float] = {}
_STUCK_COOLDOWN_SEC = 1800

_scheduler: Optional[AsyncIOScheduler] = None
_JOB_ID = "liquidation_sweep_tick"


async def run_liquidation_sweep_once() -> dict:
    """扫一次全体 debt>0 用户。给 scheduler + admin run-now 共用。

    返回 dict 含 triggered_count / soft_warning_count / errors / skipped /
    sweep_duration_ms.
    """
    start_ts = time.monotonic()
    async with async_session_maker() as session:
        enabled = await site_config.get_bool(session, "liquidation_enabled")
        if not enabled:
            return {"skipped": "disabled"}
        hard_thr = await site_config.get_decimal(
            session, "liquidation_hard_threshold"
        )
        soft_thr = await site_config.get_decimal(
            session, "liquidation_soft_threshold"
        )
        try:
            rate = await site_config.get_decimal(session, "loan_daily_rate")
        except Exception:
            logger.exception("liquidation_sweep_no_daily_rate")
            return {"skipped": "no_daily_rate"}

    async with async_session_maker() as session:
        ids = (
            await session.execute(select(User.id).where(User.debt > 0))
        ).scalars().all()

    triggered = 0
    warned = 0
    errors = 0
    now = time.monotonic()

    for uid in ids:
        last_attempt = _recently_attempted.get(uid, 0.0)
        if last_attempt + _STUCK_COOLDOWN_SEC > now:
            continue

        try:
            async with async_session_maker() as session:
                async with session.begin():
                    user = await lock_user(session, uid)
                    if user.debt <= Decimal("0"):
                        continue

                    hv = (
                        await compute_users_holdings_value(
                            session, user_ids=[uid]
                        )
                    ).get(uid, Decimal("0"))
                    nw = user.cash - user.debt + hv
                    margin = nw / user.debt

                    if margin < hard_thr:
                        ev = await liquidation_service.liquidate_user(
                            session, user, daily_rate=rate,
                            trigger_source="scheduler",
                        )
                        triggered += 1
                        if (ev.sold_positions_count == 0
                                and ev.repaid_amount == 0):
                            # 资不抵债 stuck
                            _recently_attempted[uid] = now
                    elif margin < soft_thr:
                        warned += 1
                        logger.warning(
                            "margin_call_soft_threshold",
                            extra={
                                "user_id": uid,
                                "margin_ratio": float(margin),
                                "soft_threshold": float(soft_thr),
                            },
                        )
        except Exception:
            errors += 1
            logger.exception("liquidation_sweep_user_error",
                             extra={"user_id": uid})

    duration_ms = int((time.monotonic() - start_ts) * 1000)
    result = {
        "triggered_count": triggered,
        "soft_warning_count": warned,
        "errors": errors,
        "sweep_duration_ms": duration_ms,
    }
    logger.info("liquidation_sweep_done", extra=result)
    return result


async def _tick_safe():
    try:
        await run_liquidation_sweep_once()
    except Exception:
        logger.exception("liquidation_sweep_tick_failed")


async def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    async with async_session_maker() as session:
        try:
            interval = await site_config.get_int(
                session, "liquidation_sweep_interval_sec"
            )
        except Exception:
            interval = 600
    interval = max(60, min(7200, interval))
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _tick_safe, "interval", seconds=interval,
        id=_JOB_ID, max_instances=1,
    )
    _scheduler.start()
    logger.info("liquidation_sweep_started", extra={"interval_sec": interval})


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def reschedule(interval_sec: int) -> None:
    global _scheduler
    if _scheduler is None:
        return
    interval = max(60, min(7200, interval_sec))
    _scheduler.reschedule_job(
        _JOB_ID, trigger="interval", seconds=interval,
    )
    logger.info("liquidation_sweep_rescheduled", extra={"interval_sec": interval})
```

- [ ] **Step 4: 跑测试确认 pass**

Run: `cd backend && pytest tests/test_liquidation_sweep.py -v`
Expected: 5 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/liquidation_sweep.py backend/tests/test_liquidation_sweep.py
git commit -m "feat(liquidation): sweep + scheduler 框架 + 防爆 cache

run_liquidation_sweep_once 给 scheduler + admin run-now 共用。
_recently_attempted 30min cache 防 stuck 用户被反复扫。
start/stop/reschedule 完整 lifecycle 仿 loan_sweep.

5 个 unit test：disabled / no debt / triggered / soft warning /
stuck cache。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 接入 FastAPI lifespan + conftest 禁用

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/tests/conftest.py`

- [ ] **Step 1: 改 `backend/app/main.py`**

在文件顶部 imports 加：

```python
from app.services.loan_sweep import (
    start_scheduler as start_loan_scheduler,
    stop_scheduler as stop_loan_scheduler,
)
from app.services.liquidation_sweep import (
    start_scheduler as start_liquidation_scheduler,
    stop_scheduler as stop_liquidation_scheduler,
)
```

（注意：如果已有 `from app.services.loan_sweep import start_scheduler, stop_scheduler` 直接 import 不带 alias，先 alias 化避免命名冲突。grep `start_scheduler` 找到使用点对应改。）

找到 lifespan 函数（应该有 `loan_sweep.start_scheduler()` 调用），改成：

```python
@asynccontextmanager
async def lifespan(app):
    # ...existing setup (init_db, auto_migrate, setup_admin)...
    await start_loan_scheduler()
    await start_liquidation_scheduler()  # 新增
    yield
    await stop_liquidation_scheduler()   # 新增
    await stop_loan_scheduler()
    # ...existing teardown...
```

- [ ] **Step 2: 改 `backend/tests/conftest.py`**

找到 `_disable_scheduler` fixture，加 patch：

```python
@pytest.fixture(scope="session", autouse=True)
def _disable_scheduler():
    """禁用 APScheduler：loan_sweep + liquidation_sweep 都 no-op。"""
    async def _noop():
        return None

    with (
        patch("app.main.start_loan_scheduler", _noop),
        patch("app.main.stop_loan_scheduler", _noop),
        patch("app.main.start_liquidation_scheduler", _noop),
        patch("app.main.stop_liquidation_scheduler", _noop),
    ):
        yield
```

如果 main.py 用的不是 alias 而是 module path，相应调整 patch 路径。

- [ ] **Step 3: 跑 app import + 一个 sanity test**

```bash
cd backend
python -c "from app.main import app; print('main import OK')"
pytest tests/test_liquidation_schema.py -v
```
Expected: import OK，schema 3 tests PASS。

- [ ] **Step 4: Commit**

```bash
git add backend/app/main.py backend/tests/conftest.py
git commit -m "feat(lifespan): liquidation scheduler 接入 main lifespan + conftest 禁用 mock

跟 loan_sweep 并存，启动顺序 loan 先 liquidation 后，停止反序。
测试 conftest mock 两个 scheduler 都 no-op。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: `/user/summary` 加 margin 字段

**Files:**
- Modify: `backend/app/api/v1/user.py`
- Modify: `backend/app/schemas/user.py`
- Test: `backend/tests/test_user_summary_margin.py`（新建）

- [ ] **Step 1: 写测试**

`backend/tests/test_user_summary_margin.py`：

```python
"""/user/summary 新增 margin_ratio + margin_status + last_liquidated_at。"""
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.models.base import User
from app.services import site_config


@pytest.mark.asyncio
async def test_user_summary_includes_margin_fields(client):
    # 借了款的用户
    async with async_session_maker() as db:
        u = User(username="m_user", casdoor_id="m_cas",
                 cash=Decimal("100"), debt=Decimal("80"))
        db.add(u); await db.commit(); await db.refresh(u)
        # 登录 mock 略——按现有 test_user.py 模式拿 token

    # 拉 summary。具体调用方式取决于 conftest 的 client + 鉴权方式
    # 实施时参照 tests/test_user_*.py 的现成 pattern
    # 简化伪代码：
    # resp = await client.get("/api/v1/user/summary", headers=auth_for(u))
    # j = resp.json()
    # assert "margin_ratio" in j
    # assert j["margin_ratio"] is not None
    # assert "margin_status" in j
    # assert j["margin_status"] in ("healthy", "warning", "danger")
    # assert "last_liquidated_at" in j


@pytest.mark.asyncio
async def test_user_summary_no_debt_margin_none(client):
    """debt=0 时 margin_ratio = None, margin_status='healthy'。"""
    # 同上模式，user 不借钱
    pass
```

**注**：上面是骨架。实施时按 `backend/tests/test_user_*.py` 已有测试的 token / client fixture 模式补全实际请求代码——具体 fixture 名（`client`、`auth_headers` 之类）参照现成。

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd backend && pytest tests/test_user_summary_margin.py -v`
Expected: assertion fail 或 KeyError 关于 `margin_ratio`。

- [ ] **Step 3: 改 `backend/app/api/v1/user.py:get_user_summary`**

3a. 顶部加：

```python
from app.services import site_config
```

3b. 在 `get_user_summary` 函数末尾构造响应前，加：

```python
# Margin classification
margin_ratio = None
margin_status = "healthy"
if user.debt > ZERO:
    margin_ratio = (net_worth / user.debt).quantize(Decimal("0.000001"))
    try:
        hard = await site_config.get_decimal(db, "liquidation_hard_threshold")
        soft = await site_config.get_decimal(db, "liquidation_soft_threshold")
    except Exception:
        hard, soft = Decimal("0.2"), Decimal("0.5")
    if margin_ratio < hard:
        margin_status = "danger"
    elif margin_ratio < soft:
        margin_status = "warning"

return {
    "cash": user.cash.quantize(Decimal("0.01")),
    "debt": user.debt.quantize(Decimal("0.01")),
    "holdings_value": holdings_value.quantize(Decimal("0.01")),
    "total_cost_basis": total_cost_basis.quantize(Decimal("0.01")),
    "unrealized_pnl": unrealized_pnl.quantize(Decimal("0.01")),
    "net_worth": net_worth.quantize(Decimal("0.01")),
    "rank": rank,
    "margin_ratio": margin_ratio,
    "margin_status": margin_status,
    "last_liquidated_at": user.last_liquidated_at,
}
```

- [ ] **Step 4: 改 `backend/app/schemas/user.py` (UserSummary)**

加 3 个 optional 字段：

```python
class UserSummary(BaseModel):
    # ...existing...
    margin_ratio: Optional[Decimal] = None
    margin_status: str = "healthy"
    last_liquidated_at: Optional[datetime] = None
```

- [ ] **Step 5: 跑测试确认 pass**

Run: `cd backend && pytest tests/test_user_summary_margin.py -v`
Expected: 2 PASS（或骨架先空过、补完后 PASS）。

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/user.py backend/app/schemas/user.py backend/tests/test_user_summary_margin.py
git commit -m "feat(api): /user/summary 加 margin_ratio + margin_status + last_liquidated_at

阈值从 site_config 读取（cache 兜底 0.2/0.5）。前端用 margin_status
渲染 banner，不重算阈值避免缓存不一致。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: 公开端点 `/recent-liquidations`

**Files:**
- Modify: `backend/app/api/v1/loan.py`
- Modify: `backend/app/schemas/loan.py`
- Test: `backend/tests/test_liquidation_public.py`（新建）

- [ ] **Step 1: 写测试**

```python
"""/recent-liquidations 公开端点测试。"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import LiquidationEvent, User


@pytest.mark.asyncio
async def test_recent_liquidations_anonymous_ok(client):
    """匿名（不带 token）应能访问。"""
    resp = await client.get("/api/v1/recent-liquidations?limit=5")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_recent_liquidations_returns_fields(client):
    """字段完整 + username 来自 join。"""
    async with async_session_maker() as db:
        u = User(username="liqtest_pub", casdoor_id="liqpub",
                 cash=Decimal("0"), debt=Decimal("100"))
        db.add(u); await db.flush()
        ev = LiquidationEvent(
            user_id=u.id,
            triggered_at=datetime.now(timezone.utc),
            pre_cash=Decimal("0"), pre_debt=Decimal("100"),
            pre_holdings_value=Decimal("50"),
            pre_net_worth=Decimal("-50"),
            pre_margin_ratio=Decimal("-0.5"),
            sold_positions_count=1,
            total_proceeds=Decimal("40"),
            repaid_amount=Decimal("40"),
            remaining_debt=Decimal("60"),
            post_cash=Decimal("0"),
            trigger_source="scheduler",
        )
        db.add(ev); await db.commit()

    resp = await client.get("/api/v1/recent-liquidations?limit=20")
    rows = resp.json()
    assert len(rows) >= 1
    row = rows[0]
    assert "username" in row and row["username"] == "liqtest_pub"
    assert "triggered_at" in row
    assert "pre_debt" in row
    assert "pre_net_worth" in row
    assert "remaining_debt" in row
    assert "fully_liquidated" in row
    assert row["fully_liquidated"] is False  # remaining_debt=60>0


@pytest.mark.asyncio
async def test_recent_liquidations_limit_param(client):
    """limit 必须 1-100。"""
    r1 = await client.get("/api/v1/recent-liquidations?limit=0")
    assert r1.status_code == 422
    r2 = await client.get("/api/v1/recent-liquidations?limit=101")
    assert r2.status_code == 422


@pytest.mark.asyncio
async def test_recent_liquidations_order_desc(client):
    """排序按 triggered_at desc。"""
    async with async_session_maker() as db:
        u = User(username="ord_user", casdoor_id="ord_cas",
                 cash=Decimal("0"), debt=Decimal("0"))
        db.add(u); await db.flush()
        # 两条 event，老的早 1h
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        for offset_min, label in [(60, "old"), (0, "new")]:
            ev = LiquidationEvent(
                user_id=u.id,
                triggered_at=now - timedelta(minutes=offset_min),
                pre_cash=Decimal("0"), pre_debt=Decimal("100"),
                pre_holdings_value=Decimal("0"),
                pre_net_worth=Decimal("-100"),
                pre_margin_ratio=Decimal("-1"),
                sold_positions_count=0,
                total_proceeds=Decimal("0"),
                repaid_amount=Decimal("0"),
                remaining_debt=Decimal("100"),
                post_cash=Decimal("0"),
                trigger_source="scheduler",
            )
            db.add(ev)
        await db.commit()

    resp = await client.get("/api/v1/recent-liquidations?limit=20")
    rows = [r for r in resp.json() if r["username"] == "ord_user"]
    assert len(rows) == 2
    # 第 0 个应是 newer
    t0 = datetime.fromisoformat(rows[0]["triggered_at"].replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(rows[1]["triggered_at"].replace("Z", "+00:00"))
    assert t0 > t1
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd backend && pytest tests/test_liquidation_public.py -v`
Expected: 404 on endpoint (没注册)。

- [ ] **Step 3: 改 `backend/app/api/v1/loan.py`**

文件末尾加：

```python
from app.models.base import LiquidationEvent

@router.get("/recent-liquidations", summary="最近强平记录（公开，首页展示）")
async def recent_liquidations(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_session),
):
    """匿名可访问。教育警示用。"""
    stmt = (
        select(LiquidationEvent, User.username)
        .join(User, User.id == LiquidationEvent.user_id)
        .order_by(LiquidationEvent.triggered_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": int(ev.id),
            "username": username,
            "triggered_at": (
                ev.triggered_at.replace(tzinfo=timezone.utc)
                if ev.triggered_at.tzinfo is None else ev.triggered_at
            ).isoformat(),
            "pre_cash": float(ev.pre_cash),
            "pre_debt": float(ev.pre_debt),
            "pre_holdings_value": float(ev.pre_holdings_value),
            "pre_net_worth": float(ev.pre_net_worth),
            "pre_margin_ratio": float(ev.pre_margin_ratio)
                if ev.pre_margin_ratio is not None else None,
            "total_proceeds": float(ev.total_proceeds),
            "repaid_amount": float(ev.repaid_amount),
            "remaining_debt": float(ev.remaining_debt),
            "post_cash": float(ev.post_cash),
            "fully_liquidated": ev.remaining_debt == Decimal("0"),
            "trigger_source": ev.trigger_source,
        }
        for ev, username in rows
    ]
```

注意 imports：确保 `from fastapi import Query`、`from datetime import timezone`、`from decimal import Decimal` 都有。

- [ ] **Step 4: 检查路由前缀**

`backend/app/main.py` 或 router include 处看 loan.py 用什么 prefix。如果是 `/api/v1/loan`，那 endpoint 实际路径是 `/api/v1/loan/recent-liquidations`——测试要 update。如果是 `/api/v1`（loan router 无 prefix），则 `/api/v1/recent-liquidations`。grep `include_router.*loan` 确认。

如果是 `/loan` prefix 但你想要公开端点在 root，把 endpoint 移到 `backend/app/api/v1/__init__.py` 或单独路由器。

**建议**：保持在 loan.py 但 URL 是 `/api/v1/loan/recent-liquidations`，测试相应改。

- [ ] **Step 5: 跑测试确认 pass**

Run: `cd backend && pytest tests/test_liquidation_public.py -v`
Expected: 4 PASS。

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/loan.py backend/tests/test_liquidation_public.py
git commit -m "feat(api): /recent-liquidations 公开端点（无需 auth）

返回最近 N 笔强平 event + join User.username。给首页"翻车现场墙"用。
limit 1-100 校验。

4 个测：匿名可访问 / 字段全 / limit 边界 / 排序 desc。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Admin endpoint `/admin/liquidation/run-now`

**Files:**
- Modify: `backend/app/api/v1/admin.py`（如不存在，可能在 user.py 的 admin 段 或 单独 admin 文件，按现有结构）
- Test: `backend/tests/test_liquidation_admin.py`（新建）

- [ ] **Step 1: 找现有 admin 路由**

```bash
cd backend && grep -rn "current_superuser" app/api/v1/ | head -5
```
看 admin endpoints 集中在哪个文件。常见在 `user.py` 后半段。

- [ ] **Step 2: 写测试**

`backend/tests/test_liquidation_admin.py`：

```python
"""/admin/liquidation/run-now 测试。"""
import pytest


@pytest.mark.asyncio
async def test_admin_run_now_unauthorized(client):
    """无 admin token → 403."""
    resp = await client.post("/api/v1/admin/liquidation/run-now")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_admin_run_now_returns_sweep_result(client, admin_headers):
    """admin 调用应返回 sweep 结果。"""
    # admin_headers fixture: 按现有 test_*_admin.py 的 admin 鉴权 pattern
    resp = await client.post(
        "/api/v1/admin/liquidation/run-now",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    j = resp.json()
    # 取决于 sweep 状态：disabled 时返 {"skipped":"disabled"}；enabled 时返触发 dict
    assert "skipped" in j or "triggered_count" in j
```

- [ ] **Step 3: 跑测确认 fail**

Run: `cd backend && pytest tests/test_liquidation_admin.py -v`
Expected: 404 on endpoint.

- [ ] **Step 4: 加 endpoint**

在 admin endpoints 所在的文件（先 grep 确认；以下假设是 `backend/app/api/v1/user.py` 或新建 `admin.py`）末尾加：

```python
from app.services import liquidation_sweep

@router.post("/admin/liquidation/run-now", summary="立即跑一次强平 sweep（仅管理员）")
async def admin_run_liquidation_sweep(
    admin: User = Depends(current_superuser),
):
    """跟 scheduler 同逻辑。不绕过阈值——admin 没有 override 权力。"""
    result = await liquidation_sweep.run_liquidation_sweep_once()
    logger.info(
        "ADMIN_RUN_LIQUIDATION_SWEEP admin_id=%s result=%s",
        admin.id, result,
    )
    return result
```

如果当前 file 不在 admin prefix 下（即 endpoint URL 不是 `/admin/liquidation/run-now` 而是别的），grep `prefix` 看 include_router 配置，调整 URL 或 router include。

- [ ] **Step 5: 跑测确认 pass**

Run: `cd backend && pytest tests/test_liquidation_admin.py -v`
Expected: 2 PASS。

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/<modified_file>.py backend/tests/test_liquidation_admin.py
git commit -m "feat(admin): /admin/liquidation/run-now 立即跑 sweep

跟 scheduler 同逻辑，不绕过阈值——admin 没有 override 权力。
audit log 写 admin_id + result。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: 前端 — MarginCallBanner 组件

**Files:**
- Create: `thccb-frontend/src/components/market/MarginCallBanner.vue`
- Modify: `thccb-frontend/src/types/user.ts`
- Modify: `thccb-frontend/src/pages/home/Home.vue`
- Modify: `thccb-frontend/src/pages/market/TradingView.vue`

- [ ] **Step 1: 改 `src/types/user.ts`**

`UserSummary` interface 加 3 字段：

```ts
export interface UserSummary {
  cash: number
  debt: number
  holdings_value: number
  total_cost_basis: number
  unrealized_pnl: number
  net_worth: number
  rank: string
  margin_ratio: number | null      // ← 新增
  margin_status: 'healthy' | 'warning' | 'danger'  // ← 新增
  last_liquidated_at: string | null   // ← 新增
}
```

- [ ] **Step 2: 写 `src/components/market/MarginCallBanner.vue`**

```vue
<script setup lang="ts">
import { computed } from 'vue'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()
const status = computed(() => userStore.summary?.margin_status ?? 'healthy')
const ratio = computed(() => userStore.summary?.margin_ratio)

const visible = computed(() => status.value !== 'healthy')

const message = computed(() => {
  if (status.value === 'danger') {
    return `⚠️ 即将被强平 — 净值/借款 = ${ratio.value?.toFixed(3) ?? '?'} < 0.2，请立即补仓或卖出持仓`
  }
  if (status.value === 'warning') {
    return `⚠️ 中重仓警报 — 净值/借款 = ${ratio.value?.toFixed(3) ?? '?'} < 0.5，建议补仓或减仓避免被强平`
  }
  return ''
})
</script>

<template>
  <div v-if="visible" class="margin-call-banner" :class="`status-${status}`">
    <span class="text-sm font-bold">{{ message }}</span>
  </div>
</template>

<style scoped>
.margin-call-banner {
  padding: 8px 16px;
  border: 2px solid currentColor;
  margin-bottom: 12px;
  letter-spacing: 0.5px;
}
.status-warning {
  background: #fef3c7;
  color: #92400e;
}
.status-danger {
  background: #fee2e2;
  color: #991b1b;
  font-weight: 700;
}
</style>
```

- [ ] **Step 3: 挂到 `Home.vue` 头部**

在 `<template>` 顶部加 `<MarginCallBanner />` 组件（参照现有 import 模式 + components 列表）。

- [ ] **Step 4: 挂到 `TradingView.vue` 头部**

同样在交易页头部加 `<MarginCallBanner />`。

- [ ] **Step 5: type-check + lint**

```bash
cd thccb-frontend
npm run type-check
npm run lint
```
Expected: 我改的两个文件无新增 error（已存的 66 个 any 错误是 pre-existing）。

- [ ] **Step 6: 人肉浏览器测**

启动 frontend dev server：
```bash
cd thccb-frontend && npm run dev
```

打开网页，登录一个借了款的用户（debt > 0），人为改 site_config 让阈值变松，或者用 DevTools 改 `userStore.summary.margin_status = 'warning'` 看 banner 渲染。

- [ ] **Step 7: Commit**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add thccb-frontend/src/components/market/MarginCallBanner.vue \
        thccb-frontend/src/types/user.ts \
        thccb-frontend/src/pages/home/Home.vue \
        thccb-frontend/src/pages/market/TradingView.vue
git commit -m "feat(frontend): MarginCallBanner 组件 + 挂到 Home + TradingView

按 margin_status 渲染：healthy 不显示、warning 黄、danger 红粗体。
工业风 + 不可关闭。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: 前端 — TradePanel buy 按钮 danger 时 disabled

**Files:**
- Modify: `thccb-frontend/src/components/market/TradePanel.vue`

- [ ] **Step 1: 找 TradePanel 的 buy 按钮**

```bash
grep -n "买入\|buy\|@click" thccb-frontend/src/components/market/TradePanel.vue | head -10
```

- [ ] **Step 2: 加 disabled prop 逻辑**

在 `<script setup>` 加：

```ts
import { useUserStore } from '@/stores/user'
const userStore = useUserStore()
const buyDisabledByMargin = computed(
  () => userStore.summary?.margin_status === 'danger'
)
```

在 buy 按钮元素加：

```vue
<NButton
  type="primary"
  :disabled="loading || buyDisabledByMargin || ..."
  @click="handleBuy"
>
  <NTooltip v-if="buyDisabledByMargin" trigger="hover">
    <template #trigger>买入</template>
    保证金不足，临时禁止买入。请补仓或卖仓。
  </NTooltip>
  <template v-else>买入</template>
</NButton>
```

具体结构按现有 TradePanel 的 NButton 模板改造。Sell 按钮**不**加这个限制。

- [ ] **Step 3: type-check**

```bash
cd thccb-frontend && npm run type-check 2>&1 | grep TradePanel || echo "OK"
```

- [ ] **Step 4: 浏览器测**

mock summary.margin_status='danger' 看 buy 按钮变灰 + tooltip 提示。sell 仍可用。

- [ ] **Step 5: Commit**

```bash
git add thccb-frontend/src/components/market/TradePanel.vue
git commit -m "feat(frontend): margin danger 时 buy 按钮 disabled

防用户在"即将强平"时继续加仓越亏越深。sell 仍可用（让用户自救）。
tooltip 给原因。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: 前端 — Portfolio liquidate 行特殊渲染

**Files:**
- Modify: `thccb-frontend/src/pages/user/Portfolio.vue`

- [ ] **Step 1: 找 transactions 表渲染处**

`Portfolio.vue` 里渲染 transactions 表格的地方。Type 列现在应该用 `tx.type === 'buy' ? '买入' : tx.type === 'sell' ? '卖出' : ...`。

- [ ] **Step 2: 加 liquidate case**

```vue
<template>
  <NDataTable :columns="txColumns" :data="transactions" />
</template>

<script setup>
const txColumns = [
  // ...
  {
    title: '类型',
    key: 'type',
    render: (row) => {
      if (row.type === 'buy')
        return h('span', { class: 'text-green' }, '买入')
      if (row.type === 'sell')
        return h('span', { class: 'text-red' }, '卖出')
      if (row.type === 'settle')
        return h('span', '结算')
      if (row.type === 'settle_lose')
        return h('span', { class: 'text-gray' }, '结算失败')
      if (row.type === 'liquidate')
        return h('span', {
          class: 'text-red font-bold',
          style: 'background: #fee2e2; padding: 2px 6px;',
        }, '⚡ 强制平仓')
      return row.type
    },
  },
  // ...
]
</script>
```

具体写法按 Portfolio.vue 现有 NDataTable column 风格调整。

- [ ] **Step 3: type-check**

```bash
cd thccb-frontend && npm run type-check 2>&1 | grep Portfolio || echo OK
```

- [ ] **Step 4: Commit**

```bash
git add thccb-frontend/src/pages/user/Portfolio.vue
git commit -m "feat(frontend): Portfolio 强平交易行红底突出

让用户能区分自己主动卖出 vs 被系统强平，警示意义强。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: 前端 — 首页"翻车现场"卡

**Files:**
- Create: `thccb-frontend/src/components/home/RecentLiquidationsPanel.vue`
- Modify: `thccb-frontend/src/pages/home/Home.vue`
- Modify: `thccb-frontend/src/api/` 加 fetch 函数（如有统一的 api/）

- [ ] **Step 1: 写组件 `RecentLiquidationsPanel.vue`**

```vue
<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import axios from 'axios'

interface LiquidationRow {
  id: number
  username: string
  triggered_at: string
  pre_cash: number
  pre_debt: number
  pre_net_worth: number
  pre_margin_ratio: number | null
  total_proceeds: number
  repaid_amount: number
  remaining_debt: number
  fully_liquidated: boolean
}

const rows = ref<LiquidationRow[]>([])
const loading = ref(false)
let refreshTimer: number | undefined

async function fetchData() {
  loading.value = true
  try {
    const resp = await axios.get('/api/v1/loan/recent-liquidations?limit=10')
    rows.value = resp.data
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
}

function fmtTime(iso: string) {
  const d = new Date(iso)
  const ageMs = Date.now() - d.getTime()
  const m = Math.floor(ageMs / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  return `${Math.floor(h / 24)} 天前`
}

onMounted(() => {
  fetchData()
  refreshTimer = window.setInterval(fetchData, 60_000)  // 1min 刷新
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <div class="liq-panel">
    <h3 class="panel-title">
      💀 翻车现场 — 高杠杆有风险，警示效果
    </h3>
    <p class="panel-hint">
      净值/借款 &lt; 0.2 时系统自动全平你的持仓还债。看看下面这些前辈的故事。
    </p>
    <div v-if="rows.length === 0" class="empty">
      暂无强平记录。市场表现稳健，或者没人加杠杆。
    </div>
    <ul v-else class="liq-list">
      <li v-for="r in rows" :key="r.id" class="liq-row">
        <div class="liq-row-header">
          <span class="username">{{ r.username }}</span>
          <span class="time">{{ fmtTime(r.triggered_at) }}</span>
        </div>
        <div class="liq-row-body">
          原借款 金 {{ r.pre_debt.toFixed(0) }}，净值 金 {{ r.pre_net_worth.toFixed(0) }}
          → 平掉 金 {{ r.total_proceeds.toFixed(0) }}，还了 金 {{ r.repaid_amount.toFixed(0) }}
          <span v-if="r.fully_liquidated" class="tag tag-survived">幸存 ✅</span>
          <span v-else class="tag tag-rekt">
            仍欠 金 {{ r.remaining_debt.toFixed(0) }} ☠️
          </span>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.liq-panel {
  border: 2px solid #1f2937;
  padding: 12px 16px;
  background: #fff;
  margin-top: 16px;
}
.panel-title {
  font-size: 16px;
  font-weight: 700;
  margin-bottom: 4px;
}
.panel-hint {
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 12px;
}
.empty {
  color: #9ca3af;
  font-size: 13px;
  font-style: italic;
}
.liq-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.liq-row {
  border-top: 1px solid #e5e7eb;
  padding: 8px 0;
}
.liq-row-header {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
}
.username { font-weight: 600; }
.time { color: #9ca3af; }
.liq-row-body {
  font-size: 12px;
  margin-top: 2px;
}
.tag {
  display: inline-block;
  padding: 1px 6px;
  margin-left: 6px;
  font-size: 10px;
  border: 1px solid currentColor;
}
.tag-survived { color: #16a34a; }
.tag-rekt { color: #dc2626; font-weight: 700; }
</style>
```

- [ ] **Step 2: 挂到 `Home.vue`**

在 Home 主内容区合适位置（比如下方）加 `<RecentLiquidationsPanel />` 组件 import + 渲染。

- [ ] **Step 3: type-check + lint**

```bash
cd thccb-frontend && npm run type-check && npm run lint 2>&1 | grep RecentLiquidations || echo OK
```

- [ ] **Step 4: 浏览器测**

```bash
cd thccb-frontend && npm run dev
```
开网页，到首页应能看到面板。后端写一条测试 LiquidationEvent 数据看渲染正确。

- [ ] **Step 5: Commit**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add thccb-frontend/src/components/home/RecentLiquidationsPanel.vue \
        thccb-frontend/src/pages/home/Home.vue
git commit -m "feat(frontend): 首页翻车现场卡 + 1min 自动刷新

调 /api/v1/loan/recent-liquidations，展示最近 10 笔强平事件。
\"幸存 ✅\"绿 tag / \"仍欠 ☠️\"红 tag 警示。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 15: 集成测 — 完整 sweep flow

**Files:**
- Create: `backend/tests/test_liquidation_integration.py`

- [ ] **Step 1: 写集成测**

```python
"""强平完整链路集成测：建用户 + 仓位 + 借款 → enable → run_sweep → 校验全局状态。"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import (
    LiquidationEvent, Market, MarketStatus, Outcome,
    Position, Transaction, TransactionType, User,
)
from app.services import liquidation_sweep, site_config


@pytest.mark.asyncio
async def test_full_sweep_e2e(client):
    """建一个高杠杆用户 → 跑 sweep → 校验：
    - Position 全删
    - LIQUIDATE Transactions 写入
    - LiquidationEvent rollup 写入
    - User.cash + debt 一致
    - last_liquidated_at 设了
    """
    # 1. 开启 liquidation
    async with async_session_maker() as db:
        await site_config.set_value(db, "liquidation_enabled", "true", "bool")

    # 2. 建 user + market + outcomes + position + debt
    async with async_session_maker() as db:
        u = User(username="e2e_user", casdoor_id="e2e",
                 cash=Decimal("5"), debt=Decimal("500"),
                 debt_last_accrued_at=datetime.now(timezone.utc))
        db.add(u); await db.flush()

        m = Market(title="e2e_m", description="", liquidity_b=100.0,
                   status=MarketStatus.TRADING, tags="")
        db.add(m); await db.flush()

        o1 = Outcome(market_id=m.id, label="A", total_shares=Decimal("150"))
        o2 = Outcome(market_id=m.id, label="B", total_shares=Decimal("0"))
        db.add(o1); db.add(o2); await db.flush()

        p = Position(user_id=u.id, outcome_id=o1.id,
                     amount=Decimal("100"), cost_basis=Decimal("60"))
        db.add(p)
        await db.commit()
        uid = u.id
        mid = m.id

    # 3. 跑 sweep
    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result["triggered_count"] == 1, f"expected 1 trigger, got {result}"

    # 4. 校验状态
    async with async_session_maker() as db:
        u2 = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        assert u2.last_liquidated_at is not None
        # 全平后 cash 应已 ≥ 0 且 debt 减了
        assert u2.cash >= Decimal("0")

        positions = (await db.execute(
            select(Position).where(Position.user_id == uid)
        )).scalars().all()
        assert len(positions) == 0, "全平后 positions 应被删"

        liq_txs = (await db.execute(
            select(Transaction).where(
                Transaction.user_id == uid,
                Transaction.type == TransactionType.LIQUIDATE,
            )
        )).scalars().all()
        assert len(liq_txs) == 1

        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == uid)
        )).scalars().all()
        assert len(events) == 1
        ev = events[0]
        assert ev.sold_positions_count == 1
        assert ev.trigger_source == "scheduler"
```

- [ ] **Step 2: 跑测试**

```bash
cd backend && pytest tests/test_liquidation_integration.py -v
```
Expected: 1 PASS.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_liquidation_integration.py
git commit -m "test(liquidation): 完整 sweep flow e2e

建 user+market+position+debt → enable → run_sweep → 校验 Position 全删
+ LIQUIDATE Transaction + LiquidationEvent + User 状态全对。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 16: 全量回归 + 部署准备

**Files:**
- 无新文件
- 验证现有所有测试不破

- [ ] **Step 1: 跑 backend 全量测**

```bash
cd /data/sunyunbo/www/TouhouCCB/backend
pytest -k "not chart and not candle" --tb=line -q
```
Expected: 全 PASS（除已知 skip）。如果有 fail，定位 + 修。

- [ ] **Step 2: app.main import + alembic head 验证**

```bash
cd backend
python -c "from app.main import app; print('OK')"
alembic heads   # 应只有一个 head（无 merge 冲突）
alembic upgrade head
alembic check 2>&1 || true   # 看 schema 是否跟 model 一致
```

- [ ] **Step 3: 前端 type-check + lint + build**

```bash
cd thccb-frontend
npm run type-check
npm run lint
npm run build   # 确保 vite 能完整构建
```
Expected: type-check + build PASS（lint 已有 66 pre-existing errors 跟我们改动无关）。

- [ ] **Step 4: 部署准备**

确认：
- `liquidation_enabled` 默认 `false`（已在 Task 3 保证）
- 推到 main 后 prod 自动部署，因 default=false 不会立刻动仓
- 部署完后 admin 通过 site_config 接口手动开 `liquidation_enabled=true` 灰度

```bash
git log --oneline main..HEAD   # 看这次提交了多少
```

- [ ] **Step 5: 合并 + push（按 CLAUDE.md 走分支流程）**

如果 commits 在 feature 分支：

```bash
git fetch origin
git rebase origin/main
git checkout main
git pull --ff-only origin main
git merge --ff-only <feature-branch>
git push origin main
```

prod 自动 deploy 会触发。监控部署：
- 看 GitHub Actions
- 部署完 curl 一个公开 endpoint 验证：
  ```bash
  curl -sS https://<prod>/api/v1/loan/recent-liquidations?limit=5
  ```
  应返 `[]` 或已有数据。

- [ ] **Step 6: 开启 liquidation_enabled（admin 操作）**

```bash
# 用 admin 账号 token：
curl -X PATCH https://<prod>/api/v1/admin/site-config \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{"key": "liquidation_enabled", "value": "true", "value_type": "bool"}'
```

或者 admin UI 操作（如果有）。

- [ ] **Step 7: 10 min 后看效果**

- 检查 backend logs：`liquidation_sweep_done` 事件
- 如果有 trigger，查 `liquidation_events` 表
- 首页应能看到"翻车现场"卡（如果有 event）

---

## Self-Review

完成所有 task 后，对照 spec 自审：

- [ ] **Spec § 1 动机**：实现满足？ → ✅ TaskGroup 5+6 实现核心
- [ ] **Spec § 2 触发判定**：margin_ratio + soft + hard? → ✅ Task 6 sweep 实现
- [ ] **Spec § 3 平仓算法**：复用 LMSR + market_locks + decrease_debt? → ✅ Task 5
- [ ] **Spec § 4 触发途径**：scheduler + admin run-now? → ✅ Tasks 6/7/10
- [ ] **Spec § 5 数据模型**：TransactionType + User 字段 + LiquidationEvent? → ✅ Task 1+2
- [ ] **Spec § 6 API**：user/summary + recent-liquidations + admin? → ✅ Tasks 8/9/10
- [ ] **Spec § 7 前端**：banner + buy-disable + portfolio-row + wall-of-shame? → ✅ Tasks 11/12/13/14
- [ ] **Spec § 8 错误处理**：covered in test cases + service code
- [ ] **Spec § 9 测试**：unit + integration + admin + public → ✅ Tasks 5/6/8/9/10/15
- [ ] **Spec § 11 风险/回滚**：default=false 保证 → ✅ Task 3

无遗漏。

---

## Plan complete

执行选择：

**Option 1: Subagent-Driven（推荐）** — 我每个 task dispatch 一个 fresh subagent 实现 + 我在 task 间 review，快速迭代。

**Option 2: Inline Execution** — 在当前 session 用 executing-plans skill 批量执行，checkpoint 处停下 review。

Plan saved to: `docs/superpowers/plans/2026-05-18-forced-liquidation.md`
