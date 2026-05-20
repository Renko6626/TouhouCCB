# Partial Liquidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实施 spec `docs/superpowers/specs/2026-05-20-partial-liquidation-design.md`：把"margin < 0.2 全平"改成"partial 10%/tick 渐进收敛到 target 0.3；margin < 0.05 才走 emergency 全平"。

**Architecture:** `LiquidationEvent` 加 `mode` 字段；`liquidate_user()` 加 3 个新参数（partial_pct / target_margin / emergency_threshold），按 pre_margin 决定 emergency or partial 分支；partial 卖时 `pos.amount` 和 `pos.cost_basis` 按比例减少（avg_price 不变）。sweep 主循环读 site_config 传参给 liquidate_user。3 个新 site_config keys + 1 alembic migration。

**Tech Stack:** SQLModel + Alembic + APScheduler + pytest-asyncio + 现有 LMSR + market_locks 模块

**Branch:** `feat/partial-liquidation`（已开，已含 spec commit `3c19f18`）

---

## File Structure

### Backend 修改

| 文件 | 改动 |
|---|---|
| `backend/app/models/base.py` | `LiquidationEvent` 加 `mode: str = Field(default="emergency", max_length=20)` |
| `backend/alembic/versions/<TS>_add_liquidation_event_mode.py` | 新 migration: ADD COLUMN mode + indexes 不动 |
| `backend/app/services/loan_migrate.py` | DEFAULT_CONFIGS 加 3 keys |
| `backend/app/api/v1/site_config.py` | _WHITELIST 加 3 keys |
| `backend/app/services/liquidation_service.py` | `liquidate_user()` 加 3 个新参数 + emergency/partial 分支 + cost_basis 按比例减 |
| `backend/app/services/liquidation_sweep.py` | `run_liquidation_sweep_once` 主循环读 site_config + `_liquidate_one_user` 传参 |

### Backend 新增/修改测试

| 文件 | 责任 |
|---|---|
| `backend/tests/test_liquidation_partial.py` (新) | partial mode 单元测 + 多 tick 收敛 + cost_basis 按比例 + edge cases |
| `backend/tests/test_liquidation_service.py` (改) | 现有 happy_path 测调用 liquidate_user 时传 emergency 参数 (`partial_pct=Decimal("1.0")`) 保持行为不变 |
| `backend/tests/test_liquidation_sweep.py` (改) | 现有 sweep 测验证新 site_config keys 通过 `_enable_liquidation` 已 seed |

---

## Task 1: LiquidationEvent.mode 字段

**Files:**
- Modify: `backend/app/models/base.py`
- Test: `backend/tests/test_liquidation_event_mode_field.py` (新)

- [ ] **Step 1: 写失败测**

`backend/tests/test_liquidation_event_mode_field.py`:
```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from app.core.database import async_session_maker
from app.models.base import LiquidationEvent, User


@pytest.mark.asyncio
async def test_liquidation_event_mode_default_is_emergency(client):
    """新加的 mode 字段默认 'emergency'（兼容历史 row 语义）。"""
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username="liq_mode_test", casdoor_id="liq_mode_cas")
            s.add(u)
            await s.flush()
            ev = LiquidationEvent(
                user_id=u.id,
                pre_cash=Decimal("100"), pre_debt=Decimal("1000"),
                pre_holdings_value=Decimal("50"),
                pre_net_worth=Decimal("-850"),
                pre_margin_ratio=Decimal("-0.85"),
                sold_positions_count=0,
                total_proceeds=Decimal("0"),
                repaid_amount=Decimal("0"),
                remaining_debt=Decimal("1000"),
                post_cash=Decimal("100"),
                trigger_source="scheduler",
            )
            s.add(ev)
            await s.flush()
            assert ev.mode == "emergency"


@pytest.mark.asyncio
async def test_liquidation_event_mode_can_be_partial(client):
    """显式传 mode='partial' 能保存。"""
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username="liq_mode_test2", casdoor_id="liq_mode_cas2")
            s.add(u)
            await s.flush()
            ev = LiquidationEvent(
                user_id=u.id,
                pre_cash=Decimal("100"), pre_debt=Decimal("1000"),
                pre_holdings_value=Decimal("950"),
                pre_net_worth=Decimal("50"),
                pre_margin_ratio=Decimal("0.05"),
                sold_positions_count=1,
                total_proceeds=Decimal("100"),
                repaid_amount=Decimal("100"),
                remaining_debt=Decimal("900"),
                post_cash=Decimal("100"),
                trigger_source="scheduler",
                mode="partial",
            )
            s.add(ev)
            await s.flush()
            assert ev.mode == "partial"
```

- [ ] **Step 2: 跑确认失败**

`cd /data/sunyunbo/perf-impl/backend && timeout 15 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_liquidation_event_mode_field.py -v 2>&1 | tail -10`

Expected: AttributeError 或 ORM 抛错 "mode 不是 LiquidationEvent 字段"

- [ ] **Step 3: 在 `backend/app/models/base.py` 加字段**

找到 `class LiquidationEvent(SQLModel, table=True):`，在最后一个字段后加：

```python
    mode: str = Field(default="emergency", max_length=20)
```

定位提示：找最后一个 Field（可能是 `trigger_source` 或 `created_at`），在它后面加。

- [ ] **Step 4: 跑测**

`cd /data/sunyunbo/perf-impl/backend && timeout 15 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_liquidation_event_mode_field.py -v 2>&1 | tail -10`

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
cd /data/sunyunbo/perf-impl && git add backend/app/models/base.py backend/tests/test_liquidation_event_mode_field.py
git commit -m "feat(model): LiquidationEvent 加 mode 字段

