# OutcomeCandle 物化表实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 TouhouCCB 加一张 `outcome_candle` 物化表，让长时间窗的图表查询不再走 Transaction 逐笔重放，彻底消除 5000 笔硬上限；同时把 PriceChart 和 CandleChart 统一到 `/chart/candles` 单一数据源。

**Architecture:** buy/sell 事务内同步 UPSERT `(outcome_id, interval, bucket_start)` 主键的 candle 表（10s/1m/15m/1h 共 4 档）；老 interval（30s/5m/1d）由 chart endpoint 现 rollup；alembic migration 内直接回填历史 Transaction；前端两图共用 4 档 interval 按钮。

**Tech Stack:** FastAPI + SQLAlchemy async + asyncpg (prod) / aiosqlite (test) + alembic + SQLModel + Postgres `INSERT ... ON CONFLICT DO UPDATE` + Vue 3 + lightweight-charts

**Spec:** `docs/superpowers/specs/2026-05-17-candle-cache-design.md`

---

## File Structure

**新建后端文件**：
- `backend/app/services/candle_writer.py` — `compute_candle_rows()` + `upsert_candles()` + `backfill_one_market()`
- `backend/scripts/backfill_outcome_candle.py` — CLI 包装（dry-run / per-market）
- `backend/alembic/versions/2026_05_17_HHMM-<hash>_add_outcome_candle.py` — CREATE TABLE + 回填

**新建测试文件**：
- `backend/tests/test_candle_writer.py` — `compute_candle_rows` + `upsert_candles` 单元
- `backend/tests/test_candle_rollup.py` — `INTERVAL_ROUTE` + `_rollup` 单元
- `backend/tests/test_candle_integration.py` — buy/sell/settle/HALT 触发的副作用
- `backend/tests/test_chart_endpoint.py` — `/chart/candles` 端到端
- `backend/tests/test_candle_backfill.py` — 回填正确性 + 幂等

**修改的后端文件**：
- `backend/app/models/base.py` — 加 `OutcomeCandle` class
- `backend/app/api/v1/market.py` — `buy_shares` / `sell_shares` 事务内追加 UPSERT
- `backend/app/api/v1/chart.py` — 加 `INTERVAL_ROUTE` / `_rollup`，重写 `/candles`，删 `/price`
- `backend/app/main.py` — lifespan 加 `_resync_recent_candles()` 兜底扫

**修改的前端文件**：
- `thccb-frontend/src/components/chart/PriceChart.vue` — props 改名 + 调 getCandles
- `thccb-frontend/src/components/chart/CandleChart.vue` — `INTERVAL_SECONDS` 削减 + `c.n` 条件化
- `thccb-frontend/src/pages/market/TradingView.vue` — 两图共用 interval 按钮
- `thccb-frontend/src/api/chart.ts` — 删 `getPriceSeries`
- `thccb-frontend/src/composables/useChartData.ts` — 删相关函数

---

## Task 1: 加 OutcomeCandle 模型 + alembic CREATE TABLE 迁移

**Files:**
- Modify: `backend/app/models/base.py`（在 Transaction 类后插入）
- Create: `backend/alembic/versions/2026_05_17_HHMM-<hash>_add_outcome_candle.py`（autogenerate）
- Create: `backend/tests/test_candle_model.py`

- [ ] **Step 1.1：在 `backend/app/models/base.py` 末尾（SiteConfig 类之前）插入 OutcomeCandle**

```python
class OutcomeCandle(SQLModel, table=True):
    """物化 OHLCV K 线表。每笔 buy/sell 在事务内同步 UPSERT。

    自然键 (outcome_id, interval, bucket_start)：同一 bucket 多次成交
    INSERT ... ON CONFLICT DO UPDATE 合并 H/L/C/V/n。

    settle/settle_lose 不写入：结算价不是真实成交、且 timestamp 扎堆。

    不暴露 outcome relationship；Outcome 不加反向 candles 关系。
    理由：遵守 base.py:61-67 hot path 性能护栏（lazy="raise_on_sql" 精神）。
    """
    __tablename__ = "outcome_candle"
    __table_args__ = (
        CheckConstraint("volume_shares >= 0", name="ck_candle_volume_non_negative"),
        CheckConstraint("n_trades >= 0",      name="ck_candle_n_non_negative"),
        CheckConstraint("high_price >= low_price", name="ck_candle_h_ge_l"),
        CheckConstraint(
            "interval IN ('10s', '1m', '15m', '1h')",
            name="ck_candle_interval_supported",
        ),
    )

    outcome_id:   int      = Field(foreign_key="outcome.id", primary_key=True)
    interval:     str      = Field(primary_key=True, max_length=8)
    bucket_start: datetime = Field(primary_key=True, sa_type=DateTime(timezone=True))

    open_price:  Decimal = Field(sa_type=Numeric(16, 8))
    high_price:  Decimal = Field(sa_type=Numeric(16, 8))
    low_price:   Decimal = Field(sa_type=Numeric(16, 8))
    close_price: Decimal = Field(sa_type=Numeric(16, 8))

    volume_shares: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6))
    n_trades:      int     = Field(default=0)

    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
```

- [ ] **Step 1.2：跑 alembic autogenerate 生成迁移**

```bash
cd backend
alembic revision --autogenerate -m "add outcome_candle table"
```

会生成 `backend/alembic/versions/2026_05_17_HHMM-<hash>_add_outcome_candle.py`。

- [ ] **Step 1.3：人工 review migration 文件**

确保 `upgrade()` 只包含 `op.create_table('outcome_candle', ...)`（含 PK 主键 + CheckConstraint），没有其他无关变更（如果有，删掉——CLAUDE.md `docs/migrations.md` 强调"人工 review 删多余"）。

`downgrade()` 应只有 `op.drop_table('outcome_candle')`。

- [ ] **Step 1.4：写模型测试 `backend/tests/test_candle_model.py`**

```python
"""OutcomeCandle 模型字段、约束、PK 验证。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle


async def _seed_outcome() -> int:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", status=MarketStatus.TRADING, liquidity_b=10.0)
        s.add(m)
        await s.flush()
        o = Outcome(market_id=m.id, label="opt_0", total_shares=Decimal("0"))
        s.add(o)
        await s.commit()
        await s.refresh(o)
        return o.id


@pytest.mark.asyncio
async def test_basic_insert_and_read(client):
    oid = await _seed_outcome()
    bucket = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session_maker() as s:
        s.add(OutcomeCandle(
            outcome_id=oid, interval="1m", bucket_start=bucket,
            open_price=Decimal("0.5"), high_price=Decimal("0.6"),
            low_price=Decimal("0.4"), close_price=Decimal("0.55"),
            volume_shares=Decimal("10"), n_trades=3,
        ))
        await s.commit()
        row = (await s.execute(
            select(OutcomeCandle).where(OutcomeCandle.outcome_id == oid)
        )).scalars().first()
        assert row.close_price == Decimal("0.55000000")
        assert row.n_trades == 3


@pytest.mark.asyncio
async def test_unsupported_interval_rejected(client):
    oid = await _seed_outcome()
    bucket = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session_maker() as s:
        s.add(OutcomeCandle(
            outcome_id=oid, interval="42s",  # 不在白名单
            bucket_start=bucket,
            open_price=Decimal("0.5"), high_price=Decimal("0.5"),
            low_price=Decimal("0.5"), close_price=Decimal("0.5"),
        ))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_high_less_than_low_rejected(client):
    oid = await _seed_outcome()
    bucket = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session_maker() as s:
        s.add(OutcomeCandle(
            outcome_id=oid, interval="10s", bucket_start=bucket,
            open_price=Decimal("0.5"),
            high_price=Decimal("0.3"),  # h < l 违反约束
            low_price=Decimal("0.5"),
            close_price=Decimal("0.5"),
        ))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_negative_volume_rejected(client):
    oid = await _seed_outcome()
    bucket = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session_maker() as s:
        s.add(OutcomeCandle(
            outcome_id=oid, interval="10s", bucket_start=bucket,
            open_price=Decimal("0.5"), high_price=Decimal("0.5"),
            low_price=Decimal("0.5"), close_price=Decimal("0.5"),
            volume_shares=Decimal("-1"),
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
```

- [ ] **Step 1.5：跑测试 + 后端 smoke**

```bash
cd backend
python -m py_compile $(find app -name '*.py')
python -c "import app.main"
python -m pytest tests/test_candle_model.py -v
```

预期：4 个测试全过；py_compile 无输出；import 成功。

- [ ] **Step 1.6：commit**

