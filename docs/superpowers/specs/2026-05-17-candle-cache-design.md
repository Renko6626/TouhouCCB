# OutcomeCandle 物化表设计

**Date**: 2026-05-17
**Status**: Draft → 待用户审阅
**Context**: 修完前端 bucket 降采样这个"档位 1"应急方案后，决定上"档位 3"——加 OHLCV 物化层，让长窗口图表查询不再依赖逐笔重放，彻底消除 5000 笔硬上限。

---

## 1. 背景与目标

### 1.1 问题

当前 `/api/v1/chart/price` 和 `/api/v1/chart/candles` 都靠 `_fetch_initial_shares_and_replay`（`backend/app/api/v1/chart.py:95-170`）从 Transaction 表**逐笔重放** LMSR：

1. 拉时间窗内所有 Transaction（包括做 initial_shares 反向回退的部分）
2. 在 Python 里跑 NumPy 重放 / 或读 `market_prices_post` JSON 快照
3. 聚合成 OHLCV / 按桶降采样

这个路径有一道硬上限 `if len(replay_rows) > limit: raise 400`——`limit` 默认 5000，hard cap 20000。长时间窗（24h+）触发后 chart endpoint 直接 400。

短期已用前端 `bucket` 降采样 + `limit=20000` 缓解（见 PR 上一轮的 PriceChart.vue 改动），但本质是把"5000 上限"推到"20000 上限"，没消除瓶颈。

### 1.2 目标

- **彻底消除 5000/20000 笔上限**：长窗口查询变成"查物化表 N 行"，跟交易量解耦
- **统一前后端图表数据源**：价格走势图（曲线）和 K 线图共用 `/chart/candles`，废 `/chart/price`
- **零 hot path 性能回归**：buy/sell 事务时长上限 +1ms（30% 涨幅，4ms → 5ms），不打破 10r/s SLA

### 1.3 非目标

- 不引入 TimescaleDB 或其他新依赖（继续用普通 Postgres + asyncpg）
- 不为公开 API / 第三方调用方做兼容（这是内部项目）
- 不做负载测试或大规模 benchmark（课程项目规模）
- 不动 `Transaction.market_prices_post` 字段（继续给 SSE 推送 patch 全 outcome 价用）

---

## 2. 关键设计决策（已对齐）

| # | 决策 | 备选与放弃理由 |
|---|---|---|
| D1 | **桶集合**：物化 `10s/1m/15m/1h` 共 4 档 | 砍掉 `30s/5m/1d`；前两者价值低且增加 43% 写入开销；`1d` 前端从未暴露 |
| D2 | **老桶兼容**：`30s/5m/1d` 由 chart endpoint 现 rollup | 比"全 7 档都物化"省存储 + 写入；rollup 数学等价（30s=10s×3、5m=1m×5、1d=1h×24 都整除） |
| D3 | **写入时机**：buy/sell 事务**内**同步 UPSERT | 异步 worker 方案被否决（APScheduler 在本项目历史上有 lifespan 互锁问题；同步换强一致更划算） |
| D4 | **写入触发**：仅 buy/sell；`settle/settle_lose` 不参与 | 结算非真实成交、且 timestamp 扎堆同一时刻，纳入会让最后一根 candle 异常突变 |
| D5 | **endpoint 统一**：废 `/chart/price`，全走 `/chart/candles` | 前端 PriceChart 改调 `/chart/candles` 取 `c` 字段渲染折线；最细粒度 10s（不再支持"逐笔曲线"） |
| D6 | **历史回填**：alembic migration 内直接回填 + startup hook race-window 兜底扫 | 单步部署最简；兜底扫覆盖"migration 完成到新代码上线"的 5–10 秒窗口 |

---

## 3. 数据模型

### 3.1 新表 `outcome_candle`

```python
# backend/app/models/base.py 新增
class OutcomeCandle(SQLModel, table=True):
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

    # 复合主键（自带 B-tree 索引；UPSERT + 区间扫描双用途）
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
    # 不暴露 outcome relationship；Outcome 也不加反向 candles 关系。
    # 理由：遵守 base.py:61-67 hot path 性能护栏（lazy="raise_on_sql" 精神）。
```