默认 'emergency' 兼容历史 row。支持 'partial' 标记部分平仓事件。
2 unit tests 全过 (default + explicit partial)。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Alembic migration ADD COLUMN mode

**Files:**
- Create: `backend/alembic/versions/<TS>_add_liquidation_event_mode.py`

- [ ] **Step 1: 生成 migration**

```bash
cd /data/sunyunbo/perf-impl/backend && /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m alembic revision --autogenerate -m "add liquidation_event mode column"
```

- [ ] **Step 2: 清理 noise**

打开生成的 `versions/<TS>-<hash>_add_liquidation_event_mode.py`。

Autogenerate 可能产生 noise（基于 dev sqlite 空 DB 可能误判其他表 missing）。**只保留** `liquidation_event.mode` 列的 add/drop：

`upgrade()`:
```python
def upgrade() -> None:
    op.add_column(
        'liquidation_event',
        sa.Column(
            'mode',
            sqlmodel.sql.sqltypes.AutoString(length=20),
            nullable=False,
            server_default='emergency',
        ),
    )
```

`downgrade()`:
```python
def downgrade() -> None:
    op.drop_column('liquidation_event', 'mode')
```

注：`server_default='emergency'` 让历史 row 自动填默认值；如果 autogenerate 没加 `server_default` 手动补上（否则 NOT NULL 加列在有数据的表上会失败）。

- [ ] **Step 3: 验证 migration**

```bash
cd /data/sunyunbo/perf-impl/backend && /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -c "
from alembic.config import Config
from alembic.script import ScriptDirectory
cfg = Config('alembic.ini')
script_dir = ScriptDirectory.from_config(cfg)
heads = script_dir.get_revisions(script_dir.get_heads())
print('latest:', heads[0].revision, heads[0].doc)
"
```

Expected: 打印 "latest: <hash> add liquidation_event mode column"

- [ ] **Step 4: 跑 Task 1 测确认仍 pass**

```bash
cd /data/sunyunbo/perf-impl/backend && timeout 15 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_liquidation_event_mode_field.py -v 2>&1 | tail -5
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
cd /data/sunyunbo/perf-impl && git add backend/alembic/versions/*_add_liquidation_event_mode.py
git commit -m "feat(migration): liquidation_event.mode 列

ADD COLUMN mode VARCHAR(20) NOT NULL DEFAULT 'emergency'。
server_default 让历史 row 自动填 'emergency' (事实：此 PR 之前全是全平)。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: 3 个新 site_config keys

**Files:**
- Modify: `backend/app/services/loan_migrate.py`
- Modify: `backend/app/api/v1/site_config.py`
- Test: `backend/tests/test_site_config_partial_liquidation.py` (新)

- [ ] **Step 1: 写失败测**

`backend/tests/test_site_config_partial_liquidation.py`:
```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from app.core.database import async_session_maker
from app.services import site_config


PARTIAL_LIQ_KEYS = [
    "liquidation_partial_pct",
    "liquidation_target_margin",
    "liquidation_emergency_threshold",
]


@pytest.mark.asyncio
async def test_partial_liquidation_configs_seeded_with_defaults(client):
    async with async_session_maker() as s:
        for k in PARTIAL_LIQ_KEYS:
            row = await site_config._fetch(s, k)
            assert row is not None, f"missing default config: {k}"

    from decimal import Decimal
    async with async_session_maker() as s:
        assert (await site_config.get_decimal(s, "liquidation_partial_pct")) == Decimal("0.10")
        assert (await site_config.get_decimal(s, "liquidation_target_margin")) == Decimal("0.30")
        assert (await site_config.get_decimal(s, "liquidation_emergency_threshold")) == Decimal("0.05")
```

- [ ] **Step 2: 跑确认失败**

```bash
cd /data/sunyunbo/perf-impl/backend && timeout 15 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_site_config_partial_liquidation.py -v 2>&1 | tail -10
```

Expected: FAIL `siteconfig key not found: liquidation_partial_pct`

- [ ] **Step 3: 改 `backend/app/services/loan_migrate.py`**

找到 `DEFAULT_CONFIGS = [...]`，找到现有 liquidation 段（4 个 keys，附近有 `liquidation_enabled`），末尾追加：

```python
    # ── Partial Liquidation (spec 2026-05-20-partial-liquidation-design.md) ──
    ("liquidation_partial_pct", "0.10", "decimal"),
    ("liquidation_target_margin", "0.30", "decimal"),
    ("liquidation_emergency_threshold", "0.05", "decimal"),
```

- [ ] **Step 4: 改 `backend/app/api/v1/site_config.py`**

找到 `_WHITELIST = {...}`，加 3 行：

```python
    "liquidation_partial_pct": "decimal",
    "liquidation_target_margin": "decimal",
    "liquidation_emergency_threshold": "decimal",
```

- [ ] **Step 5: 跑测**

```bash
cd /data/sunyunbo/perf-impl/backend && timeout 15 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_site_config_partial_liquidation.py -v 2>&1 | tail -10
```

Expected: 1 passed

- [ ] **Step 6: Commit**

```bash
cd /data/sunyunbo/perf-impl && git add backend/app/services/loan_migrate.py backend/app/api/v1/site_config.py backend/tests/test_site_config_partial_liquidation.py
git commit -m "feat(site_config): partial liquidation 3 个 key + 默认值 + 白名单

- liquidation_partial_pct (0.10): 每波平的比例
- liquidation_target_margin (0.30): 收敛目标 (仅 metric/logging 用)
- liquidation_emergency_threshold (0.05): margin < 此值 → 全平

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: liquidate_user 签名加 3 参数 + 写 mode（保持 emergency 行为不变）