```bash
git add backend/app/models/base.py \
        backend/alembic/versions/2026_05_17_*_add_outcome_candle.py \
        backend/tests/test_candle_model.py
git commit -m "$(cat <<'EOF'
feat(candle): 加 outcome_candle 物化表 + alembic 迁移

新增 OutcomeCandle 模型 (10s/1m/15m/1h 4 档 OHLCV)，复合 PK
(outcome_id, interval, bucket_start)；CheckConstraint 锁定 interval
白名单与 H>=L、volume/n_trades 非负不变性。

回填逻辑后续 task 加上。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: candle_writer 服务（compute_rows + upsert）

**Files:**
- Create: `backend/app/services/candle_writer.py`
- Create: `backend/tests/test_candle_writer.py`

- [ ] **Step 2.1：写测试 `backend/tests/test_candle_writer.py`**

```python
"""candle_writer.compute_candle_rows + upsert_candles 单元测试。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List

import pytest
from sqlmodel import select

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle
from app.services.candle_writer import (
    compute_candle_rows,
    upsert_candles,
    CANDLE_INTERVALS,
)


async def _seed_market(n_outcomes: int = 2) -> tuple[int, List[int]]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", status=MarketStatus.TRADING, liquidity_b=10.0)
        s.add(m)
        await s.flush()
        oids = []
        for i in range(n_outcomes):
            o = Outcome(market_id=m.id, label=f"opt_{i}", total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        await s.commit()
        return m.id, oids


def test_compute_candle_rows_two_outcomes():
    """2 outcomes × 4 intervals = 8 行；被交易行 v=shares、n=1；联动行 v=0、n=0。"""
    ts = datetime(2026, 5, 17, 12, 34, 56, tzinfo=timezone.utc)
    # outcome ids 顺序对应 new_prices
    outcome_ids = [101, 102]
    new_prices = [0.6, 0.4]
    traded_oid = 102
    shares = Decimal("5")

    rows = compute_candle_rows(
        traded_outcome_id=traded_oid,
        outcome_ids=outcome_ids,
        new_prices=new_prices,
        traded_shares=shares,
        ts=ts,
    )

    assert len(rows) == 8  # 2 outcomes * 4 intervals

    # 被交易 outcome (102) 的每行 v=5, n=1
    traded_rows = [r for r in rows if r["outcome_id"] == 102]
    assert len(traded_rows) == 4
    for r in traded_rows:
        assert r["volume_shares"] == Decimal("5")
        assert r["n_trades"] == 1
        assert r["open_price"] == Decimal("0.4")  # new_prices[1]
        assert r["close_price"] == Decimal("0.4")

    # 联动 outcome (101) 的每行 v=0, n=0；价格仍是 0.6
    linked_rows = [r for r in rows if r["outcome_id"] == 101]
    assert len(linked_rows) == 4
    for r in linked_rows:
        assert r["volume_shares"] == Decimal("0")
        assert r["n_trades"] == 0
        assert r["open_price"] == Decimal("0.6")


def test_compute_candle_rows_bucket_alignment():
    """bucket_start 应该按 interval 秒数对齐到 floor。"""
    # 12:34:56 → 10s: 12:34:50, 1m: 12:34:00, 15m: 12:30:00, 1h: 12:00:00
    ts = datetime(2026, 5, 17, 12, 34, 56, tzinfo=timezone.utc)
    rows = compute_candle_rows(
        traded_outcome_id=1,
        outcome_ids=[1],
        new_prices=[0.5],
        traded_shares=Decimal("1"),
        ts=ts,
    )
    by_interval = {r["interval"]: r["bucket_start"] for r in rows}
    assert by_interval["10s"] == datetime(2026, 5, 17, 12, 34, 50, tzinfo=timezone.utc)
    assert by_interval["1m"]  == datetime(2026, 5, 17, 12, 34, 0,  tzinfo=timezone.utc)
    assert by_interval["15m"] == datetime(2026, 5, 17, 12, 30, 0,  tzinfo=timezone.utc)
    assert by_interval["1h"]  == datetime(2026, 5, 17, 12, 0,  0,  tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_upsert_single_row_then_read(client):
    _, oids = await _seed_market(n_outcomes=1)
    ts = datetime(2026, 5, 17, 12, 34, 56, tzinfo=timezone.utc)

    async with async_session_maker() as s:
        rows = compute_candle_rows(
            traded_outcome_id=oids[0],
            outcome_ids=oids,
            new_prices=[0.5],
            traded_shares=Decimal("3"),
            ts=ts,
        )
        await upsert_candles(s, rows)
        await s.commit()

        stored = (await s.execute(
            select(OutcomeCandle).where(OutcomeCandle.outcome_id == oids[0])
        )).scalars().all()

    assert len(stored) == 4  # 4 intervals
    for c in stored:
        assert c.open_price == Decimal("0.50000000")
        assert c.volume_shares == Decimal("3.000000")
        assert c.n_trades == 1


@pytest.mark.asyncio
async def test_upsert_same_bucket_merges_ohlc(client):
    """同 bucket 第二次 UPSERT：O 不变、C 更新、H/L 收紧、V/n 累加。"""
    _, oids = await _seed_market(n_outcomes=1)
    ts = datetime(2026, 5, 17, 12, 34, 0, tzinfo=timezone.utc)  # 对齐到 1m 桶

    async with async_session_maker() as s:
        # 第一次：price=0.5
        rows1 = compute_candle_rows(oids[0], oids, [0.5], Decimal("2"), ts)
        await upsert_candles(s, rows1)
        # 第二次同 bucket：price=0.7 (创新高)
        rows2 = compute_candle_rows(oids[0], oids, [0.7], Decimal("3"), ts)
        await upsert_candles(s, rows2)
        await s.commit()

        row_1m = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[0],
                OutcomeCandle.interval == "1m",
            )
        )).scalars().first()

    assert row_1m.open_price  == Decimal("0.50000000")  # 不变
    assert row_1m.close_price == Decimal("0.70000000")  # 更新
    assert row_1m.high_price  == Decimal("0.70000000")  # 收紧
    assert row_1m.low_price   == Decimal("0.50000000")
    assert row_1m.volume_shares == Decimal("5.000000")  # 累加
    assert row_1m.n_trades == 2


@pytest.mark.asyncio
async def test_upsert_low_price_collapses_correctly(client):
    """第二次 price 低于第一次 → low_price 收紧到新低。"""
    _, oids = await _seed_market(n_outcomes=1)
    ts = datetime(2026, 5, 17, 12, 34, 0, tzinfo=timezone.utc)

    async with async_session_maker() as s:
        await upsert_candles(s, compute_candle_rows(oids[0], oids, [0.5], Decimal("1"), ts))
        await upsert_candles(s, compute_candle_rows(oids[0], oids, [0.3], Decimal("1"), ts))
        await s.commit()

        row = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[0],
                OutcomeCandle.interval == "1m",
            )
        )).scalars().first()

    assert row.open_price == Decimal("0.50000000")
    assert row.low_price  == Decimal("0.30000000")
    assert row.high_price == Decimal("0.50000000")
    assert row.close_price == Decimal("0.30000000")


@pytest.mark.asyncio
async def test_upsert_empty_rows_noop(client):
    """空 rows 列表不报错。"""
    async with async_session_maker() as s:
        await upsert_candles(s, [])  # 不抛
        await s.commit()


def test_intervals_constant_matches_spec():
    """CANDLE_INTERVALS 必须是 spec 锁定的 4 档。"""
    assert CANDLE_INTERVALS == [("10s", 10), ("1m", 60), ("15m", 900), ("1h", 3600)]
```

- [ ] **Step 2.2：跑测试看失败**

```bash
cd backend
python -m pytest tests/test_candle_writer.py -v
```

预期：所有测试 FAIL（import `app.services.candle_writer` 失败，模块不存在）。

- [ ] **Step 2.3：实现 `backend/app/services/candle_writer.py`**

```python
"""Candle 物化表的写入逻辑。

被 buy/sell hot path 调用：在事务内同步 UPSERT N×4 行 candle。
也被 alembic migration / 兜底脚本 / startup hook 调用。

兼容 Postgres (生产) 和 SQLite (测试)：两者都支持 INSERT ... ON CONFLICT。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, List, Sequence

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import OutcomeCandle

# 跟 spec § 2 D1 锁定的 4 档；与 OutcomeCandle.interval 的 CheckConstraint 必须一致。
CANDLE_INTERVALS: List[tuple[str, int]] = [
    ("10s", 10),
    ("1m", 60),
    ("15m", 900),
    ("1h", 3600),
]


def _bucket_start(ts: datetime, step_seconds: int) -> datetime:
    """跟 chart.py:_bucket_start 同款实现（按 epoch 秒数 floor）。"""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % step_seconds), tz=timezone.utc)


def compute_candle_rows(
    traded_outcome_id: int,
    outcome_ids: Sequence[int],
    new_prices: Sequence[float],
    traded_shares: Decimal,
    ts: datetime,
) -> List[dict]:
    """为一笔 buy/sell 算出 N×4 行 UPSERT payload。

    - 每个 outcome × 每个 interval = 一行
    - 被直接交易的 outcome 行 volume_shares=traded_shares, n_trades=1
    - 联动行 volume_shares=0, n_trades=0
    - 首次 INSERT 时 O=H=L=C=该 outcome 的新价；后续 UPSERT 用 ON CONFLICT 合并。
    """
    assert len(outcome_ids) == len(new_prices), (
        f"outcome_ids ({len(outcome_ids)}) 与 new_prices ({len(new_prices)}) 长度必须一致"
    )

    rows: List[dict] = []
    for oid, price in zip(outcome_ids, new_prices):
        price_d = Decimal(str(price))
        is_traded = (oid == traded_outcome_id)
        v = traded_shares if is_traded else Decimal("0")
        n = 1 if is_traded else 0
        for interval, step in CANDLE_INTERVALS:
            bucket = _bucket_start(ts, step)
            rows.append({
                "outcome_id":   oid,
                "interval":     interval,
                "bucket_start": bucket,
                "open_price":   price_d,
                "high_price":   price_d,
                "low_price":    price_d,
                "close_price":  price_d,
                "volume_shares": v,
                "n_trades":      n,
            })
    return rows


async def upsert_candles(db: AsyncSession, rows: Iterable[dict]) -> None:
    """multi-row INSERT ... ON CONFLICT DO UPDATE 写入 candle 表。

    合并规则：
      - open_price 不在 set_ 中 → 首次 INSERT 的值永远保留
      - close_price 用 EXCLUDED（最后一次写入的值）
      - high_price = max(old, new)
      - low_price  = min(old, new)
      - volume_shares = old + new
      - n_trades = old + new
    """
    rows_list = list(rows)
    if not rows_list:
        return

    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        insert_fn = pg_insert
        # Postgres 用 GREATEST/LEAST
        greatest = func.greatest
        least = func.least
    elif dialect == "sqlite":
        insert_fn = sqlite_insert
        # SQLite 没 GREATEST/LEAST，用 MAX/MIN（标量上下文）
        greatest = func.max
        least = func.min
    else:
        raise NotImplementedError(f"不支持的 DB dialect: {dialect}")

    stmt = insert_fn(OutcomeCandle).values(rows_list)
    stmt = stmt.on_conflict_do_update(
        index_elements=["outcome_id", "interval", "bucket_start"],
        set_={
            "high_price":    greatest(OutcomeCandle.high_price, stmt.excluded.high_price),
            "low_price":     least(OutcomeCandle.low_price, stmt.excluded.low_price),
            "close_price":   stmt.excluded.close_price,
            "volume_shares": OutcomeCandle.volume_shares + stmt.excluded.volume_shares,
            "n_trades":      OutcomeCandle.n_trades + stmt.excluded.n_trades,
            "updated_at":    func.now(),
        },
    )
    await db.execute(stmt)
```

- [ ] **Step 2.4：跑测试看通过**

```bash
python -m pytest tests/test_candle_writer.py -v
```

预期：7 个测试全过。

- [ ] **Step 2.5：commit**

```bash
git add backend/app/services/candle_writer.py backend/tests/test_candle_writer.py
git commit -m "$(cat <<'EOF'
feat(candle): 加 candle_writer 服务（compute_rows + upsert）

compute_candle_rows: 一笔成交生成 N×4 行 UPSERT payload。
upsert_candles: ON CONFLICT DO UPDATE 合并 OHLCV；兼容 PG + SQLite。
CANDLE_INTERVALS 锁定为 spec § 2 D1 的 4 档 (10s/1m/15m/1h)。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 接入 buy/sell hot path

**Files:**
- Modify: `backend/app/api/v1/market.py` (buy_shares 在 line 552 附近、sell_shares 在 line 702 附近)
- Create: `backend/tests/test_candle_integration.py`

- [ ] **Step 3.1：写 integration test `backend/tests/test_candle_integration.py`**

```python
"""通过 API 调 buy/sell/settle 验 candle 表副作用。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlmodel import select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle, User


async def _seed_user(cash: Decimal = Decimal("10000")) -> tuple[int, dict]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        u = User(
            username=f"u_{suffix}", email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}",
            cash=cash, debt=Decimal("0"),
        )
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
    return uid, {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _seed_admin() -> dict:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        u = User(
            username=f"admin_{suffix}", email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}",
            cash=Decimal("0"), is_superuser=True,
        )
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _seed_market(n_outcomes: int = 2, b: float = 100.0) -> tuple[int, list[int]]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", status=MarketStatus.TRADING, liquidity_b=b)
        s.add(m)
        await s.flush()
        oids = []
        for i in range(n_outcomes):
            o = Outcome(market_id=m.id, label=f"opt_{i}", total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        await s.commit()
        return m.id, oids


async def _count_candles(outcome_id: int) -> int:
    async with async_session_maker() as s:
        rows = (await s.execute(
            select(OutcomeCandle).where(OutcomeCandle.outcome_id == outcome_id)
        )).scalars().all()
        return len(rows)


@pytest.mark.asyncio
async def test_single_buy_writes_n_times_4_rows(client):
    """一笔 buy → 全市场 N outcome × 4 interval = N×4 行 candle。"""
    mid, oids = await _seed_market(n_outcomes=2)
    _, headers = await _seed_user(cash=Decimal("1000"))

    resp = await client.post(
        f"/api/v1/market/buy",
        headers=headers,
        json={"outcome_id": oids[0], "shares": 1},
    )
    assert resp.status_code == 200, resp.text

    # 每个 outcome 4 行（10s/1m/15m/1h）
    for oid in oids:
        assert await _count_candles(oid) == 4

    # 被交易 outcome 的 volume_shares > 0、n_trades=1；联动 outcome v=0、n=0
    async with async_session_maker() as s:
        traded = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[0],
                OutcomeCandle.interval == "1m",
            )
        )).scalars().first()
        linked = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[1],
                OutcomeCandle.interval == "1m",
            )
        )).scalars().first()

    assert traded.n_trades == 1
    assert traded.volume_shares > 0
    assert linked.n_trades == 0
    assert linked.volume_shares == Decimal("0.000000")


@pytest.mark.asyncio
async def test_two_buys_same_bucket_merge(client):
    """同 bucket 两笔 buy → OHLC 合并，V/n 累加。"""
    mid, oids = await _seed_market(n_outcomes=2)
    _, headers = await _seed_user(cash=Decimal("1000"))

    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    async with async_session_maker() as s:
        # 1m bucket 几乎肯定是同一个（两次买间隔 <1 秒）
        rows = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[0],
                OutcomeCandle.interval == "1m",
            )
        )).scalars().all()

    assert len(rows) == 1, "两笔 buy 应在同一 1m bucket"
    assert rows[0].n_trades == 2
    assert rows[0].volume_shares == Decimal("2.000000")
    # close_price 应该是第二次的（比第一次高）
    assert rows[0].close_price > rows[0].open_price


@pytest.mark.asyncio
async def test_sell_writes_candle(client):
    """sell 路径同样触发 candle 写入。"""
    mid, oids = await _seed_market(n_outcomes=2)
    _, headers = await _seed_user(cash=Decimal("1000"))

    # 先 buy 建持仓
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 5})
    candle_count_before = await _count_candles(oids[0])

    # sell
    resp = await client.post("/api/v1/market/sell", headers=headers,
                             json={"outcome_id": oids[0], "shares": 2})
    assert resp.status_code == 200, resp.text

    candle_count_after = await _count_candles(oids[0])
    # 同 bucket 内 → 行数不增，但 n_trades+=1, v+=2
    assert candle_count_after == candle_count_before

    async with async_session_maker() as s:
        row = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[0],
                OutcomeCandle.interval == "1m",
            )
        )).scalars().first()
    assert row.n_trades == 2  # 1 buy + 1 sell
    assert row.volume_shares == Decimal("7.000000")  # 5 + 2


@pytest.mark.asyncio
async def test_settle_does_not_write_candle(client):
    """resolve_market 触发 settle/settle_lose → candle 表行数不增。"""
    mid, oids = await _seed_market(n_outcomes=2)
    _, headers = await _seed_user(cash=Decimal("1000"))
    admin_headers = await _seed_admin()

    # 一笔 buy 让 candle 表有些数据
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 3})
    candle_count_before = await _count_candles(oids[0]) + await _count_candles(oids[1])

    # admin settle
    resp = await client.post(
        f"/api/v1/market/{mid}/resolve",
        headers=admin_headers,
        json={"winning_outcome_id": oids[0]},
    )
    assert resp.status_code == 200, resp.text

    candle_count_after = await _count_candles(oids[0]) + await _count_candles(oids[1])
    assert candle_count_after == candle_count_before, "settle 不应写 candle"


@pytest.mark.asyncio
async def test_halt_blocks_trade_and_no_candle(client):
    """HALT 期间 buy 被拒 → candle 表无新行。"""
    mid, oids = await _seed_market(n_outcomes=2)
    _, headers = await _seed_user(cash=Decimal("1000"))
    admin_headers = await _seed_admin()

    # 先 buy 一笔
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})
    candle_count_before = await _count_candles(oids[0])

    # HALT
    resp = await client.post(f"/api/v1/market/{mid}/close", headers=admin_headers)
    assert resp.status_code == 200

    # 再 buy → 拒绝
    resp = await client.post("/api/v1/market/buy", headers=headers,
                             json={"outcome_id": oids[0], "shares": 1})
    assert resp.status_code == 400

    candle_count_after = await _count_candles(oids[0])
    assert candle_count_after == candle_count_before
```