### 3.2 精度选择

| 字段 | 类型 | 对齐字段 |
|---|---|---|
| `open/high/low/close_price` | `Numeric(16, 8)` | `Transaction.price` / `post_market_price` |
| `volume_shares` | `Numeric(16, 6)` | `Outcome.total_shares` / `Transaction.shares` |
| `n_trades` | `Integer` | `Candle.n` schema |

### 3.3 不变性约束

- `open_price` 首次 INSERT 后**永不更新**（bucket 第一笔的价格）
- `close_price` 每次 UPSERT 更新为最新（bucket 最后一笔的价格）
- `high_price` 单调上升，`low_price` 单调下降
- `volume_shares >= 0`，`n_trades >= 0`，`high_price >= low_price`：DB 层 CheckConstraint
- `interval` 限定 4 个值之一：DB 层 CheckConstraint

### 3.4 索引策略

只用复合主键 `(outcome_id, interval, bucket_start)` 的自带 B-tree。覆盖所有查询模式：
`WHERE outcome_id=X AND interval=Y AND bucket_start BETWEEN a AND b`

不加额外 index——写入侧 UPSERT 只动一棵索引，避免 bloat。

---

## 4. 写入流（buy/sell 事务内）

### 4.1 流程

`backend/app/api/v1/market.py:buy_shares` / `sell_shares` 的现有事务里，**在 Transaction INSERT 之后、commit 之前**插入：

```python
# ① 现有：算 LMSR 新价
new_prices: List[float] = ...  # 按 outcome.id 升序

# ② 现有：INSERT Transaction(market_prices_post=new_prices)
db.add(Transaction(...))

# ③ ★ 新增：UPSERT N×4 行 candle
now = datetime.now(timezone.utc)
traded_oid = outcome.id
traded_shares = shares_d

rows = []
for i, o in enumerate(all_outcomes):
    price_i = Decimal(str(new_prices[i]))
    is_traded = (o.id == traded_oid)
    v_i = traded_shares if is_traded else Decimal("0")
    n_i = 1 if is_traded else 0
    for interval, step in [("10s", 10), ("1m", 60), ("15m", 900), ("1h", 3600)]:
        bucket_start = _bucket_start(now, step)
        rows.append({
            "outcome_id": o.id, "interval": interval, "bucket_start": bucket_start,
            "open_price": price_i, "high_price": price_i,
            "low_price": price_i,  "close_price": price_i,
            "volume_shares": v_i, "n_trades": n_i,
        })

await upsert_candles(db, rows)
# ④ commit（现有），SSE 广播（事务外，现有）
```

### 4.2 UPSERT SQL

```python
# backend/app/services/candle_writer.py（新文件）
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

async def upsert_candles(db: AsyncSession, rows: List[dict]) -> None:
    if not rows:
        return
    dialect = db.bind.dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = insert_fn(OutcomeCandle).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["outcome_id", "interval", "bucket_start"],
        set_={
            # open_price 不更新（首次 INSERT 后永久固定）
            "high_price":    func.greatest(OutcomeCandle.high_price, stmt.excluded.high_price),
            "low_price":     func.least(OutcomeCandle.low_price, stmt.excluded.low_price),
            "close_price":   stmt.excluded.close_price,
            "volume_shares": OutcomeCandle.volume_shares + stmt.excluded.volume_shares,
            "n_trades":      OutcomeCandle.n_trades + stmt.excluded.n_trades,
            "updated_at":    func.now(),
        },
    )
    await db.execute(stmt)
```

### 4.3 关键语义