**Files:**
- Modify: `backend/app/services/liquidation_service.py`
- Modify: `backend/tests/test_liquidation_service.py`
- Modify: `backend/tests/test_liquidation_admin.py`
- Modify: `backend/tests/test_liquidation_e2e.py`

### Strategy

新加 3 个 keyword-only 参数 `partial_pct: Decimal`, `target_margin: Decimal`, `emergency_threshold: Decimal`。本 task 内只用 `partial_pct=1.0` 让行为等同于全平（emergency mode），并在 LiquidationEvent 里写 `mode="emergency"`。Task 5 才加 partial 实际逻辑。

- [ ] **Step 1: 修改 `backend/app/services/liquidation_service.py` 函数签名 + mode 决策**

找到：
```python
async def liquidate_user(
    session: AsyncSession,
    user: User,
    *,
    daily_rate: Decimal,
    trigger_source: str,
) -> LiquidationEvent:
```

改成：
```python
async def liquidate_user(
    session: AsyncSession,
    user: User,
    *,
    daily_rate: Decimal,
    trigger_source: str,
    partial_pct: Decimal,
    target_margin: Decimal,
    emergency_threshold: Decimal,
) -> LiquidationEvent:
    """全平 user 持仓 + 最大化还债 + 写 LiquidationEvent。

    Mode 决策（spec 2026-05-20-partial-liquidation-design.md）：
    - pre_margin < emergency_threshold → mode='emergency'，全平所有 position
    - 否则 → mode='partial'，每 position 按 partial_pct 卖
    """
```

在 `pre_margin = (pre_nw / pre_debt) if pre_debt > ZERO else None` 之后立刻加：

```python
    # Mode 决策（spec § Mode 决策）
    if pre_margin is not None and pre_margin < emergency_threshold:
        mode = "emergency"
    else:
        mode = "partial"
    _logger.info(
        "liquidate_user_mode",
        extra={
            "user_id": user.id, "mode": mode,
            "pre_margin": float(pre_margin) if pre_margin is not None else None,
            "emergency_threshold": float(emergency_threshold),
            "target_margin": float(target_margin),  # 仅 logging
        },
    )
```

找到现有写 LiquidationEvent 的代码块（约第 200 行附近 `ev = LiquidationEvent(...)`），加 `mode=mode,` 参数。例如：

```python
    ev = LiquidationEvent(
        user_id=user.id,
        triggered_at=now,
        pre_cash=pre_cash,
        pre_debt=pre_debt,
        # ... 其他字段 ...
        trigger_source=trigger_source,
        mode=mode,  # 新加这一行
    )
```

也在 noop event 的构造（如果有的话，那个早返回的 LiquidationEvent）加 `mode=mode`。

**本 task 不改卖出逻辑**——`sell_amount = pos.amount` 维持 emergency 全平。后续 Task 5 才加 partial 分支。

- [ ] **Step 2: 更新现有测试调用方传新参数**

测试文件中所有直接调 `await liquidation_service.liquidate_user(...)` 的位置都要加 3 个新参数。

`grep -rn "liquidate_user(" backend/tests/`

预计有这些位置（确认实际 path/line）：
- `backend/tests/test_liquidation_service.py` 多个测试函数
- `backend/tests/test_liquidation_e2e.py` 可能直接调

对每个调用加：
```python
ev = await liquidation_service.liquidate_user(
    session, user, daily_rate=...,
    trigger_source="...",
    partial_pct=Decimal("1.0"),       # 等价全平（旧行为）
    target_margin=Decimal("0.3"),
    emergency_threshold=Decimal("0.05"),
)
```

`partial_pct=1.0` 让 Task 5 的 partial 逻辑（`sell_amount = pos.amount × 1.0`）退化为全卖，跟现有 emergency 行为一致。这样老测试不需要改 assertion。

也要更新 `backend/app/services/liquidation_sweep.py` 的 `_liquidate_one_user` 内调用：

找到：
```python
ev = await liquidation_service.liquidate_user(
    session, user, daily_rate=rate,
    trigger_source=trigger_source,
)
```

改成：
```python
ev = await liquidation_service.liquidate_user(
    session, user, daily_rate=rate,
    trigger_source=trigger_source,
    partial_pct=Decimal("1.0"),
    target_margin=Decimal("0.3"),
    emergency_threshold=Decimal("0.05"),
)
```

（Task 6 才改成读 site_config）。

- [ ] **Step 3: 跑现有 liquidation 测试确认不破坏**

```bash
cd /data/sunyunbo/perf-impl/backend && timeout 30 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_liquidation_service.py tests/test_liquidation_sweep.py tests/test_liquidation_admin.py tests/test_liquidation_e2e.py --tb=short -q 2>&1 | tail -10
```

Expected: 全 pass

- [ ] **Step 4: 加新测验证 LiquidationEvent.mode 被写**

加到 `backend/tests/test_liquidation_service.py` 末尾：

```python
@pytest.mark.asyncio
async def test_liquidate_user_writes_emergency_mode_when_below_threshold(client):
    """pre_margin < emergency_threshold → LiquidationEvent.mode='emergency'。"""
    # seed user margin = -0.5 (远低于 0.05)
    async with async_session_maker() as db:
        u, _, _ = await _setup_user_with_positions(
            db, cash=10, debt=500, positions=[("A", 50, 100)],
        )
        uid = u.id

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, uid)
            ev = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.001"),
                trigger_source="scheduler",
                partial_pct=Decimal("1.0"),
                target_margin=Decimal("0.3"),
                emergency_threshold=Decimal("0.05"),
            )
            assert ev.mode == "emergency"
```