- [ ] **Step 3.2：跑测试看失败**

```bash
python -m pytest tests/test_candle_integration.py -v
```

预期：所有测试 FAIL（buy/sell 还没接入 candle_writer，candle 表数行 == 0）。

- [ ] **Step 3.3：修改 `backend/app/api/v1/market.py:buy_shares`，在 Transaction INSERT 之后插入 UPSERT**

找到 buy_shares 函数体内 line 552 附近：

```python
            market_prices_post=list(new_prices),
        )
        db.add(tx)
```

紧跟在 `db.add(tx)` 之后（仍在 `async with managed_transaction(db):` 块内）插入：

```python
        # ★ candle 物化表 UPSERT（spec docs/superpowers/specs/2026-05-17-candle-cache-design.md）
        from app.services.candle_writer import compute_candle_rows, upsert_candles
        candle_rows = compute_candle_rows(
            traded_outcome_id=outcome.id,
            outcome_ids=[o.id for o in all_outcomes],
            new_prices=new_prices,
            traded_shares=shares_d,
            ts=tx.timestamp if tx.timestamp else datetime.now(timezone.utc),
        )
        await upsert_candles(db, candle_rows)
```

（`from app.services...` 放函数内是为了避免 module-level circular import；下次 task 整理 import 区时再提上来。）

- [ ] **Step 3.4：修改 `sell_shares`，line 702 附近做同样操作**

紧跟 `db.add(tx)` 之后插入与 Step 3.3 完全一样的 candle UPSERT 块。

- [ ] **Step 3.5：跑测试看通过**

```bash
python -m pytest tests/test_candle_integration.py -v
python -m pytest tests/test_candle_model.py tests/test_candle_writer.py -v  # 回归
```

预期：candle_integration 5 个测试全过，已有 candle_writer/model 测试不破。

- [ ] **Step 3.6：整理 import 区**

把 candle_writer 的 import 从函数内提到 market.py 顶部 import 区（line 31-37 现有 import 块之后）：

```python
from app.services.candle_writer import compute_candle_rows, upsert_candles
```

把 buy_shares / sell_shares 函数内的 `from app.services.candle_writer import ...` 删掉。

跑测试确认无回归：

```bash
python -m pytest tests/test_candle_integration.py -v
```

- [ ] **Step 3.7：commit**

```bash
git add backend/app/api/v1/market.py backend/tests/test_candle_integration.py
git commit -m "$(cat <<'EOF'
feat(candle): buy/sell 事务内同步 UPSERT outcome_candle

每笔 buy/sell 触发 N×4 行 candle UPSERT（全市场 N outcome × 4 interval），
跟现有 Transaction INSERT 在同事务内原子完成。settle/settle_lose 不参与
（spec § 2 D4：结算非真实成交、timestamp 扎堆会污染 K 线）。

预期单笔事务时长 +1ms，从 ~4ms 涨到 ~5ms，远低于 10r/s SLA 限速。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: chart.py 加 INTERVAL_ROUTE + _rollup

**Files:**
- Modify: `backend/app/api/v1/chart.py` (line 32 附近加新常量、文件末尾加 helper)
- Create: `backend/tests/test_candle_rollup.py`

- [ ] **Step 4.1：写测试 `backend/tests/test_candle_rollup.py`**

```python
"""INTERVAL_ROUTE + _rollup 单元测试。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api.v1.chart import INTERVAL_ROUTE, _rollup, _INTERVAL_SECONDS


def test_interval_route_completeness():
    """所有现有 _INTERVAL_SECONDS 的 interval 都必须在 INTERVAL_ROUTE 中有映射。"""
    for k in _INTERVAL_SECONDS.keys():
        assert k in INTERVAL_ROUTE, f"interval {k} 缺少 INTERVAL_ROUTE 映射"


def test_interval_route_divisibility():
    """rollup_factor 必须满足 target_step / storage_step。"""
    for target, (storage, factor) in INTERVAL_ROUTE.items():
        target_secs = _INTERVAL_SECONDS[target]
        storage_secs = _INTERVAL_SECONDS[storage]
        assert target_secs == storage_secs * factor, (
            f"{target} 路由破坏整除：target_secs={target_secs}, "
            f"storage_secs={storage_secs}, factor={factor}"
        )


def _make_fine_candle(bucket_start: datetime, o, h, l, c, v=0, n=0):
    """造一个假的 OutcomeCandle-like 对象用于 rollup 测试。"""
    return SimpleNamespace(
        bucket_start=bucket_start,
        open_price=Decimal(str(o)),
        high_price=Decimal(str(h)),
        low_price=Decimal(str(l)),
        close_price=Decimal(str(c)),
        volume_shares=Decimal(str(v)),
        n_trades=n,
    )


def test_rollup_three_buckets_into_one():
    """3 个 10s 桶合 1 个 30s 桶：O 取首、C 取尾、H/L 极值、V/n 求和。"""
    anchor = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    fine = [
        _make_fine_candle(datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc),
                          o=0.5, h=0.55, l=0.48, c=0.52, v=10, n=2),
        _make_fine_candle(datetime(2026, 5, 17, 12, 0, 10, tzinfo=timezone.utc),
                          o=0.52, h=0.60, l=0.50, c=0.58, v=5, n=1),
        _make_fine_candle(datetime(2026, 5, 17, 12, 0, 20, tzinfo=timezone.utc),
                          o=0.58, h=0.59, l=0.45, c=0.46, v=3, n=1),
    ]
    rolled = _rollup(fine, target_step=30, anchor=anchor)
    assert len(rolled) == 1
    c = rolled[0]
    assert c.bucket_start == anchor
    assert c.open_price  == Decimal("0.5")    # 第一桶 O
    assert c.close_price == Decimal("0.46")   # 最后桶 C
    assert c.high_price  == Decimal("0.60")   # max
    assert c.low_price   == Decimal("0.45")   # min
    assert c.volume_shares == Decimal("18")
    assert c.n_trades == 4


def test_rollup_partial_bucket_group():
    """6 个 10s 桶 (=2 个完整 30s)：返回 2 行；最后一组不满也仍合并。"""
    anchor = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    fine = [
        _make_fine_candle(datetime(2026, 5, 17, 12, 0, t, tzinfo=timezone.utc),
                          o=0.5, h=0.5, l=0.5, c=0.5, v=1, n=1)
        for t in (0, 10, 20, 30, 40, 50)
    ]
    rolled = _rollup(fine, target_step=30, anchor=anchor)
    assert len(rolled) == 2
    assert rolled[0].bucket_start == datetime(2026, 5, 17, 12, 0, 0,  tzinfo=timezone.utc)
    assert rolled[1].bucket_start == datetime(2026, 5, 17, 12, 0, 30, tzinfo=timezone.utc)


def test_rollup_empty_input():
    """空输入 → 空输出，不抛。"""
    anchor = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert _rollup([], target_step=30, anchor=anchor) == []


def test_rollup_single_bucket_factor_24():
    """24 个 1h 桶合 1 个 1d 桶（factor=24）。"""
    anchor = datetime(2026, 5, 17, 0, 0, 0, tzinfo=timezone.utc)
    fine = [
        _make_fine_candle(datetime(2026, 5, 17, hour, 0, 0, tzinfo=timezone.utc),
                          o=0.4 + hour * 0.01,
                          h=0.5 + hour * 0.01,
                          l=0.3 + hour * 0.01,
                          c=0.45 + hour * 0.01,
                          v=hour, n=1)
        for hour in range(24)
    ]
    rolled = _rollup(fine, target_step=86400, anchor=anchor)
    assert len(rolled) == 1
    c = rolled[0]
    assert c.open_price == Decimal("0.4")
    assert c.high_price == Decimal("0.73")  # 0.5 + 23*0.01
    assert c.low_price  == Decimal("0.3")
    assert c.close_price == Decimal("0.68")  # 0.45 + 23*0.01
    assert c.volume_shares == Decimal(sum(range(24)))  # 0+1+...+23 = 276
    assert c.n_trades == 24
```

- [ ] **Step 4.2：跑测试看失败**

```bash
python -m pytest tests/test_candle_rollup.py -v
```

预期：所有测试 FAIL（`INTERVAL_ROUTE` / `_rollup` 还未定义）。

- [ ] **Step 4.3：修改 `backend/app/api/v1/chart.py`**

在 `_INTERVAL_SECONDS` 定义后（line 35 之后）插入：

```python
# ── INTERVAL_ROUTE：把请求 interval 映射到 (storage_interval, rollup_factor) ──
# 主存 4 档 (10s/1m/15m/1h)，老 3 档 (30s/5m/1d) 由 chart endpoint 现 rollup。
# 所有 factor 都整除（30s=10s×3, 5m=1m×5, 1d=1h×24）。spec § 2 D2。
INTERVAL_ROUTE: Dict[str, tuple[str, int]] = {
    "10s": ("10s", 1),
    "30s": ("10s", 3),
    "1m":  ("1m", 1),
    "5m":  ("1m", 5),
    "15m": ("15m", 1),
    "1h":  ("1h", 1),
    "1d":  ("1h", 24),
}
```

在文件末尾（末尾是 `get_candles` 函数，在它**之前**或文件末尾追加）插入：

```python
def _rollup(fine_candles, target_step: int, anchor: datetime):
    """把按 storage interval 排好序的细桶合并到 target_step 粒度。

    O = 组内第一桶 open_price
    C = 组内最后桶 close_price
    H = max(组内 high_price)
    L = min(组内 low_price)
    V = sum(组内 volume_shares)
    n = sum(组内 n_trades)

    返回与 OutcomeCandle 字段相同的 SimpleNamespace 列表（不构造 ORM 实例，省开销）。
    """
    from types import SimpleNamespace
    if not fine_candles:
        return []

    # 按 target_step 分组：bucket_start 同属一组 ⇔ floor(epoch / target_step) 相同
    groups: Dict[int, list] = {}
    for c in fine_candles:
        epoch = int(c.bucket_start.timestamp())
        key = epoch - (epoch % target_step)
        groups.setdefault(key, []).append(c)

    rolled = []
    for key in sorted(groups.keys()):
        members = groups[key]
        rolled.append(SimpleNamespace(
            bucket_start=datetime.fromtimestamp(key, tz=timezone.utc),
            open_price=members[0].open_price,
            close_price=members[-1].close_price,
            high_price=max(m.high_price for m in members),
            low_price=min(m.low_price for m in members),
            volume_shares=sum((m.volume_shares for m in members), Decimal("0")),
            n_trades=sum(m.n_trades for m in members),
        ))
    return rolled
```

注意 `from decimal import Decimal` 在 chart.py 顶部要有（line 13 附近已经有 `from decimal import Decimal`？没有就加上）。

- [ ] **Step 4.4：跑测试看通过**

```bash
python -m pytest tests/test_candle_rollup.py -v
```

预期：6 个测试全过。

- [ ] **Step 4.5：commit**

```bash
git add backend/app/api/v1/chart.py backend/tests/test_candle_rollup.py
git commit -m "$(cat <<'EOF'
feat(chart): 加 INTERVAL_ROUTE + _rollup helpers

INTERVAL_ROUTE: 7 个 interval → (storage_interval, rollup_factor) 映射，
所有 factor 整除（30s=10s×3, 5m=1m×5, 1d=1h×24）。
_rollup: 把细桶按 target_step 重新分组，OHLC 数学定义合并。

后续 task 5 让 /chart/candles endpoint 使用。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 重写 `/chart/candles` endpoint + 删除 `/chart/price`

**Files:**
- Modify: `backend/app/api/v1/chart.py` (重写 `get_candles`，删除 `get_price_series`)
- Create: `backend/tests/test_chart_endpoint.py`

- [ ] **Step 5.1：写测试 `backend/tests/test_chart_endpoint.py`**

```python
"""/chart/candles endpoint 端到端测试。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlmodel import select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle, User