- **联动行的 volume/n**：被直接 buy/sell 的 outcome 行 `v=shares, n=1`；联动的其他 outcome 行 `v=0, n=0`。这跟 volume 含义"该 outcome 实际成交量"一致
- **同 bucket 多笔合并**：ON CONFLICT 路径里 `open_price` 不在 `set_` 中 → 首次 INSERT 的 open 永远保留；`close_price` 每次覆盖；`H/L` 用 `GREATEST/LEAST`；`V/n` 累加
- **DB dialect**：Postgres 生产 + SQLite 测试都支持 `ON CONFLICT (...) DO UPDATE`（SQLite ≥ 3.24）
- **前端 `CandleChart.applyTrade` 的 `c.n += 1` 改动**：当前是无条件 +1（每次 SSE trade 推送都加），改成 `if (trade.outcome_id === props.outcomeId) c.n += 1` —— 跟后端"被直接交易行才 +1"语义对齐

---

## 5. 读取流（chart endpoint）

### 5.1 endpoint 改造

`GET /api/v1/chart/candles?outcome_id=X&interval=Y&from_ts=A&to_ts=B&fill=bool`

```python
INTERVAL_ROUTE = {
    "10s": ("10s", 1),   "30s": ("10s", 3),
    "1m":  ("1m", 1),    "5m":  ("1m", 5),
    "15m": ("15m", 1),
    "1h":  ("1h", 1),    "1d":  ("1h", 24),
}
```

步骤：
1. `storage_interval, rollup_factor = INTERVAL_ROUTE[interval]`
2. 按 `target_step = _INTERVAL_SECONDS[interval]` 对齐请求时间窗（沿用现有 `_align_range_to_buckets`）
3. `SELECT ... FROM outcome_candle WHERE outcome_id=X AND interval=storage_interval AND bucket_start BETWEEN ...`
4. `if rollup_factor > 1: candles = _rollup(fine_candles, target_step, aligned_from)`
5. `fill=true` 时空 bucket 用 prev_close 填（沿用现有 `chart.py:357-367` 模式）
6. Schema 转换 → `CandleSeriesResponse`

### 5.2 Rollup 函数

```python
def _rollup(fine_candles: List[OutcomeCandle], target_step: int, anchor: datetime) -> List[Candle]:
    """按 target_step 重新分桶，组内 OHLC 按数学定义合并。
    O = 第一个细桶 O；C = 最后细桶 C；H = max(H); L = min(L); V = ΣV; n = Σn"""
```

性能预算：30s rollup 24h 窗口 ≈ 8640 行 → 2880 桶，纯 in-memory 几毫秒。

### 5.3 废除 `/chart/price`

- 后端：`@router.get("/price")` 函数删除；`_fetch_initial_shares_and_replay` 仅 `/candles` 用，**保留**（migration 回填脚本也用它）
- 前端：`api/chart.ts:getPriceSeries` 删除；`composables/useChartData.ts:getPriceSeries` / `priceData` 删除
- `PriceChart.vue` 改调 `chartApi.getCandles(outcomeId, interval, ...)`，渲染时取 `c.c` 连折线

### 5.4 前端时间选择器统一

**当前不一致**：
- `CandleChart` 用 `interval` 按钮（10秒/30秒/1分钟/5分钟/15分钟/1小时）→ 每档约 80–90 根 K 线
- `PriceChart` 用 `lookback` 按钮（1小时/6小时/24小时/3天/7天）→ 按时间窗口选

两套按钮逻辑不一样，用户视觉上无法一致比较。

**新方案：两个图共用同一组 `interval` 按钮**：

```
interval   后端 storage    UI 显示   默认 lookback   桶数（≈点数）
─────────────────────────────────────────────────────────────────
10s        直读             "10秒"     15 分钟         90
1m         直读             "1分钟"    80 分钟         80
15m        直读             "15分钟"   20 小时         80
1h         直读             "1小时"    3.3 天          80
```

`LOOKBACK_MAP` 在前后端都是这 4 档（沿用 `CandleChart` 当前的常量）。

`TradingView.vue` 改动：
- 删除 `priceLookback` state 和 `priceLookbackOptions`
- 价格走势模式与 K 线模式**共用** `candleInterval` state 和 `candleIntervalOptions`
- 删除 `candleIntervalOptions` 中的 30秒/5分钟两档（跟 § 2 D1 决策一致）