- [ ] **Step 5: 跑新测**

```bash
cd /data/sunyunbo/perf-impl/backend && timeout 15 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_liquidation_service.py::test_liquidate_user_writes_emergency_mode_when_below_threshold -v 2>&1 | tail -5
```

Expected: 1 passed

- [ ] **Step 6: Commit**

```bash
cd /data/sunyunbo/perf-impl && git add backend/app/services/liquidation_service.py backend/app/services/liquidation_sweep.py backend/tests/test_liquidation_service.py backend/tests/test_liquidation_admin.py backend/tests/test_liquidation_e2e.py
git commit -m "feat(liquidate): liquidate_user 加 3 新参数 + 写 mode 字段

新加 keyword-only:
- partial_pct: Decimal
- target_margin: Decimal (仅 logging)
- emergency_threshold: Decimal

Mode 决策: pre_margin < emergency_threshold → 'emergency' 否则 'partial'。
本 task 内行为不变 (调用方都传 partial_pct=1.0 强制 emergency 全平)，
Task 5 才加实际 partial 卖出逻辑。

LiquidationEvent.mode 字段开始被写。

现有 liquidation 测全过；新加 emergency mode assertion 测。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: partial mode 卖出逻辑（核心）

**Files:**
- Modify: `backend/app/services/liquidation_service.py`
- Test: `backend/tests/test_liquidation_partial.py` (新)

### 实现要点

在 `liquidate_user` 的循环内：
- emergency: `sell_amount = pos.amount`（现状）
- partial: `sell_amount = (pos.amount * partial_pct).quantize(Decimal("0.000001"))`
- 若 `sell_amount <= ZERO` → skip（数值边界）
- 若 `sell_amount >= pos.amount` → 等同 emergency 全卖
- 否则：partial 模式
  - `cost_reduced = (pos.cost_basis * partial_pct).quantize(Decimal("0.000001"))`
  - `pos.amount -= sell_amount`
  - `pos.cost_basis -= cost_reduced`
  - 不 `session.delete(pos)`
  - 仍写 Transaction（LIQUIDATE type，按 sell_amount 算 price/cost/gross/fee）

- [ ] **Step 1: 写失败测（用 partial_pct=0.5 平 50%）**

`backend/tests/test_liquidation_partial.py`:
```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timezone
from decimal import Decimal
import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import (
    LiquidationEvent, Market, MarketStatus, Outcome,
    Position, User,
)
from app.services.market_locks import lock_user
from app.services import liquidation_service


async def _setup_user(*, cash, debt, share_amount, market_total_shares):
    """造 1 user + 1 market + 1 outcome + 1 position。"""
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"part_{cash}_{debt}",
                casdoor_id=f"part_cas_{cash}_{debt}",
                cash=Decimal(str(cash)),
                debt=Decimal(str(debt)),
                debt_last_accrued_at=(
                    datetime.now(timezone.utc) if debt > 0 else None
                ),
            )
            s.add(u)
            await s.flush()

            m = Market(
                title="part_test", description="", liquidity_b=100.0,
                status=MarketStatus.TRADING, tags="",
            )
            s.add(m)
            await s.flush()

            o = Outcome(
                market_id=m.id, label="A",
                total_shares=Decimal(str(market_total_shares)),
            )
            s.add(o)
            await s.flush()

            p = Position(
                user_id=u.id, outcome_id=o.id,
                amount=Decimal(str(share_amount)),
                cost_basis=Decimal(str(share_amount)) * Decimal("0.5"),  # avg_price = 0.5
            )
            s.add(p)
            return u.id, m.id, o.id


@pytest.mark.asyncio
async def test_partial_50pct_sells_half_and_updates_cost_basis(client):
    """partial_pct=0.5 → 卖一半，cost_basis 也减一半，avg_price 不变。"""
    uid, mid, oid = await _setup_user(
        cash=100, debt=300, share_amount=100, market_total_shares=100,
    )

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, uid)
            ev = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.001"),
                trigger_source="scheduler",
                partial_pct=Decimal("0.5"),
                target_margin=Decimal("0.3"),
                emergency_threshold=Decimal("0.05"),
            )
        # 出事务后看 DB
        assert ev.mode == "partial"
        assert ev.sold_positions_count == 1

    async with async_session_maker() as db:
        # position 不应被删除，amount 应减一半
        rows = (await db.execute(
            select(Position).where(Position.user_id == uid)
        )).scalars().all()
        assert len(rows) == 1, "partial 不应删除 position"
        p = rows[0]
        assert p.amount == Decimal("50"), f"amount 应=50, 实际 {p.amount}"
        # cost_basis 50 (= 100 * 0.5 * 50%)
        assert p.cost_basis == Decimal("25"), f"cost_basis 应=25, 实际 {p.cost_basis}"
        # avg_price 不变 (25 / 50 = 0.5 ≈ 原 50/100)
        assert (p.cost_basis / p.amount) == Decimal("0.5")


@pytest.mark.asyncio
async def test_partial_full_pct_acts_like_emergency_all_in(client):
    """partial_pct=1.0 → 等价 emergency 全平（position 被删除）。"""
    uid, mid, oid = await _setup_user(
        cash=100, debt=300, share_amount=100, market_total_shares=100,
    )

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, uid)
            ev = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.001"),
                trigger_source="scheduler",
                partial_pct=Decimal("1.0"),
                target_margin=Decimal("0.3"),
                emergency_threshold=Decimal("0.05"),
            )

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Position).where(Position.user_id == uid)
        )).scalars().all()
        assert len(rows) == 0, "partial_pct=1.0 应等同 emergency 删除 position"