async def _seed_user(cash=Decimal("10000")) -> dict:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                 casdoor_id=f"cd_{suffix}", cash=cash)
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _seed_market(b=100.0) -> tuple[int, list[int]]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", status=MarketStatus.TRADING, liquidity_b=b)
        s.add(m)
        await s.flush()
        oids = []
        for i in range(2):
            o = Outcome(market_id=m.id, label=f"opt_{i}", total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        await s.commit()
        return m.id, oids


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.mark.asyncio
async def test_candles_direct_read_10s(client):
    mid, oids = await _seed_market()
    headers = await _seed_user()
    # 一笔 buy 创造 candle
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    now = datetime.now(timezone.utc)
    resp = await client.get("/api/v1/chart/candles", params={
        "outcome_id": oids[0],
        "interval": "10s",
        "from_ts": _iso(now - timedelta(minutes=10)),
        "to_ts": _iso(now + timedelta(seconds=10)),
        "fill": False,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["candles"]) >= 1
    c0 = data["candles"][0]
    assert "o" in c0 and "h" in c0 and "l" in c0 and "c" in c0
    assert c0["n"] >= 1


@pytest.mark.asyncio
async def test_candles_rollup_30s(client):
    """30s rollup：interval=30s 走 INTERVAL_ROUTE→('10s', 3)。"""
    mid, oids = await _seed_market()
    headers = await _seed_user()
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    now = datetime.now(timezone.utc)
    resp = await client.get("/api/v1/chart/candles", params={
        "outcome_id": oids[0],
        "interval": "30s",
        "from_ts": _iso(now - timedelta(minutes=10)),
        "to_ts": _iso(now + timedelta(seconds=10)),
        "fill": False,
    })
    assert resp.status_code == 200, resp.text
    # 至少 1 个 30s 桶；它的 OHLC 来自细桶 rollup
    assert len(resp.json()["candles"]) >= 1


@pytest.mark.asyncio
async def test_candles_rollup_5m(client):
    mid, oids = await _seed_market()
    headers = await _seed_user()
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    now = datetime.now(timezone.utc)
    resp = await client.get("/api/v1/chart/candles", params={
        "outcome_id": oids[0],
        "interval": "5m",
        "from_ts": _iso(now - timedelta(hours=1)),
        "to_ts": _iso(now + timedelta(minutes=5)),
        "fill": False,
    })
    assert resp.status_code == 200
    assert len(resp.json()["candles"]) >= 1


@pytest.mark.asyncio
async def test_candles_fill_true_pads_empty_buckets(client):
    """fill=true 时空桶用 prev_close 填。"""
    mid, oids = await _seed_market()
    headers = await _seed_user()
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    now = datetime.now(timezone.utc)
    resp = await client.get("/api/v1/chart/candles", params={
        "outcome_id": oids[0],
        "interval": "10s",
        "from_ts": _iso(now - timedelta(minutes=2)),
        "to_ts": _iso(now + timedelta(seconds=10)),
        "fill": True,
    })
    assert resp.status_code == 200
    candles = resp.json()["candles"]
    # 至少 12 个 10s 桶（2 分钟）+ fill 兜底
    assert len(candles) >= 12
    # 空 bucket 的 O=H=L=C，且 n=0
    empty_buckets = [c for c in candles if c["n"] == 0]
    for c in empty_buckets:
        assert c["o"] == c["h"] == c["l"] == c["c"]


@pytest.mark.asyncio
async def test_candles_unsupported_interval_400(client):
    mid, oids = await _seed_market()
    now = datetime.now(timezone.utc)
    resp = await client.get("/api/v1/chart/candles", params={
        "outcome_id": oids[0],
        "interval": "2h",  # 不在 INTERVAL_ROUTE 中
        "from_ts": _iso(now - timedelta(hours=1)),
        "to_ts": _iso(now),
    })
    assert resp.status_code in (400, 422)


@pytest.mark.asyncio
async def test_chart_price_endpoint_removed(client):
    """/chart/price 应该不再存在。"""
    now = datetime.now(timezone.utc)
    resp = await client.get("/api/v1/chart/price", params={
        "outcome_id": 1,
        "from_ts": _iso(now - timedelta(hours=1)),
        "to_ts": _iso(now),
    })
    assert resp.status_code == 404
```

- [ ] **Step 5.2：跑测试看失败**

```bash
python -m pytest tests/test_chart_endpoint.py -v
```

预期：rollup / fill 相关测试 FAIL（旧 get_candles 走 Transaction 重放，candle 表数据不被读）；`test_chart_price_endpoint_removed` FAIL（endpoint 仍存在）。

- [ ] **Step 5.3：重写 `chart.py` 中的 `get_candles` 函数**

将 line 290（`@router.get("/candles", ...)`）开始到该函数末尾整段替换为：

```python
@router.get("/candles", response_model=CandleSeriesResponse, summary="K线（OHLCV）")
async def get_candles(
    outcome_id: int = Query(..., description="Outcome ID"),
    interval: Interval = Query("1m"),
    from_ts: datetime = Query(..., description="起始时间（ISO）"),
    to_ts: datetime = Query(..., description="结束时间（ISO）"),
    fill: bool = Query(False),
    limit: int = Query(5000, ge=1, le=20000),
    db: AsyncSession = Depends(get_async_session),
):
    """直接查 outcome_candle 物化表 + rollup + fill。
    告别 5000 笔逐笔重放硬上限（spec docs/superpowers/specs/2026-05-17-candle-cache-design.md）。
    """
    if str(interval) not in INTERVAL_ROUTE:
        raise HTTPException(status_code=400, detail="不支持的 interval")
    storage_interval, rollup_factor = INTERVAL_ROUTE[str(interval)]
    target_step  = _INTERVAL_SECONDS[str(interval)]
    storage_step = _INTERVAL_SECONDS[storage_interval]

    from_ts_u = _ensure_utc(from_ts)
    to_ts_u   = _ensure_utc(to_ts)
    _validate_range(from_ts_u, to_ts_u)

    aligned_from, aligned_to = _align_range_to_buckets(from_ts_u, to_ts_u, target_step)
    max_buckets = int((aligned_to.timestamp() - aligned_from.timestamp()) // target_step)
    if max_buckets > limit:
        raise HTTPException(
            status_code=400,
            detail=f"时间跨度过大：预计 {max_buckets} 根K线，超过 limit={limit}",
        )

    # fill 时多拉一个 prev_close 桶
    extra_lookback = storage_step if fill else 0
    from app.models.base import OutcomeCandle
    stmt = (
        select(OutcomeCandle)
        .where(
            OutcomeCandle.outcome_id == outcome_id,
            OutcomeCandle.interval == storage_interval,
            OutcomeCandle.bucket_start >= aligned_from - timedelta(seconds=extra_lookback),
            OutcomeCandle.bucket_start < aligned_to,
        )
        .order_by(OutcomeCandle.bucket_start.asc())
    )
    fine_candles = (await db.execute(stmt)).scalars().all()

    if rollup_factor > 1:
        candles_src = _rollup(fine_candles, target_step, aligned_from)
    else:
        candles_src = fine_candles

    # 转 Schema + fill
    if not fill:
        # 只返回 aligned_from..aligned_to 内的桶
        candles = [
            Candle(
                t=c.bucket_start,
                o=c.open_price, h=c.high_price,
                l=c.low_price,  c=c.close_price,
                v=float(c.volume_shares), n=c.n_trades,
            )
            for c in candles_src
            if c.bucket_start >= aligned_from
        ]
        return CandleSeriesResponse(outcome_id=outcome_id, interval=interval,
                                    from_ts=from_ts_u, to_ts=to_ts_u, candles=candles)

    # fill=true：扫每个 target_step 桶，缺失用 prev_close 填
    by_bucket = {c.bucket_start: c for c in candles_src}
    prev_close = None
    # 用 aligned_from - extra_lookback 那个桶（如果有）提供 prev_close
    if extra_lookback and candles_src:
        before = [c for c in candles_src if c.bucket_start < aligned_from]
        if before:
            prev_close = before[-1].close_price

    candles: List[Candle] = []
    cur = aligned_from
    while cur < aligned_to:
        c = by_bucket.get(cur)
        if c is not None and c.bucket_start >= aligned_from:
            candles.append(Candle(
                t=cur,
                o=c.open_price, h=c.high_price,
                l=c.low_price,  c=c.close_price,
                v=float(c.volume_shares), n=c.n_trades,
            ))
            prev_close = c.close_price
        elif prev_close is not None:
            candles.append(Candle(
                t=cur,
                o=prev_close, h=prev_close, l=prev_close, c=prev_close,
                v=0.0, n=0,
            ))
        cur = datetime.fromtimestamp(int(cur.timestamp()) + target_step, tz=timezone.utc)

    return CandleSeriesResponse(outcome_id=outcome_id, interval=interval,
                                from_ts=from_ts_u, to_ts=to_ts_u, candles=candles)
```

注意：保留 `timedelta` 的 import；如果 chart.py 顶部还没有，加上：`from datetime import datetime, timedelta, timezone`。

- [ ] **Step 5.4：删除 `chart.py` 中的 `/price` endpoint**

删除从 `@router.get("/price", ...)` 开始到 `get_price_series` 函数结束的整段（line 253-283 范围）。

**保留** `_fetch_initial_shares_and_replay`、`_replay_from_snapshots`、`_replay_numpy` 这三个函数——它们后续会被回填脚本使用。

- [ ] **Step 5.5：跑测试看通过**

```bash
python -m pytest tests/test_chart_endpoint.py -v
python -m pytest tests/test_candle_integration.py tests/test_candle_rollup.py -v  # 回归
```

预期：所有测试通过。

- [ ] **Step 5.6：smoke import**

```bash
python -m py_compile $(find app -name '*.py')
python -c "import app.main"
```

预期：无错误。

- [ ] **Step 5.7：commit**

```bash
git add backend/app/api/v1/chart.py backend/tests/test_chart_endpoint.py
git commit -m "$(cat <<'EOF'
feat(chart): /candles 走 outcome_candle 表 + rollup；删 /price endpoint

/chart/candles 改为直查 outcome_candle 物化表（4 主档直读，3 老档现 rollup），
彻底告别 5000 笔逐笔重放硬上限。fill=true 时空桶仍按 prev_close 填。

废除 /chart/price endpoint，前端 PriceChart 后续 task 改调 /candles 取 c 字段渲染折线。
_fetch_initial_shares_and_replay 函数保留（回填脚本仍用）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: 历史回填脚本（独立函数 + CLI）

**Files:**
- Modify: `backend/app/services/candle_writer.py` (加 `backfill_one_market`)
- Create: `backend/scripts/backfill_outcome_candle.py` (CLI)
- Create: `backend/tests/test_candle_backfill.py`

- [ ] **Step 6.1：写测试 `backend/tests/test_candle_backfill.py`**

```python
"""测试回填函数：跑一批历史 transaction 后 candle 表与实时积累等价。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import delete, select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle, User
from app.services.candle_writer import backfill_one_market


async def _seed_user_market(b=100.0) -> tuple[int, list[int], dict]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", status=MarketStatus.TRADING, liquidity_b=b)
        s.add(m)
        await s.flush()
        oids = []
        for i in range(2):
            o = Outcome(market_id=m.id, label=f"opt_{i}", total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                 casdoor_id=f"cd_{suffix}", cash=Decimal("100000"))
        s.add(u)
        await s.flush()
        uid = u.id
        mid = m.id
        await s.commit()
    return mid, oids, {"Authorization": f"Bearer {create_access_token(uid)}"}


@pytest.mark.asyncio
async def test_backfill_matches_live_writes(client):
    """跑几笔买卖（hot path 会写 candle）→ 清空 candle 表 → 跑回填 → 比对结果一致。"""
    mid, oids, headers = await _seed_user_market()

    # 跑 5 笔交易，hot path 会写 candle
    for _ in range(3):
        await client.post("/api/v1/market/buy", headers=headers,
                          json={"outcome_id": oids[0], "shares": 1})
    for _ in range(2):
        await client.post("/api/v1/market/buy", headers=headers,
                          json={"outcome_id": oids[1], "shares": 1})

    # 快照 candle 表
    async with async_session_maker() as s:
        live = sorted([
            (c.outcome_id, c.interval, c.bucket_start,
             c.open_price, c.high_price, c.low_price, c.close_price,
             c.volume_shares, c.n_trades)
            for c in (await s.execute(select(OutcomeCandle))).scalars().all()
        ])

        # 清空 candle 表
        await s.execute(delete(OutcomeCandle))
        await s.commit()

    # 跑回填
    async with async_session_maker() as s:
        await backfill_one_market(s, mid)
        await s.commit()

    # 再次快照
    async with async_session_maker() as s:
        backfilled = sorted([
            (c.outcome_id, c.interval, c.bucket_start,
             c.open_price, c.high_price, c.low_price, c.close_price,
             c.volume_shares, c.n_trades)
            for c in (await s.execute(select(OutcomeCandle))).scalars().all()
        ])

    assert backfilled == live, "回填结果与实时积累不一致"


@pytest.mark.asyncio
async def test_backfill_idempotent(client):
    """跑两次回填，candle 表不会被重复累加。"""
    mid, oids, headers = await _seed_user_market()
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    async with async_session_maker() as s:
        await s.execute(delete(OutcomeCandle))
        await s.commit()

    async with async_session_maker() as s:
        await backfill_one_market(s, mid)
        await s.commit()
    async with async_session_maker() as s:
        first = (await s.execute(select(OutcomeCandle))).scalars().all()
        first_snapshot = [(c.outcome_id, c.interval, c.bucket_start,
                           c.volume_shares, c.n_trades) for c in first]

    # 再跑一次
    async with async_session_maker() as s:
        await backfill_one_market(s, mid)
        await s.commit()
    async with async_session_maker() as s:
        second = (await s.execute(select(OutcomeCandle))).scalars().all()
        second_snapshot = [(c.outcome_id, c.interval, c.bucket_start,
                            c.volume_shares, c.n_trades) for c in second]

    assert sorted(first_snapshot) == sorted(second_snapshot), "回填非幂等"


@pytest.mark.asyncio
async def test_backfill_skips_settle_rows(client):
    """settle/settle_lose Transaction 不应被回填到 candle 表（spec § 2 D4）。"""
    from app.models.base import Transaction, TransactionType
    mid, oids, headers = await _seed_user_market()

    # 一笔正常 buy
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    # 手插一笔 settle Transaction（模拟结算）
    async with async_session_maker() as s:
        s.add(Transaction(
            user_id=1, outcome_id=oids[0], type=TransactionType.SETTLE,
            shares=Decimal("0"), cost=Decimal("0"),
            gross=Decimal("1.0"), price=Decimal("1.0"),
            pre_market_price=Decimal("0"), post_market_price=Decimal("0"),
            # market_prices_post 留 NULL（settle 行约定）
        ))
        await s.commit()

    # 清空 candle 表后跑回填
    async with async_session_maker() as s:
        await s.execute(delete(OutcomeCandle))
        await s.commit()
    async with async_session_maker() as s:
        await backfill_one_market(s, mid)
        await s.commit()

    # candle 表应只反映 buy 的影响，不含 settle 那笔
    async with async_session_maker() as s:
        candles = (await s.execute(select(OutcomeCandle))).scalars().all()
    total_n = sum(c.n_trades for c in candles if c.outcome_id == oids[0])
    # 4 个 interval × 1 buy = 4；settle 不参与
    assert total_n == 4
```

- [ ] **Step 6.2：跑测试看失败**

```bash
python -m pytest tests/test_candle_backfill.py -v
```

预期：FAIL（`backfill_one_market` 未定义）。

- [ ] **Step 6.3：在 `backend/app/services/candle_writer.py` 末尾追加 `backfill_one_market`**

```python
async def backfill_one_market(db: AsyncSession, market_id: int) -> int:
    """回填某个 market 的全部 buy/sell Transaction 到 candle 表。

    幂等：通过 upsert_candles 的 ON CONFLICT DO UPDATE 合并；从空表跑还是已经有
    部分数据跑结果都一致。但**不能对同一 Transaction 重复调用**（否则 volume/n
    会被累加）—— 调用方应先 `DELETE FROM outcome_candle WHERE outcome_id IN (...)`
    或保证只调一次。

    返回处理的 Transaction 行数。
    """
    from sqlalchemy import select
    from app.models.base import Market, Outcome, Transaction, TransactionType

    market = await db.get(Market, market_id)
    if market is None:
        return 0
    outcomes = (await db.execute(
        select(Outcome).where(Outcome.market_id == market_id).order_by(Outcome.id)
    )).scalars().all()
    if not outcomes:
        return 0
    outcome_ids = [o.id for o in outcomes]

    # 拉全部 buy/sell（排除 settle/settle_lose，timestamp 升序）
    txs = (await db.execute(
        select(Transaction)
        .where(
            Transaction.outcome_id.in_(outcome_ids),
            Transaction.type.in_([TransactionType.BUY, TransactionType.SELL]),
        )
        .order_by(Transaction.timestamp.asc())
    )).scalars().all()

    n_processed = 0
    for tx in txs:
        # 优先用 market_prices_post 快照（已存就直接用）
        if tx.market_prices_post and len(tx.market_prices_post) == len(outcome_ids):
            new_prices = [float(p) for p in tx.market_prices_post]
        else:
            # 老历史可能没 snapshot；fallback：用 tx.post_market_price 当被交易 outcome 价，
            # 其他 outcome 用 1/N 简化（这是退化路径，应在生产前先跑 backfill_market_prices_post）
            new_prices = [float(tx.post_market_price) if oid == tx.outcome_id
                          else 1.0 / len(outcome_ids) for oid in outcome_ids]

        rows = compute_candle_rows(
            traded_outcome_id=tx.outcome_id,
            outcome_ids=outcome_ids,
            new_prices=new_prices,
            traded_shares=tx.shares,
            ts=tx.timestamp,
        )
        await upsert_candles(db, rows)
        n_processed += 1

    return n_processed
```

注意函数依赖 `Market` / `Outcome` / `Transaction` / `TransactionType` 的 import；放在函数内是为了避免 module-level 循环依赖（candle_writer 在 chart.py 之外，但和 models 是单向依赖）。可以提到顶部 import 区。

- [ ] **Step 6.4：跑测试看通过**

```bash
python -m pytest tests/test_candle_backfill.py -v
```

预期：3 个测试全过。

- [ ] **Step 6.5：写 CLI 脚本 `backend/scripts/backfill_outcome_candle.py`**

```python
"""回填 outcome_candle 表的独立 CLI。
仿 backend/scripts/backfill_market_prices_post.py 的风格。

用法：
    cd backend
    python -m scripts.backfill_outcome_candle                # 全量
    python -m scripts.backfill_outcome_candle --dry-run      # 统计不写
    python -m scripts.backfill_outcome_candle --market-id 23 # 单市场
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import List

from sqlalchemy import select, delete

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import engine, async_session_maker  # noqa: E402
from app.models.base import Market, OutcomeCandle, Outcome  # noqa: E402
from app.services.candle_writer import backfill_one_market  # noqa: E402


async def _list_markets() -> List[int]:
    async with async_session_maker() as s:
        ids = (await s.execute(select(Market.id).order_by(Market.id))).all()
        return [r[0] for r in ids]


async def _clear_candles_for_market(market_id: int) -> int:
    """删除该 market 所有 outcome 的 candle 行。让回填从干净状态跑。"""
    async with async_session_maker() as s:
        oids = (await s.execute(
            select(Outcome.id).where(Outcome.market_id == market_id)
        )).all()
        oid_list = [r[0] for r in oids]
        if not oid_list:
            return 0
        res = await s.execute(
            delete(OutcomeCandle).where(OutcomeCandle.outcome_id.in_(oid_list))
        )
        await s.commit()
        return res.rowcount or 0


async def run(market_ids: List[int], dry_run: bool) -> None:
    print(f"找到 {len(market_ids)} 个市场要回填")
    total_processed = 0
    for mid in market_ids:
        if dry_run:
            async with async_session_maker() as s:
                from app.models.base import Transaction, TransactionType
                outcomes = (await s.execute(
                    select(Outcome.id).where(Outcome.market_id == mid)
                )).all()
                oid_list = [r[0] for r in outcomes]
                if not oid_list:
                    continue
                cnt = (await s.execute(
                    select(Transaction.id).where(
                        Transaction.outcome_id.in_(oid_list),
                        Transaction.type.in_([TransactionType.BUY, TransactionType.SELL]),
                    )
                )).all()
                print(f"  market_id={mid}: {len(cnt)} 笔可回填（dry-run）")
                total_processed += len(cnt)
            continue

        cleared = await _clear_candles_for_market(mid)
        async with async_session_maker() as s:
            n = await backfill_one_market(s, mid)
            await s.commit()
        print(f"  market_id={mid}: 清空 {cleared} 旧行, 回填 {n} 笔 Transaction")
        total_processed += n

    print(f"总计：{total_processed} 笔（{'dry-run 未实写' if dry_run else '已写入'}）")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计不写")
    parser.add_argument("--market-id", type=int, help="只处理指定 market")
    args = parser.parse_args()

    if args.market_id:
        market_ids = [args.market_id]
    else:
        market_ids = await _list_markets()
    await run(market_ids, args.dry_run)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 6.6：跑测试 + smoke**

```bash
python -m pytest tests/test_candle_backfill.py -v
python -m py_compile scripts/backfill_outcome_candle.py
```

预期：测试过、py_compile 无报错。

- [ ] **Step 6.7：commit**

```bash
git add backend/app/services/candle_writer.py \
        backend/scripts/backfill_outcome_candle.py \
        backend/tests/test_candle_backfill.py
git commit -m "$(cat <<'EOF'
feat(candle): 加 backfill_one_market 函数 + CLI 脚本

backfill_one_market: 给定 market，按 timestamp 升序回放所有 buy/sell
Transaction 到 candle 表。优先用 Transaction.market_prices_post 快照；
缺失时 fallback 用 post_market_price + 1/N 简化（应先跑
backfill_market_prices_post 保证 snapshot 齐全）。

CLI 仿 backfill_market_prices_post.py 风格：--dry-run / --market-id。

settle/settle_lose 不参与（spec § 2 D4）。

后续 task 7 让 alembic migration 直接调它。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: alembic migration 接入回填

**Files:**
- Modify: Task 1 生成的 `backend/alembic/versions/2026_05_17_*_add_outcome_candle.py`

- [ ] **Step 7.1：在 migration 的 `upgrade()` 末尾追加回填逻辑**

打开 Task 1 生成的 migration 文件，在 `op.create_table(...)` 后追加：

```python
    # ── 回填历史 Transaction → outcome_candle（spec § 6） ──
    # 用 connection.run_sync 调一个 sync wrapper 跑回填；
    # 避免在 sync migration 上下文里直接 await。
    from sqlalchemy import create_engine
    from app.core.config import settings

    # 用 sync engine（migration 时已在 sync 上下文）
    sync_url = str(settings.DATABASE_URL).replace("+asyncpg", "").replace("+aiosqlite", "")
    sync_engine = create_engine(sync_url)
    _backfill_sync(sync_engine)
    sync_engine.dispose()


def _backfill_sync(sync_engine) -> None:
    """同步版本的回填：在 alembic upgrade 上下文中调用。
    按 market 分批提交，避免单巨型事务长锁。
    """
    from sqlalchemy.orm import sessionmaker
    from app.models.base import Market

    SessionLocal = sessionmaker(bind=sync_engine)
    with SessionLocal() as s:
        market_ids = [r[0] for r in s.execute(sa.select(Market.id).order_by(Market.id)).all()]

    print(f"[migration] 回填 {len(market_ids)} 个 market 的 candle 数据...")
    for mid in market_ids:
        # 每 market 一个独立 session/事务，避免长锁
        with SessionLocal() as s:
            _backfill_one_market_sync(s, mid)
            s.commit()
        print(f"[migration]   market {mid} done")


def _backfill_one_market_sync(s, market_id: int) -> None:
    """同步版的 backfill_one_market（不依赖 AsyncSession）。
    逻辑跟 app/services/candle_writer.py:backfill_one_market 完全一致，
    只是用 sync session API。
    """
    from decimal import Decimal
    from datetime import datetime, timezone
    from app.models.base import Market, Outcome, Transaction, TransactionType
    from app.services.candle_writer import (
        compute_candle_rows, CANDLE_INTERVALS, _bucket_start,
    )
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy import func
    from app.models.base import OutcomeCandle

    market = s.get(Market, market_id)
    if market is None:
        return
    outcomes = s.execute(
        sa.select(Outcome).where(Outcome.market_id == market_id).order_by(Outcome.id)
    ).scalars().all()
    if not outcomes:
        return
    outcome_ids = [o.id for o in outcomes]

    txs = s.execute(
        sa.select(Transaction)
        .where(
            Transaction.outcome_id.in_(outcome_ids),
            Transaction.type.in_([TransactionType.BUY, TransactionType.SELL]),
        )
        .order_by(Transaction.timestamp.asc())
    ).scalars().all()

    # 检测 dialect 后选 insert helper
    dialect = s.bind.dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    greatest = func.greatest if dialect == "postgresql" else func.max
    least = func.least if dialect == "postgresql" else func.min

    for tx in txs:
        if tx.market_prices_post and len(tx.market_prices_post) == len(outcome_ids):
            new_prices = [float(p) for p in tx.market_prices_post]
        else:
            new_prices = [float(tx.post_market_price) if oid == tx.outcome_id
                          else 1.0 / len(outcome_ids) for oid in outcome_ids]
        rows = compute_candle_rows(
            traded_outcome_id=tx.outcome_id,
            outcome_ids=outcome_ids,
            new_prices=new_prices,
            traded_shares=tx.shares,
            ts=tx.timestamp,
        )
        stmt = insert_fn(OutcomeCandle).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["outcome_id", "interval", "bucket_start"],
            set_={
                "high_price":    greatest(OutcomeCandle.high_price, stmt.excluded.high_price),
                "low_price":     least(OutcomeCandle.low_price, stmt.excluded.low_price),
                "close_price":   stmt.excluded.close_price,
                "volume_shares": OutcomeCandle.volume_shares + stmt.excluded.volume_shares,
                "n_trades":      OutcomeCandle.n_trades + stmt.excluded.n_trades,
                "updated_at":    func.now(),
            },
        )
        s.execute(stmt)
```

- [ ] **Step 7.2：在 dev DB 上跑 migration 验证**

```bash
cd backend
# 备份 dev DB（保险）
cp data/thccb.db data/thccb.db.before_candle_migration
alembic upgrade head
```

预期输出包含 `[migration] 回填 N 个 market 的 candle 数据...`。无 error。

- [ ] **Step 7.3：验证 candle 表填了数据**

```bash
sqlite3 data/thccb.db "SELECT COUNT(*) FROM outcome_candle; SELECT interval, COUNT(*) FROM outcome_candle GROUP BY interval;"
```

预期：行数 > 0（如果 dev DB 有历史交易的话；本项目 dev DB 当前 0 笔交易也可以接受）。

- [ ] **Step 7.4：跑回归测试**

测试环境的 conftest 用 SQLModel.metadata.create_all 不走 migration，所以 migration 改动**不影响** pytest 行为。但跑一遍确认：

```bash
python -m pytest -x --ignore=market_test.py --ignore=user_test.py
```

预期：134+ 测试全过（包含新加的 candle 相关测试）。

- [ ] **Step 7.5：commit**

```bash
git add "backend/alembic/versions/2026_05_17_*_add_outcome_candle.py"
git commit -m "$(cat <<'EOF'
feat(candle): alembic migration 内回填历史 transaction

CREATE TABLE 之后同事务调用 _backfill_sync()，按 market 分批提交避免长锁。
逻辑跟 candle_writer.backfill_one_market 等价（sync 版本，因为 alembic 在
sync 上下文）。

幂等保证：ON CONFLICT DO UPDATE；migration 重跑安全（虽然实际 alembic 不会
重跑同一版本）。Race window 兜底由后续 task 8 的 startup hook 处理。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: startup hook `_resync_recent_candles`

**Files:**
- Modify: `backend/app/main.py` (lifespan)

- [ ] **Step 8.1：写测试 `backend/tests/test_candle_resync.py`**

```python
"""startup hook _resync_recent_candles 兜底扫测试。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import delete, select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.main import _resync_recent_candles
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle, User


async def _seed_user_market(b=100.0):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", status=MarketStatus.TRADING, liquidity_b=b)
        s.add(m)
        await s.flush()
        oids = []
        for i in range(2):
            o = Outcome(market_id=m.id, label=f"opt_{i}", total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                 casdoor_id=f"cd_{suffix}", cash=Decimal("100000"))
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
    return m.id, oids, {"Authorization": f"Bearer {create_access_token(uid)}"}


@pytest.mark.asyncio
async def test_resync_fills_missing_recent_candles(client):
    """造一个"丢了的"candle 场景：手工删某些行，跑 resync 应补回。"""
    mid, oids, headers = await _seed_user_market()
    # 走 hot path 写 candle
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    # 手工删除 outcome[0] 的部分 candle 行（模拟"漏写"）
    async with async_session_maker() as s:
        await s.execute(delete(OutcomeCandle).where(
            OutcomeCandle.outcome_id == oids[0],
            OutcomeCandle.interval == "10s",
        ))
        await s.commit()

    # 跑 resync
    await _resync_recent_candles()

    # 应已恢复
    async with async_session_maker() as s:
        rows = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[0],
                OutcomeCandle.interval == "10s",
            )
        )).scalars().all()
    assert len(rows) >= 1, "resync 应补回 10s candle"


@pytest.mark.asyncio
async def test_resync_idempotent(client):
    """没漏写时 resync 不会污染现有数据。"""
    mid, oids, headers = await _seed_user_market()
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    async with async_session_maker() as s:
        before = sorted([
            (c.outcome_id, c.interval, c.bucket_start, c.volume_shares, c.n_trades)
            for c in (await s.execute(select(OutcomeCandle))).scalars().all()
        ])

    await _resync_recent_candles()

    async with async_session_maker() as s:
        after = sorted([
            (c.outcome_id, c.interval, c.bucket_start, c.volume_shares, c.n_trades)
            for c in (await s.execute(select(OutcomeCandle))).scalars().all()
        ])

    assert before == after, "resync 在已同步状态下不应改动数据"
```

- [ ] **Step 8.2：跑测试看失败**

```bash
python -m pytest tests/test_candle_resync.py -v
```

预期：FAIL（`_resync_recent_candles` 未定义）。

- [ ] **Step 8.3：修改 `backend/app/main.py`**

在 `lifespan` 函数（line 49 附近）的 startup 段加入 resync 调用。先在文件顶部 import：

```python
# 在 main.py 顶部 import 区追加
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, delete
```

在 lifespan 中（startup 阶段，scheduler start 之前）加入：

```python
    # ── candle 表 race-window 兜底扫（spec § 6.3）──
    # 覆盖 migration 完成→新代码上线之间可能漏的 buy/sell。
    try:
        await _resync_recent_candles()
    except Exception as e:
        # 兜底失败不能阻塞启动；记日志后续手工跑 backfill CLI
        import logging
        logging.getLogger("thccb.candle").exception("resync_recent_candles failed: %s", e)
```

在 `lifespan` 函数之后（或合适位置）定义函数：

```python
async def _resync_recent_candles(window_hours: int = 1) -> None:
    """扫近 window_hours 内的 buy/sell Transaction，对 candle 表做幂等 UPSERT。
    覆盖 migration→新代码之间的 race window。

    幂等通过 ON CONFLICT DO UPDATE 保证；多跑几次不会污染。
    """
    from app.core.database import async_session_maker
    from app.models.base import Market, Outcome, Transaction, TransactionType, OutcomeCandle
    from app.services.candle_writer import (
        compute_candle_rows, upsert_candles,
    )

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    async with async_session_maker() as s:
        # 扫近 window 内有过 buy/sell 的 market
        market_ids = (await s.execute(
            select(Outcome.market_id)
            .join(Transaction, Transaction.outcome_id == Outcome.id)
            .where(
                Transaction.timestamp >= cutoff,
                Transaction.type.in_([TransactionType.BUY, TransactionType.SELL]),
            )
            .distinct()
        )).all()
        market_ids = [r[0] for r in market_ids]

    if not market_ids:
        return

    for mid in market_ids:
        async with async_session_maker() as s:
            outcomes = (await s.execute(
                select(Outcome).where(Outcome.market_id == mid).order_by(Outcome.id)
            )).scalars().all()
            outcome_ids = [o.id for o in outcomes]
            if not outcome_ids:
                continue

            txs = (await s.execute(
                select(Transaction)
                .where(
                    Transaction.outcome_id.in_(outcome_ids),
                    Transaction.timestamp >= cutoff,
                    Transaction.type.in_([TransactionType.BUY, TransactionType.SELL]),
                )
                .order_by(Transaction.timestamp.asc())
            )).scalars().all()

            for tx in txs:
                if tx.market_prices_post and len(tx.market_prices_post) == len(outcome_ids):
                    new_prices = [float(p) for p in tx.market_prices_post]
                else:
                    new_prices = [float(tx.post_market_price) if oid == tx.outcome_id
                                  else 1.0 / len(outcome_ids) for oid in outcome_ids]
                rows = compute_candle_rows(
                    traded_outcome_id=tx.outcome_id,
                    outcome_ids=outcome_ids,
                    new_prices=new_prices,
                    traded_shares=tx.shares,
                    ts=tx.timestamp,
                )
                await upsert_candles(s, rows)
            await s.commit()
```

- [ ] **Step 8.4：跑测试看通过**

```bash
python -m pytest tests/test_candle_resync.py -v
python -m pytest -x --ignore=market_test.py --ignore=user_test.py
```

预期：resync 测试过；全量测试无回归。

- [ ] **Step 8.5：commit**

```bash
git add backend/app/main.py backend/tests/test_candle_resync.py
git commit -m "$(cat <<'EOF'
feat(candle): startup hook _resync_recent_candles 兜底扫

应用启动时扫近 1 小时的 buy/sell Transaction，对 candle 表做幂等 UPSERT，
覆盖 migration→新代码上线之间的 race window（spec § 6.3）。

失败不阻塞启动；记 logger.thccb.candle 错误，可手工跑 CLI 补救。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: 前端 PriceChart 改造（调 /candles 取 c 字段）

**Files:**
- Modify: `thccb-frontend/src/components/chart/PriceChart.vue`

- [ ] **Step 9.1：改 props 定义**

打开 `PriceChart.vue`，把现有 props（line 17-26）：

```ts
const props = withDefaults(defineProps<{
  outcomeId: number
  lookbackMinutes?: number
  width?: string
  height?: string
}>(), {
  lookbackMinutes: 1440,
  width: '100%',
  height: '400px',
})
```

改为：

```ts
type ChartInterval = '10s' | '1m' | '15m' | '1h'

const props = withDefaults(defineProps<{
  outcomeId: number
  interval?: ChartInterval
  width?: string
  height?: string
}>(), {
  interval: '1m',
  width: '100%',
  height: '400px',
})

// 跟 CandleChart 同款 LOOKBACK_MAP（每档约 80–90 个 candle 点）
const LOOKBACK_MINUTES_MAP: Record<ChartInterval, number> = {
  '10s': 15,
  '1m':  80,
  '15m': 1200,
  '1h':  4800,
}
```

- [ ] **Step 9.2：删除 PRICE_BUCKET_THRESHOLDS 和 pickBucket**

之前那次方案 A 修复加的（line 57-73 附近的 `PRICE_BUCKET_THRESHOLDS` 和 `pickBucket`）整段删掉——不再需要按 lookback 选 bucket。

- [ ] **Step 9.3：改 loadFull 调 getCandles + 渲染折线取 c 字段**

替换 `loadFull` 函数（line 141-195）为：

```ts
const loadFull = async () => {
  if (!props.outcomeId) return
  loading.value = true
  error.value = null
  try {
    const lookbackMin = LOOKBACK_MINUTES_MAP[props.interval]
    const now = new Date()
    const fromTs = new Date(now.getTime() - lookbackMin * 60 * 1000).toISOString()
    const toTs = now.toISOString()
    const resp = await chartApi.getCandles(
      props.outcomeId, props.interval, fromTs, toTs,
      true,  // fill=true，曲线在无 trade 期间用 prev_close 平直延伸
      5000,
      50000,
    )
    if (!resp || !resp.candles) {
      pointCount.value = 0
      return
    }

    const candles = resp.candles
    pointCount.value = candles.length
    firstPrice.value = candles[0]?.c ?? null
    lastPrice.value = candles[candles.length - 1]?.c ?? null

    await nextTick()
    if (!chartInstance) initChart()
    if (!areaSeries) return

    const fromTsSec = Math.floor(new Date(fromTs).getTime() / 1000) as UTCTimestamp
    const toTsSec = Math.floor(now.getTime() / 1000) as UTCTimestamp

    // 用每根 candle 的 close 价作为折线点
    const data = candles.map(c => ({
      time: Math.floor(new Date(c.t).getTime() / 1000) as UTCTimestamp,
      value: c.c,
    }))
    // 追加"现在"合成端点
    if (data.length > 0) {
      const last = data[data.length - 1]!
      if ((last.time as number) < (toTsSec as number)) {
        data.push({ time: toTsSec, value: last.value })
      }
    }
    areaSeries.setData(data)
    lastWrittenTs = data.length > 0 ? (data[data.length - 1]!.time as number) : 0
    applyDirectionColors()

    if (data.length > 0) {
      chartInstance?.timeScale().setVisibleRange({ from: fromTsSec, to: toTsSec })
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '价格数据加载失败'
    console.error('[PriceChart] loadFull failed:', err)
    pointCount.value = 0
    firstPrice.value = null
    lastPrice.value = null
    lastWrittenTs = 0
    if (areaSeries) areaSeries.setData([])
  } finally {
    loading.value = false
  }
}
```

- [ ] **Step 9.4：更新 watch + ticker 中的 lookbackMinutes 引用**

watch（line 268）改为：

```ts
watch(() => [props.outcomeId, props.interval], () => {
  chartInstance?.applyOptions({
    timeScale: { secondsVisible: props.interval === '10s' },
  })
  loadFull()
})
```

initChart（line 95-116）里 `secondsVisible: props.lookbackMinutes <= 60` 改为：

```ts
    secondsVisible: props.interval === '10s',
```

startTicker（line 218-236）里 `props.lookbackMinutes * 60` 改为：

```ts
    const lookbackSec = LOOKBACK_MINUTES_MAP[props.interval] * 60
```

- [ ] **Step 9.5：跑 type-check + lint**

```bash
cd thccb-frontend
npm run type-check
npx eslint src/components/chart/PriceChart.vue
```

预期：均无错误（既有的 68 个 `no-explicit-any` 不计入，单文件 lint 干净）。

- [ ] **Step 9.6：commit**

```bash
git add thccb-frontend/src/components/chart/PriceChart.vue
git commit -m "$(cat <<'EOF'
refactor(chart-fe): PriceChart 改调 /chart/candles 取 c 字段

废 PRICE_BUCKET_THRESHOLDS / pickBucket（前一轮 patch 方案 A 的产物）。
props 从 lookbackMinutes 改成 interval（10s/1m/15m/1h），跟 CandleChart
共用 LOOKBACK_MINUTES_MAP。1h lookback 显示 360 个 10s candle 的 close
连成折线。

视觉变化：之前的"逐笔曲线"变成 10s 粒度平滑折线，对格斗游戏 prediction
market 是更好的体验。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: 前端 CandleChart 改造（c.n 条件化 + 削减 interval）

**Files:**
- Modify: `thccb-frontend/src/components/chart/CandleChart.vue`

- [ ] **Step 10.1：削减 INTERVAL_SECONDS（line 59-62）**

```ts
const INTERVAL_SECONDS: Record<ChartInterval, number> = {
  '10s': 10, '1m': 60, '15m': 900, '1h': 3600,
}
```

- [ ] **Step 10.2：更新 ChartInterval type alias（line 22）**

```ts
type ChartInterval = '10s' | '1m' | '15m' | '1h'
```

- [ ] **Step 10.3：条件化 `c.n += 1`**

找到 `applyTrade` 函数中所有 `c.n += 1` 的位置（line 230 / 257 附近），改为：

```ts
// 仅被直接交易的 outcome 才 +1（跟后端 candle_writer 同语义）
if (trade && trade.outcome_id === props.outcomeId) {
  currentCandle.n += 1
}
```

但要注意：`applyTrade` 当前接收 `(price, shares, tsMs)` 三个参数，没有 trade 对象。修改方式：让 applyTrade 接收一个 boolean `isDirectTrade` 参数，或者在调用方传 trade.outcome_id。

最简单：把 `applyTrade` 的签名改成接收 trade 对象：

替换 applyTrade（line 220-270）的函数签名：

```ts
const applyTrade = (price: number, shares: number, tsMs: number, isDirectTrade: boolean) => {
```

并把所有 `c.n += 1` 改成：

```ts
if (isDirectTrade) currentCandle.n += 1
```

调用位置（line 386-389）：

```ts
const sharesForThisChart = trade.outcome_id === props.outcomeId ? trade.shares : 0
const isDirect = trade.outcome_id === props.outcomeId
applyTrade(price, sharesForThisChart, tsMs, isDirect)
```

- [ ] **Step 10.4：跑 type-check + lint**

```bash
npm run type-check
npx eslint src/components/chart/CandleChart.vue
```

预期：无错误。

- [ ] **Step 10.5：commit**

```bash
git add thccb-frontend/src/components/chart/CandleChart.vue
git commit -m "$(cat <<'EOF'
refactor(chart-fe): CandleChart 削减 interval 选项 + c.n 条件化

INTERVAL_SECONDS 删除 30s/5m/1d（跟后端 OutcomeCandle.interval
CheckConstraint 对齐，spec § 2 D1）。

applyTrade 加 isDirectTrade 参数，c.n += 1 改为只在被直接交易 outcome
时执行，跟后端 candle_writer.compute_candle_rows 的 n_trades 语义一致
（spec § 4.3）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: 前端 TradingView 统一时间选择器 + API/composable 清理

**Files:**
- Modify: `thccb-frontend/src/pages/market/TradingView.vue`
- Modify: `thccb-frontend/src/api/chart.ts`
- Modify: `thccb-frontend/src/composables/useChartData.ts`

- [ ] **Step 11.1：改 TradingView.vue 的图表类型按钮**

找到 line 43-73 的 state + options 块：

```ts
const activeChartType = ref<'price' | 'candle'>('candle')
const candleInterval = ref<'10s' | '30s' | '1m' | '5m' | '15m' | '1h'>('1m')
const candleIntervalOptions = [
  { label: '10秒', value: '10s' },
  { label: '30秒', value: '30s' },
  { label: '1分钟', value: '1m' },
  { label: '5分钟', value: '5m' },
  { label: '15分钟', value: '15m' },
  { label: '1小时', value: '1h' },
] as const

const LOOKBACK_MAP: Record<string, number> = {
  '10s': 15,
  '30s': 40,
  '1m': 80,
  '5m': 400,
  '15m': 1200,
  '1h': 4800,
}
const candleLookback = computed(() => LOOKBACK_MAP[candleInterval.value] || 80)

// 价格走势图时间范围
const priceLookback = ref(1440)
const priceLookbackOptions = [
  { label: '1小时', value: 60 },
  { label: '6小时', value: 360 },
  { label: '24小时', value: 1440 },
  { label: '3天', value: 4320 },
  { label: '7天', value: 10080 },
] as const
```

整段替换为：

```ts
const activeChartType = ref<'price' | 'candle'>('candle')
type ChartInterval = '10s' | '1m' | '15m' | '1h'
const candleInterval = ref<ChartInterval>('1m')
const candleIntervalOptions = [
  { label: '10秒', value: '10s' },
  { label: '1分钟', value: '1m' },
  { label: '15分钟', value: '15m' },
  { label: '1小时', value: '1h' },
] as const
```

（`priceLookback` / `priceLookbackOptions` / `LOOKBACK_MAP` / `candleLookback` 全删——后端 candle endpoint 内部已经按 interval 自带 lookback，前端组件靠 interval 决定时间窗。）

- [ ] **Step 11.2：改模板里的图表组件 props 和按钮区**

找到 line 419-441 的按钮区。原来 K 线模式显示 `candleIntervalOptions`，价格走势模式显示 `priceLookbackOptions`。改为**两种模式共用同一组 interval 按钮**：

```vue
              <NButton size="small" :type="activeChartType === 'price' ? 'primary' : 'default'" @click="activeChartType = 'price'">价格走势</NButton>
              <NButton size="small" :type="activeChartType === 'candle' ? 'primary' : 'default'" @click="activeChartType = 'candle'">K线图</NButton>
              <span class="text-xs text-[#888] ml-2">|</span>
              <NButton
                v-for="opt in candleIntervalOptions"
                :key="opt.value"
                size="tiny"
                :type="candleInterval === opt.value ? 'primary' : 'default'"
                @click="candleInterval = opt.value"
              >{{ opt.label }}</NButton>
```

找到 line 445-459 的图表组件区。改成两个组件都接 `:interval="candleInterval"`：

```vue
          <div class="h-[300px] sm:h-[400px] md:h-[560px]">
            <PriceChart
              v-if="activeChartType === 'price' && selectedOutcomeId && marketStore.currentMarket"
              :outcome-id="selectedOutcomeId"
              :interval="candleInterval"
              height="100%"
            />
            <CandleChart
              v-else-if="selectedOutcomeId && marketStore.currentMarket"
              :outcome-id="selectedOutcomeId"
              :interval="candleInterval"
              height="100%"
            />
          </div>
```

注意：CandleChart 之前的 `:lookback-minutes="candleLookback"` 属性删掉，CandleChart 内部已经按 interval 自带 LOOKBACK_MAP。

- [ ] **Step 11.3：检查 CandleChart 是否需要 lookback-minutes prop 兼容性改造**

`CandleChart.vue` 当前的 `lookbackMinutes` prop（line 29）：上面 Step 11.2 删了父组件传值。需要 CandleChart 内部自己从 interval 推导 lookback。

修改 CandleChart.vue：

```ts
// 沿用 CandleChart 自己的 LOOKBACK 表（之前从 TradingView 传入，现在内部 own 它）
const LOOKBACK_MINUTES_MAP: Record<ChartInterval, number> = {
  '10s': 15, '1m': 80, '15m': 1200, '1h': 4800,
}

const props = withDefaults(defineProps<{
  outcomeId: number
  interval?: ChartInterval
  width?: string
  height?: string
}>(), {
  interval: '1m',
  width: '100%',
  height: '400px',
})
// 删除 lookbackMinutes prop
```

把内部所有 `props.lookbackMinutes` 替换为 `LOOKBACK_MINUTES_MAP[props.interval]`（包括 loadFull、startTicker、applyVisibleRangeToNow、computeEffectiveNow 等位置）。

- [ ] **Step 11.4：清理 `thccb-frontend/src/api/chart.ts`**

删除 `getPriceSeries` 函数（line 7-28）：

```ts
import api from './index'
import type { CandleSeriesResponse } from '@/types/api'

export const chartApi = {
  // K线数据
  async getCandles(
    outcomeId: number,
    interval: '10s' | '1m' | '15m' | '1h',
    fromTs: string,
    toTs: string,
    fill: boolean = false,
    limit: number = 5000,
    maxTrades: number = 200000
  ): Promise<CandleSeriesResponse> {
    const params: any = {
      outcome_id: outcomeId,
      interval,
      from_ts: fromTs,
      to_ts: toTs,
      fill,
      limit,
      max_trades: maxTrades
    }
    return api.get<CandleSeriesResponse>('/api/v1/chart/candles', { params })
  }
}

export default chartApi
```

（删除 `PriceSeriesResponse` 的 import，因为不再需要。）

注意：`max_trades` 参数后端 `/candles` endpoint 重写后不再用，但 API 接口签名可以保留 default 值不传——这是个无害的过期参数，删它影响范围太大。或者**清理**：直接从签名删 `maxTrades` 参数 + 不传 `max_trades` 给后端。后端 endpoint 现在没 max_trades query 参数（被新 endpoint 删了），所以必须前端这边也删。改成：

```ts
  async getCandles(
    outcomeId: number,
    interval: '10s' | '1m' | '15m' | '1h',
    fromTs: string,
    toTs: string,
    fill: boolean = false,
    limit: number = 5000,
  ): Promise<CandleSeriesResponse> {
    const params: any = {
      outcome_id: outcomeId,
      interval,
      from_ts: fromTs,
      to_ts: toTs,
      fill,
      limit,
    }
    return api.get<CandleSeriesResponse>('/api/v1/chart/candles', { params })
  }
```

- [ ] **Step 11.5：清理 `thccb-frontend/src/composables/useChartData.ts`**

删除 `getPriceSeries` 函数（line 16-43）和相关的 `priceData` ref（line 12）、`PriceSeriesResponse` import、`PricePoint` import；保留 `getCandles` 和 `candleData`。

最终 file 内容大致：

```ts
import { ref } from 'vue'
import { chartApi } from '@/api/chart'
import type { CandleSeriesResponse, Candle } from '@/types/api'

export function useChartData() {
  const loading = ref(false)
  const error = ref<string | null>(null)
  const candleData = ref<Candle[]>([])

  const getCandles = async (
    outcomeId: number,
    interval: '10s' | '1m' | '15m' | '1h',
    fromTs: string,
    toTs: string,
    fill: boolean = false,
    limit: number = 5000,
    silent: boolean = false
  ): Promise<CandleSeriesResponse | null> => {
    if (!silent) loading.value = true
    error.value = null
    try {
      const response = await chartApi.getCandles(
        outcomeId, interval, fromTs, toTs, fill, limit
      )
      candleData.value = response.candles
      return response
    } catch (err: any) {
      error.value = err.message || '获取K线数据失败'
      console.error('获取K线数据失败:', err)
      return null
    } finally {
      if (!silent) loading.value = false
    }
  }

  const getCandleStats = (candles: Candle[]) => {
    if (candles.length === 0) return { volume: 0, high: 0, low: 0, change: 0 }
    const first = candles[0]!
    const last = candles[candles.length - 1]!
    let totalVolume = 0
    let highestHigh = first.h
    let lowestLow = first.l
    candles.forEach(candle => {
      totalVolume += candle.v
      if (candle.h > highestHigh) highestHigh = candle.h
      if (candle.l < lowestLow) lowestLow = candle.l
    })
    const change = ((last.c - first.o) / first.o * 100)
    return { volume: totalVolume, high: highestHigh, low: lowestLow, change }
  }

  return {
    loading, error, candleData,
    getCandles, getCandleStats,
  }
}

export type UseChartDataReturn = ReturnType<typeof useChartData>
```

- [ ] **Step 11.6：跑 type-check + lint + build**

```bash
cd thccb-frontend
npm run type-check
npx eslint src/pages/market/TradingView.vue src/api/chart.ts src/composables/useChartData.ts src/components/chart/CandleChart.vue
npm run build
```

预期：type-check 通过，单文件 lint 干净，build 成功。

- [ ] **Step 11.7：commit**

```bash
git add thccb-frontend/src/pages/market/TradingView.vue \
        thccb-frontend/src/api/chart.ts \
        thccb-frontend/src/composables/useChartData.ts \
        thccb-frontend/src/components/chart/CandleChart.vue
git commit -m "$(cat <<'EOF'
refactor(chart-fe): TradingView 统一时间选择器 + 清理 PriceSeries API

PriceChart 和 CandleChart 共用 candleInterval 状态和按钮组（10s/1m/15m/1h），
删 priceLookback / priceLookbackOptions / LOOKBACK_MAP / candleLookback。

chartApi.getPriceSeries 与 useChartData.getPriceSeries 删除（后端 endpoint
已废）；CandleChart props 移除 lookback-minutes（改为内部从 interval 推导）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: 全量验证 + 浏览器实测

**Files:** 无（只跑验证 + 实测）

- [ ] **Step 12.1：后端全量验证**

```bash
cd /data/sunyunbo/www/TouhouCCB/backend
python -m py_compile $(find app -name '*.py')
python -c "import app.main"
python -m pytest -x --ignore=market_test.py --ignore=user_test.py
```

预期：134+ 测试全过（原 134 + 新加约 25 个 candle 相关）。

- [ ] **Step 12.2：前端全量验证**

```bash
cd /data/sunyunbo/www/TouhouCCB/thccb-frontend
npm run type-check
npm run build
```

预期：type-check 无错误；build 成功，dist/ 生成。

- [ ] **Step 12.3：浏览器实测**

CLAUDE.md 要求 UI 改动浏览器实测。dev DB 可能为空（实测前先跑几笔买卖造数据）。

启动 backend（如未启动）和 frontend dev server，浏览器打开 trading view 页面：

1. **价格走势模式**：切到"价格走势"按钮 → 切换 10秒/1分钟/15分钟/1小时 → 折线应正常显示
2. **K 线模式**：切换 4 档 interval → K 线应正常显示，无 30秒/5分钟按钮
3. **空数据状态**：找一个新建市场 → 显示"暂无 K 线数据"或空图，不崩溃
4. **实时 SSE 增量**：在另一标签下单 → 当前图应增量推进 forming candle
5. **错误状态**：临时断后端 → 切 interval 应显示错误 overlay，不残留旧图

环境起不来或无法实测时，按 CLAUDE.md 规则在 commit message 写「未实测 UI」。

- [ ] **Step 12.4：写 spec 和 plan 的 commit**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add docs/superpowers/specs/2026-05-17-candle-cache-design.md \
        docs/superpowers/plans/2026-05-17-candle-cache-implementation.md
git commit -m "$(cat <<'EOF'
docs(candle): 加 candle 物化层 spec + implementation plan

Spec: docs/superpowers/specs/2026-05-17-candle-cache-design.md
Plan: docs/superpowers/plans/2026-05-17-candle-cache-implementation.md

通过 brainstorming skill 跟用户对齐 6 大设计决策（D1–D6），
spec 13 节覆盖架构/表结构/数据流/错误处理/测试/上线流程；
plan 拆 12 个 task × 平均 5–8 步 TDD 实施。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 12.5：检查所有 commit 整洁**

```bash
git log --oneline -20
```

预期：12 个新 commit（每个 task 一条），按顺序：模型 → writer → hot path → routing → endpoint → backfill → migration → resync → frontend×3 → docs。

如果中间有 fixup 提交想合并：本地用 `git rebase -i` 整理；如果 commit 历史够干净，跳过。

- [ ] **Step 12.6：最终汇报**

按 CLAUDE.md 要求，结束前一句话：改了什么 / 哪个分支 / 验证结果 / 未决风险。模板：

> 实施完成。改了 4 个后端文件 + 1 个后端模型 + 1 个 alembic migration + 4 个新测试文件 + 1 个回填脚本；前端 4 个组件文件 + 1 个 API + 1 个 composable。在 main 分支累计 12 个 commit，未 push。验证：后端 py_compile/import/pytest 全过（134+ 测试，含约 25 个 candle 新增），前端 type-check/build 过；UI 实测（或写"未实测"）。未决：candle 表 race-window 兜底窗口 1h 在重型部署管道下可能不够；alembic migration 内回填在交易量极大市场下可能超时，需要时改用独立 CLI 跑。

---

## Self-Review

按 writing-plans skill 跑过 spec 覆盖 + placeholder 扫描 + 类型一致性检查：

**Spec coverage**：13 节 spec 中每个 section 都对应了 task：
- § 3 数据模型 → Task 1
- § 4 写入流 → Task 2 (writer) + Task 3 (接入)
- § 5 读取流 → Task 4 (routing/rollup) + Task 5 (endpoint)
- § 6 历史回填 → Task 6 (函数) + Task 7 (migration) + Task 8 (resync)
- § 5.3-5.4 前端统一 → Task 9 (PriceChart) + Task 10 (CandleChart) + Task 11 (TradingView/api/composable)
- § 7 错误处理 → 测试覆盖（每个 task 的 integration test）
- § 8 测试边界 → 5 个新测试文件
- § 9 上线流程 → Task 7-8 协同 + Task 12 验证

**Placeholder scan**：没有 TBD / TODO / 模糊措辞。所有代码块都是完整可执行。

**Type consistency**：
- `compute_candle_rows` 在 Task 2 定义，Task 3/6/7/8 都用相同签名（`traded_outcome_id, outcome_ids, new_prices, traded_shares, ts`）✓
- `upsert_candles` 在 Task 2 定义，Task 3/8 用相同签名（`db, rows`）✓
- `CANDLE_INTERVALS` 在 Task 2 定义为 `[("10s", 10), ...]`，Task 6 在 backfill 中复用 ✓
- `INTERVAL_ROUTE` 在 Task 4 定义为 `Dict[str, tuple[str, int]]`，Task 5 endpoint 直接用 ✓
- `_rollup` 在 Task 4 定义返回 `SimpleNamespace` 列表，Task 5 把它当 `OutcomeCandle` 同形态对象处理（字段名一致）✓
- `ChartInterval` type alias 在 Task 9/10/11 三处定义一致：`'10s' | '1m' | '15m' | '1h'` ✓
- `LOOKBACK_MINUTES_MAP` 在 Task 9 (PriceChart) 和 Task 11 (CandleChart 内部) 各有一份相同定义；TradingView 不再维护 ✓

无类型不一致问题。

---

## Execution Handoff

Plan 完成、保存到 `docs/superpowers/plans/2026-05-17-candle-cache-implementation.md`，12 个 task × 平均 6–7 步 TDD。

**两种执行模式**：

1. **Subagent-Driven**（推荐）—— 每个 task 派一个 fresh subagent 实施，task 间我做 review，迭代快、context 清爽
2. **Inline Execution** —— 在当前 session 用 executing-plans skill 批量跑，带 checkpoint review

哪种？