`PriceChart.vue` 改动：
- props 从 `lookback-minutes: number` 改成 `interval: ChartInterval`
- 内部用同一份 `LOOKBACK_MAP` 算 `fromTs` / `toTs`
- 删除 `PRICE_BUCKET_THRESHOLDS` 和 `pickBucket()`（不再需要按 lookback 选 bucket，因为 interval 就是 bucket）

`CandleChart.vue` 改动：仅删 `candleIntervalOptions` 中两档；其余不变。

**结果**：两图共用 4 档 interval 按钮 + 同一套 lookback 映射；切换图表类型时，时间尺度自然保持不变，体验一致。

---

## 6. 历史回填

### 6.1 Alembic migration

`backend/alembic/versions/2026_05_17_HHMM-<hash>_add_outcome_candle.py`：

1. `op.create_table('outcome_candle', ...)` — 沿用 `2026_05_15_1420-..._add_danmuku_exchange_table.py` 的写法范式
2. 调用 `_backfill_all_markets(connection)`，这个函数定义在迁移文件内部（也可以提取到 `app/services/candle_writer.py` 让生产脚本与 migration 共用）
3. **回填实现策略**：migration 里 Python 迭代 `for market in markets: backfill_one_market(conn, market_id); conn.execute(text("COMMIT")); conn.execute(text("BEGIN"))` —— 按 market 分批提交，避免单巨型事务长锁
4. 幂等保证：回填用 `INSERT ... ON CONFLICT DO NOTHING`，重跑 `alembic upgrade head` 不重复累加 volume

> 注：alembic 默认在单事务里跑 `upgrade()`；手动 `COMMIT/BEGIN` 用于分批提交。这种 pattern 在 `migrate_loan_v1.py` 类似的 init 脚本里有先例。Migration 失败时 alembic 会保留已分批提交的 candle 数据，但 `outcome_candle` 表本身的 CREATE 是回滚的——这是个 inconsistency。**缓解**：CREATE TABLE 单独在第一个子事务里 commit，回填失败时下次 `alembic upgrade` 看到表已存在直接重跑回填（幂等 DO NOTHING 保证安全）。

### 6.2 回填算法

按市场逐个 replay：

```
for market in all_markets:
    for outcome in market.outcomes:
        # 复用 chart.py 的逐笔重放 + market_prices_post 逻辑
        rows = SELECT Transaction WHERE outcome_id IN market.outcome_ids
               AND type IN ('buy', 'sell')                # settle/settle_lose 不参与
               ORDER BY timestamp ASC
        for tx in rows:
            for interval, step in [('10s',10), ('1m',60), ('15m',900), ('1h',3600)]:
                bucket_start = _bucket_start(tx.timestamp, step)
                # 从 tx.market_prices_post 取该 outcome 的 post 价
                # 走同样的 UPSERT 合并逻辑
                upsert_candle(outcome_id, interval, bucket_start, price, ...)
```

### 6.3 Race-window 兜底

`app/main.py` 的 lifespan startup hook 里新增 `_resync_recent_candles()`：

```python
async def _resync_recent_candles():
    """补 migration 完成到新代码上线之间的 race window。
    扫 timestamp > (now - 1h) 的 Transaction，对 candle 表做幂等 UPSERT。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    # 跟回填脚本同样逻辑，但 WHERE timestamp >= cutoff
```

1 小时窗口已经覆盖任何合理 deploy 时长；幂等 UPSERT 让"已经写过的"不会被搞坏。

---

## 7. 错误处理