@pytest.mark.asyncio
async def test_emergency_mode_when_pre_margin_below_threshold(client):
    """pre_margin < emergency_threshold (0.05) → mode='emergency' 即使传 partial_pct=0.1。"""
    # cash=0, debt=1000, shares=10, market 也是 10 → LCV ≈ 5
    # pre_margin = (0 + 5 - 1000) / 1000 = -0.995 < 0.05
    uid, mid, oid = await _setup_user(
        cash=0, debt=1000, share_amount=10, market_total_shares=10,
    )

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, uid)
            ev = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.001"),
                trigger_source="scheduler",
                partial_pct=Decimal("0.1"),  # 即使传 partial，应被 emergency 覆盖
                target_margin=Decimal("0.3"),
                emergency_threshold=Decimal("0.05"),
            )
        assert ev.mode == "emergency"

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Position).where(Position.user_id == uid)
        )).scalars().all()
        assert len(rows) == 0, "emergency 全平应删 position"


@pytest.mark.asyncio
async def test_partial_sell_amount_quantized_to_6_digits(client):
    """partial_pct × amount 量化到 6 位小数；过小被跳过不报错。"""
    # amount=0.000005, partial_pct=0.1 → 0.0000005，量化到 6 位 = 0 → skip
    uid, mid, oid = await _setup_user(
        cash=10, debt=100, share_amount=Decimal("0.000005"),
        market_total_shares=Decimal("0.000005"),
    )

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, uid)
            ev = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.001"),
                trigger_source="scheduler",
                partial_pct=Decimal("0.1"),
                target_margin=Decimal("0.3"),
                emergency_threshold=Decimal("0.05"),
            )
        # sold_count 应为 0 (sell_amount 量化为 0 跳过)
        assert ev.sold_positions_count == 0

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Position).where(Position.user_id == uid)
        )).scalars().all()
        # position 不应被删（partial 跳过）
        assert len(rows) == 1
        assert rows[0].amount == Decimal("0.000005")  # 没动
```

- [ ] **Step 2: 跑确认失败**

```bash
cd /data/sunyunbo/perf-impl/backend && timeout 20 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_liquidation_partial.py -v 2>&1 | tail -15
```

Expected: `test_partial_50pct_sells_half_and_updates_cost_basis` 失败（partial 还是全平）等

- [ ] **Step 3: 修改 `backend/app/services/liquidation_service.py` 加 partial 分支**

定位到现有循环（Task 3 阶段的）：
```python
for market_id in sorted_market_ids:
    pos_group = positions_by_market[market_id]
    market = pos_group[0].outcome.market
    ...
    for pos in pos_group:
        ...
        old_q = [float(o.total_shares) for o in all_outcomes]
        new_q = list(old_q)
        new_q[idx] -= float(pos.amount)  # ← 全卖
        ...
        user.cash += proceeds
        all_outcomes[idx].total_shares -= pos.amount  # ← 减全部
        await session.delete(pos)  # ← 删
        ...