| 故障 | 表现 | 处理 |
|---|---|---|
| `upsert_candles` 抛异常 | 整个 buy/sell 事务回滚 | 不需特殊处理；Transaction 也不入库，保持一致 |
| 进程崩溃 / 网络断 | 事务未 commit | DB 状态一致：Transaction 表和 candle 表要么都有、要么都没 |
| `new_prices` 长度 ≠ outcome 数 | LMSR 不变性破坏 | `assert` → 事务回滚（这是 bug，应 fail fast） |
| Migration 回填超时 | Single migration 长事务 | 按 market 分块提交（每市场一个事务）；可加 `--market-id` 单跑 |
| 数据类型溢出 | `Numeric(16, 8)` 写超 | LMSR 价 ∈ [0,1]、shares ≤ 持仓上限，物理上不可能触发 |
| Rollup 因子不整除 | 桶错位 | DB CheckConstraint 限定 `interval` 取值；`INTERVAL_ROUTE` 静态表手算验证 |
| HALT 期间有 trade | 不可能（`_require_trading()` 拦截）| 不需处理 |

---

## 8. 测试边界

### 8.1 新增测试文件

| 文件 | 范围 |
|---|---|
| `tests/test_candle_writer.py` | `upsert_candles` 单元 + PG/SQLite dialect 行为对齐 |
| `tests/test_candle_rollup.py` | `_rollup` + `INTERVAL_ROUTE` 单元 |
| `tests/test_candle_integration.py` | 通过 buy/sell API 验 candle 副作用 |
| `tests/test_chart_endpoint.py` | `/chart/candles` 直读 + rollup + fill |
| `tests/test_candle_backfill.py` | migration 回填正确性 + 幂等 |

### 8.2 关键测试用例

**单元层**：
- `_bucket_start(ts, step)` 跨秒/分/时边界对齐
- `_rollup(fine, factor)` OHLC 数学定义（手算 3 桶合并）
- `INTERVAL_ROUTE` 整除验证

**集成层（核心）**：
- 单笔 buy → candle 表 N×4 行 INSERT，被交易行 `v=shares, n=1`，联动行 `v=0, n=0`
- 同 bucket 连续 buy：O 不变、C 更新、H/L 收紧、V/n 累加
- 跨 bucket：新 bucket 行 INSERT，老行不被污染
- sell 路径同 buy 镜像
- `settle` 不触发 candle 写入：`/resolve` 后 candle 表行数无新增
- HALT 拒绝 buy → candle 表无新行；resume 后正常

**Endpoint 层**：
- 直读 `interval=10s` 返回原始桶
- rollup `interval=30s` / `5m` / `1d` 跟手算结果对齐
- `fill=true` 空 bucket 用 prev_close
- 跨 HALT 时段查询：HALT 段 bucket 缺失或被填

**回填层**：
- migration 跑完后老 Transaction 完整反映在 candle 表
- migration 幂等：重跑不污染（ON CONFLICT DO NOTHING）
- 回填结果 vs 实时积累 vs 同笔交易 → 等价

### 8.3 测试 Seed 工厂

复用 `tests/test_market_slippage_lock.py:_make_market` 模式，新增 `_make_trade` helper 走真实 API 而非直接 `s.add(Transaction(...))`，确保 hot path 副作用全被覆盖。

### 8.4 显式跳过

- 并发 race（Postgres 行锁 + asyncio 单进程顺序已保证）
- 大规模负载（属 bench 范畴，非 test）
- 100k+ Transaction 的回填性能（项目规模不到）

---

## 9. 上线流程

1. **Step 1**：合 PR——alembic migration + 新模型 + `upsert_candles` + buy/sell 接入 + `/chart/candles` 改造 + `/chart/price` 删除 + 前端 PriceChart 改调
2. **Step 2**：`git push main` 触发部署
3. **部署管道**：
   - `alembic upgrade head` 跑 migration：建表 + 历史回填
   - 新代码起服务
   - startup hook `_resync_recent_candles()` 兜底扫近 1 小时 Transaction
4. **Step 3（手动验证）**：浏览器打开 K 线视图切 24h/7d，确认不再 400

---

## 10. 性能预算

| 操作 | 估算 | 触发频率 |
|---|---|---|
| 写：multi-row UPSERT N×4 行 | ~1ms | 每笔 buy/sell（10r/s SLA）|
| 读：直读单 interval、≤1000 桶 | < 5ms | chart 查询（用户操作）|
| 读：rollup factor=3，≤3000 细桶 | ~3ms | 兼容 `30s` 查询 |
| 读：rollup factor=24 | < 1ms | 兼容 `1d` 查询 |
| Migration 回填：单 market 历史 | < 数秒 | 仅部署时一次 |

事务时长预算：从 ~4ms（无 candle）涨到 ~5ms（+1ms candle UPSERT），30% 涨幅，仍远低于 10r/s SLA 下的可接受时长。

---

## 11. 安全 / 红线检查

参考 `CLAUDE.md`：

- 改的文件包括 `backend/app/api/v1/market.py`（高敏感）和 `backend/app/models/base.py`（高敏感字段定义）→ **要走 alembic autogenerate + 人工 review**
- 新增反向关系**不加**（保持 `lazy="raise_on_sql"` 精神）
- 数据精度 `Numeric(16, 8)` / `Numeric(16, 6)` 沿用项目约定
- backfill 脚本风格沿用 `scripts/backfill_market_prices_post.py`
- 测试覆盖 buy/sell hot path 不破坏

---

## 12. 未决问题（待后续 plan 阶段或运行时确认）

1. **Race-window 兜底窗口长度**：当前定 1 小时，是否够？取决于实际部署管道时长。用户已确认"应该没问题"
2. **Migration 回填的批大小**：每 market 一个事务是否够细？真有交易量极大的市场（短期不会发生）再说

---

## 13. 文件改动清单（implementation plan 用）

**后端新增**：
- `backend/app/models/base.py`：新 class `OutcomeCandle`
- `backend/app/services/candle_writer.py`（新文件）：`upsert_candles()`
- `backend/alembic/versions/2026_05_17_HHMM-<hash>_add_outcome_candle.py`（新迁移）：建表 + 回填
- `backend/scripts/backfill_outcome_candle.py`（可选，作为 migration 内 Python 调用的实现）：单独 CLI 也能跑
- `backend/app/main.py`：lifespan startup hook 加 `_resync_recent_candles()`

**后端修改**：
- `backend/app/api/v1/market.py:buy_shares` / `sell_shares`：事务内追加 `upsert_candles` 调用
- `backend/app/api/v1/chart.py`：删 `/price` endpoint、重写 `/candles` 走 candle 表 + rollup + fill；移除 `_fetch_initial_shares_and_replay` 在 `/candles` 的调用（回填脚本仍用）

**前端修改**：
- `thccb-frontend/src/components/chart/PriceChart.vue`：
  - props 从 `lookback-minutes` 改为 `interval`
  - 改调 `getCandles`，渲染折线取 `c.c`
  - 删除 `PRICE_BUCKET_THRESHOLDS` / `pickBucket()`（不再需要）
  - 使用与 `CandleChart` 同款 `LOOKBACK_MAP` 算 `fromTs/toTs`
- `thccb-frontend/src/components/chart/CandleChart.vue`：
  - `applyTrade` 中 `c.n += 1` 改为仅被直接交易 outcome 时才 +1
  - `INTERVAL_SECONDS` 移除 `30s/5m/1d`（与后端 CheckConstraint 对齐）
- `thccb-frontend/src/pages/market/TradingView.vue`：
  - 删除 `priceLookback` state 和 `priceLookbackOptions`
  - 价格走势模式与 K 线模式共用 `candleInterval` 与 `candleIntervalOptions`
  - `candleIntervalOptions` 移除 30秒/5分钟两档
- `thccb-frontend/src/api/chart.ts`：删 `getPriceSeries`
- `thccb-frontend/src/composables/useChartData.ts`：删 `getPriceSeries` / `priceData`

**测试新增**：
- `tests/test_candle_writer.py`
- `tests/test_candle_rollup.py`
- `tests/test_candle_integration.py`
- `tests/test_chart_endpoint.py`
- `tests/test_candle_backfill.py`

**文档**：
- 本 spec
- 后续 implementation plan