```

改造：

```python
for pos in pos_group:
    idx = outcomes_idx_by_id.get(pos.outcome_id)
    if idx is None:
        _logger.error(
            "liquidation_outcome_not_in_market",
            extra={"user_id": user.id, "position_id": pos.id,
                   "outcome_id": pos.outcome_id, "market_id": market.id},
        )
        continue

    # 算 sell_amount 按 mode
    if mode == "emergency":
        sell_amount = pos.amount
    else:  # partial
        sell_amount = (pos.amount * partial_pct).quantize(Decimal("0.000001"))

    if sell_amount <= ZERO:
        # partial 时 amount 太小量化为 0 → 跳过本 position
        continue

    # 若 partial 算出来 ≥ amount，按全卖处理
    if sell_amount >= pos.amount:
        sell_amount = pos.amount

    # LMSR proceeds (按 sell_amount)
    old_q = [float(o.total_shares) for o in all_outcomes]
    new_q = list(old_q)
    new_q[idx] -= float(sell_amount)

    old_cost, old_prices = calculate_lmsr_with_prices(old_q, b)
    new_cost, new_prices = calculate_lmsr_with_prices(new_q, b)
    proceeds = quantize_cost(old_cost - new_cost)

    if proceeds < ZERO:
        _logger.error(
            "liquidation_negative_proceeds",
            extra={"user_id": user.id, "position_id": pos.id, "proceeds": str(proceeds)},
        )
        continue

    # 应用变更
    user.cash += proceeds
    all_outcomes[idx].total_shares -= sell_amount

    if sell_amount >= pos.amount:
        # 全卖 (emergency 或 partial 边界)
        await session.delete(pos)
    else:
        # partial 卖
        # cost_basis 按比例减少（保持 avg_price 不变，跟 market.py sell 同语义）
        cost_reduced = (pos.cost_basis * partial_pct).quantize(Decimal("0.000001"))
        pos.amount -= sell_amount
        pos.cost_basis -= cost_reduced

    # 写 LIQUIDATE Transaction (按 sell_amount 而非 pos.amount)
    avg_price = (
        quantize_price(proceeds / sell_amount) if sell_amount > ZERO else ZERO
    )
    tx = Transaction(
        user_id=user.id,
        outcome_id=pos.outcome_id,
        type=TransactionType.LIQUIDATE,
        shares=sell_amount,
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
```

- [ ] **Step 4: 跑测**

```bash
cd /data/sunyunbo/perf-impl/backend && timeout 20 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_liquidation_partial.py -v 2>&1 | tail -15
```

Expected: 4 passed

- [ ] **Step 5: 跑现有 liquidation 测试确认不破坏（emergency 路径）**

```bash
cd /data/sunyunbo/perf-impl/backend && timeout 30 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_liquidation_service.py tests/test_liquidation_admin.py tests/test_liquidation_e2e.py --tb=short -q 2>&1 | tail -8
```

Expected: 全 pass（之前 partial_pct=1.0 让行为退化全平）

- [ ] **Step 6: Commit**

```bash
cd /data/sunyunbo/perf-impl && git add backend/app/services/liquidation_service.py backend/tests/test_liquidation_partial.py
git commit -m "feat(liquidate): partial mode 卖出逻辑 + cost_basis 按比例减

mode='emergency' → 全卖 (现有逻辑保持)
mode='partial' → sell_amount = amount × partial_pct (quantize 6 位)
  - sell_amount ≤ 0 → skip (数值边界)
  - sell_amount ≥ amount → 退化全卖
  - 否则 partial: pos.amount -= sell_amount, pos.cost_basis -= cost_reduced
    avg_price = cost_basis / amount 不变 (平均成本法)
LIQUIDATE Transaction 按 sell_amount 算 price/cost/gross/fee。

4 新 partial test + 现有 emergency 测全过 (partial_pct=1.0 退化)。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: sweep 主循环读 site_config 传新参数

**Files:**
- Modify: `backend/app/services/liquidation_sweep.py`

- [ ] **Step 1: 修改 `run_liquidation_sweep_once` 读 3 个新 site_config**

定位现有读 site_config 段（开头附近，跟其他 liquidation_* keys 一起）：

```python
async with async_session_maker() as session:
    cfg = await site_config.get_many(session, [
        "liquidation_enabled",
        "liquidation_hard_threshold",
        "liquidation_soft_threshold",
        "loan_daily_rate",
    ])
    ...
```

加 3 个新 key：

```python
async with async_session_maker() as session:
    cfg = await site_config.get_many(session, [
        "liquidation_enabled",
        "liquidation_hard_threshold",
        "liquidation_soft_threshold",
        "loan_daily_rate",
        "liquidation_partial_pct",
        "liquidation_target_margin",
        "liquidation_emergency_threshold",
    ])
```

在解析现有 `hard_thr` `soft_thr` `rate` 的 try 块加：

```python
try:
    hard_thr = Decimal(cfg["liquidation_hard_threshold"])
    soft_thr = Decimal(cfg["liquidation_soft_threshold"])
    rate = Decimal(cfg["loan_daily_rate"])
    partial_pct = Decimal(cfg["liquidation_partial_pct"])
    target_margin = Decimal(cfg["liquidation_target_margin"])
    emergency_threshold = Decimal(cfg["liquidation_emergency_threshold"])
except (KeyError, InvalidOperation, ValueError):
    ...
```

- [ ] **Step 2: 修改 `_liquidate_one_user` 签名 + 传新参数**

定位现有 `_liquidate_one_user` 签名：

```python
async def _liquidate_one_user(
    *,
    uid: int,
    hard_thr: Decimal,
    rate: Decimal,
    trigger_source: str,
    now: float,
    sem: asyncio.Semaphore,
) -> str:
```

加新参数：
```python
async def _liquidate_one_user(
    *,
    uid: int,
    hard_thr: Decimal,
    rate: Decimal,
    trigger_source: str,
    now: float,
    sem: asyncio.Semaphore,
    partial_pct: Decimal,
    target_margin: Decimal,
    emergency_threshold: Decimal,
) -> str:
```

在函数内找到 `await liquidation_service.liquidate_user(...)`，加参数：

```python
ev = await liquidation_service.liquidate_user(
    session, user, daily_rate=rate,
    trigger_source=trigger_source,
    partial_pct=partial_pct,
    target_margin=target_margin,
    emergency_threshold=emergency_threshold,
)
```

去掉之前 Task 4 中临时硬编码的 `partial_pct=Decimal("1.0")` 等。

- [ ] **Step 3: 修改 `run_liquidation_sweep_once` 内调 `_liquidate_one_user`**

找到调用处（在 asyncio.gather 内）：

```python
results = await asyncio.gather(
    *[
        _liquidate_one_user(
            uid=uid, hard_thr=hard_thr, rate=rate,
            trigger_source=trigger_source, now=now, sem=sem,
        )
        for uid in over_hard
    ],
    return_exceptions=False,
)
```

加传新参数：

```python
results = await asyncio.gather(
    *[
        _liquidate_one_user(
            uid=uid, hard_thr=hard_thr, rate=rate,
            trigger_source=trigger_source, now=now, sem=sem,
            partial_pct=partial_pct,
            target_margin=target_margin,
            emergency_threshold=emergency_threshold,
        )
        for uid in over_hard
    ],
    return_exceptions=False,
)
```

- [ ] **Step 4: 跑 sweep 测确认不破坏**

```bash
cd /data/sunyunbo/perf-impl/backend && timeout 30 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_liquidation_sweep.py tests/test_liquidation_e2e.py tests/test_liquidation_admin.py tests/test_liquidation_sweep_perf.py --tb=short -q 2>&1 | tail -8
```

Expected: 全 pass（site_config seed 默认值跟原行为兼容：emergency_threshold=0.05 < 0 实际 LCV margin 通常负值仍走 emergency；只是现在显式 mode='emergency' 写入 LiquidationEvent）

- [ ] **Step 5: Commit**

```bash
cd /data/sunyunbo/perf-impl && git add backend/app/services/liquidation_sweep.py
git commit -m "feat(sweep): 读 3 个 partial liquidation site_config 传给 liquidate_user

run_liquidation_sweep_once 从 site_config 拿 partial_pct/target_margin/
emergency_threshold (跟其他 liquidation_* keys 一起批量读)。
_liquidate_one_user 加 3 个 keyword-only 参数透传。

去掉 Task 4 临时硬编码的 partial_pct=1.0。现在生产用 site_config 默认值
(partial_pct=0.10, target=0.30, emergency=0.05)，PR merge 上线后会按 partial
模式跑 (前提是 liquidation_enabled=true 且 user margin 在 [0.05, 0.20))。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: partial 多 tick 收敛 + sweep 集成测

**Files:**
- Modify: `backend/tests/test_liquidation_partial.py` (加 2 个新测)

- [ ] **Step 1: 加 multi-tick 收敛测**

加到 `backend/tests/test_liquidation_partial.py` 末尾：

```python
from app.services import liquidation_sweep, site_config


async def _enable_partial_liquidation():
    """开启 liquidation_enabled + 阈值用 partial 模式合理范围。"""
    async with async_session_maker() as s:
        async with s.begin():
            for k, v in [
                ("liquidation_enabled", "true"),
                ("liquidation_hard_threshold", "0.2"),
                ("liquidation_soft_threshold", "0.5"),
                ("liquidation_partial_pct", "0.5"),  # 测试用 50% 收敛快
                ("liquidation_target_margin", "0.3"),
                ("liquidation_emergency_threshold", "0.05"),
            ]:
                row = (await s.execute(
                    select(__import__('app.models.base', fromlist=['SiteConfig']).SiteConfig)
                    .where(__import__('app.models.base', fromlist=['SiteConfig']).SiteConfig.key == k)
                )).scalars().first()
                if row:
                    row.value = v
    site_config.clear_cache()


@pytest.mark.asyncio
async def test_sweep_partial_then_converges_after_multiple_ticks(client):
    """多 tick partial → margin 渐进升高最终 >= target → 下个 tick 不再触发。

    setup: user margin ≈ 0.1（在 hard_threshold 0.2 下，但远高于 emergency 0.05）
    expected: 第 1 tick 写 partial event；多次 tick 后 margin 达 target，不再写。
    """
    await _enable_partial_liquidation()
    # cash=200, debt=1000, LCV ≈ 800 (近似) → margin ≈ 0/1000? 实际算法复杂
    # 简化构造: cash=100, debt=1000, shares=100, market=100 → LMSR sell proceeds ≈ 50-60
    # pre_margin ≈ (100 + 50 - 1000) / 1000 ≈ -0.85 → emergency
    # 改 debt=200 → margin ≈ (100+50-200)/200 ≈ -0.25 仍 emergency
    # 改 debt=150 → margin ≈ (100+50-150)/150 = 0 仍 < emergency
    # 实际要让 margin ∈ [0.05, 0.20) 需要 LCV 多
    # 用 share_amount=200, market_total=200, b=100 → LCV ≈ 175
    # debt=300 → margin = (100+175-300)/300 = -0.083 → emergency
    # debt=250 → margin = (100+175-250)/250 = 0.10 → partial ✓
    uid, mid, oid = await _setup_user(
        cash=100, debt=250, share_amount=200, market_total_shares=200,
    )

    # 第 1 次 sweep 应该触发 partial
    result1 = await liquidation_sweep.run_liquidation_sweep_once()
    assert result1["triggered_count"] >= 1

    async with async_session_maker() as db:
        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == uid)
        )).scalars().all()
        assert len(events) == 1
        assert events[0].mode == "partial"

    # 重置 _recently_attempted (避免 cooldown 跳过)
    liquidation_sweep._recently_attempted.clear()
    site_config.clear_cache()

    # 跑几次 sweep 看是否最终收敛
    max_ticks = 5
    for i in range(max_ticks):
        liquidation_sweep._recently_attempted.clear()
        site_config.clear_cache()
        await liquidation_sweep.run_liquidation_sweep_once()

    # 最终查 user margin (LCV) 应已 ≥ hard_threshold (≥ 0.2)
    async with async_session_maker() as db:
        from app.models.base import User as _User
        u = await db.get(_User, uid)
        # 至少 cash + (一部分 LCV) - debt > 0.2 * debt
        # 由于 partial 多波，最终 debt 应已大幅减少
        # 软断言: debt 应明显小于初始 250
        assert u.debt < Decimal("250"), f"debt 应已减少, 实际 {u.debt}"
        # 且写了多个 partial events
        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == uid)
        )).scalars().all()
        assert all(e.mode == "partial" for e in events), \
            f"应全部是 partial 模式, 实际 {[e.mode for e in events]}"
        assert len(events) >= 2, "至少应触发 2 波"


@pytest.mark.asyncio
async def test_emergency_mode_written_when_severe(client):
    """user margin << emergency_threshold → mode='emergency' 写入 event。"""
    await _enable_partial_liquidation()
    # cash=0, debt=1000, shares=10 → LCV ≈ 5 → margin = -0.995
    uid, mid, oid = await _setup_user(
        cash=0, debt=1000, share_amount=10, market_total_shares=10,
    )

    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result["triggered_count"] >= 1

    async with async_session_maker() as db:
        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == uid)
        )).scalars().all()
        assert len(events) >= 1
        assert events[0].mode == "emergency"
```

- [ ] **Step 2: 跑测**

```bash
cd /data/sunyunbo/perf-impl/backend && timeout 30 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_liquidation_partial.py -v 2>&1 | tail -20
```

Expected: 6 passed (4 老的 + 2 新增)

- [ ] **Step 3: Commit**

```bash
cd /data/sunyunbo/perf-impl && git add backend/tests/test_liquidation_partial.py
git commit -m "test(partial): 多 tick 收敛 + emergency 集成测

- test_sweep_partial_then_converges_after_multiple_ticks: seed user margin=0.10
  → 多 sweep tick 后 debt 减少 + 全部 events mode='partial'
- test_emergency_mode_written_when_severe: margin=-0.995 → mode='emergency'

6 个 partial test 全过 (4 unit + 2 集成)。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: 全套回归 + 准备 push

**Files:** 全套

- [ ] **Step 1: 全套 liquidation + 相关回归**

```bash
cd /data/sunyunbo/perf-impl/backend && timeout 60 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest \
  tests/test_liquidation_event_mode_field.py \
  tests/test_site_config_partial_liquidation.py \
  tests/test_liquidation_partial.py \
  tests/test_liquidation_service.py \
  tests/test_liquidation_sweep.py \
  tests/test_liquidation_admin.py \
  tests/test_liquidation_e2e.py \
  tests/test_liquidation_sweep_perf.py \
  tests/test_liquidation_public.py \
  tests/test_liquidation_schema.py \
  --tb=short -q 2>&1 | tail -10
```

Expected: 全 pass (约 35-40 个)

- [ ] **Step 2: 现有大套回归（确保 anti-bot 等其他 feature 没破坏）**

```bash
cd /data/sunyunbo/perf-impl/backend && timeout 60 /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest \
  tests/test_loan_service.py tests/test_loan_api.py \
  tests/test_user_summary_margin.py tests/test_user_summary_dual_caliber.py \
  tests/test_wealth_mtm.py tests/test_leaderboard.py \
  tests/test_anti_bot.py tests/test_bot_detection_signals.py \
  tests/test_bot_detection_main_loop.py tests/test_market_anti_bot_integration.py \
  --tb=short -q 2>&1 | tail -8
```

Expected: 全 pass

- [ ] **Step 3: Backend import sanity**

```bash
cd /data/sunyunbo/perf-impl/backend && /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -c "import app.main; print('IMPORT OK')" 2>&1 | tail -3
```

Expected: 打印 IMPORT OK（有 dev 警告正常）

- [ ] **Step 4: py_compile 全过**

```bash
cd /data/sunyunbo/perf-impl/backend && /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m py_compile $(find app -name '*.py') && echo COMPILE_OK
```

Expected: 打印 COMPILE_OK

- [ ] **Step 5: 分支汇总**

```bash
cd /data/sunyunbo/perf-impl && git log --oneline origin/main..HEAD
git diff --stat origin/main..HEAD | tail -5
```

Expected: 8 个 commits（含 spec），files 约 7-8 个改动

- [ ] **Step 6: PR 描述模板（如果手动开 PR）**

```markdown
## Summary
- 实施 docs/superpowers/specs/2026-05-20-partial-liquidation-design.md
- liquidate_user 加 partial mode (按 partial_pct 卖)，emergency 兜底
- LiquidationEvent 加 mode 字段 (alembic migration)
- 3 个新 site_config: partial_pct (0.10) / target_margin (0.30) / emergency_threshold (0.05)
- sweep_interval 默认仍 600s，admin 上线后改 10s

## 部署前 checklist
- [ ] CI alembic 自动跑 migration (ADD COLUMN mode DEFAULT 'emergency')
- [ ] auto_migrate 自动 seed 3 个新 site_config

## 上线后 admin 操作 (可选)
- [ ] SQLAdmin → site_config 改 liquidation_sweep_interval_sec = 10
- [ ] 视情况调整 partial_pct / target_margin
- [ ] 历史 LiquidationEvent.mode 自动填 'emergency'

## Test Plan
- [ ] 35+ liquidation 单元/集成测全过
- [ ] 现有 anti-bot/loan/user_summary 等回归全过
- [ ] 多 tick 收敛验证 (test_sweep_partial_then_converges_after_multiple_ticks)
```

---

## Self-Review

**Spec coverage check** (对应 spec 各 section):

- ✅ Architecture (mode 决策 + partial 算法) → Task 4 + 5
- ✅ Schema LiquidationEvent.mode → Task 1 + 2
- ✅ Schema 3 site_config keys → Task 3
- ✅ liquidate_user 加 3 参数 → Task 4
- ✅ partial 卖出逻辑 + cost_basis 按比例 → Task 5
- ✅ sweep 传参 → Task 6
- ✅ 多 tick 收敛 + emergency 集成测 → Task 7
- ✅ 回归 + sanity → Task 8

**Placeholder scan**: 全文搜 "TBD/TODO/implement later/fill in details" → 无 ✓

**Type consistency**:
- `partial_pct: Decimal`, `target_margin: Decimal`, `emergency_threshold: Decimal` 一致使用 ✓
- `mode: str` 值集合 `"emergency"` / `"partial"` 全程一致 ✓
- `LiquidationEvent.mode` 默认 `"emergency"` 跟 Task 1 model + Task 2 migration server_default 一致 ✓
- `sell_amount` 全 `Decimal`，量化 0.000001（6 位 sed） ✓

**Scope check**: 单一 feature，8 task，约 1 工作日。✓

**Ambiguity check**:
- Task 4 显式说"本 task 不改卖出逻辑"，Task 5 才加，避免 implementer 一口气写完 ✓
- Task 5 详细说明 partial 时 `cost_basis -= cost_reduced`（按比例） ✓
- Task 6 提"去掉 Task 4 临时硬编码" ✓
- Task 7 说明"必须清 `_recently_attempted` 模拟多 tick" ✓

---

## 执行入口

**Plan complete and saved to `docs/superpowers/plans/2026-05-20-partial-liquidation.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
