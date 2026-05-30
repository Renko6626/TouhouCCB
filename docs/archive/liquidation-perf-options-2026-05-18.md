# 强制平仓 sweep 性能优化清单

> 状态：调研档案，未实施。明天开 perf 分支照此实现。
> 触发条件：`feat/forced-liquidation` 合 main 之后单开 `perf/liquidation-sweep-batched`。

## 背景

`feat/forced-liquidation` 已实现 10 min 定时 sweep，默认关闭，可通过 admin 切换。
当前实现在 100 用户规模下一次 sweep 约 **2.3 秒**，绝大多数时间花在串行的 DB
roundtrip 上。如果将来想：
- 把 sweep tick 提到秒级（应对 LMSR 单笔 ±10% 波动 + 砸盘瀑布场景）
- 配合「成交后 per-market debounce 实时检查」方案

那当前实现是性能瓶颈，需要重写。但 **YAGNI：当前规模下 10 min 间隔够用，
此清单仅作未来参考**。

## 当前一次 sweep 的真实 query 流水

100 用户其中 30 个 debt > 0、5 个越线（margin < 0.2）为例：

```
阶段 A: 启动配置（独立 session）
├─ Q1: SELECT site_config WHERE key='liquidation_enabled'       ← 串行 1
├─ Q2: SELECT site_config WHERE key='liquidation_hard_threshold' ← 串行 2
├─ Q3: SELECT site_config WHERE key='liquidation_soft_threshold' ← 串行 3
└─ Q4: SELECT site_config WHERE key='loan_daily_rate'           ← 串行 4

阶段 B: 候选用户
└─ Q5: SELECT id FROM user WHERE debt > 0

阶段 C: 30 用户 × 串行处理（每用户独立 session）
  for each uid (30 次串行):
  ├─ Qa: SELECT user FOR UPDATE                                  ← 持锁开始
  ├─ Qb: SELECT position selectinload(outcome→market)            ← 实际 3 query
  ├─ Qc: SELECT outcome WHERE market_id IN (...) ORDER BY        
  └─ commit + 释放锁

阶段 D: 5 个越线用户额外（嵌在 C 中）
  ├─ Qd: SELECT outcome FOR UPDATE WHERE market_id=? (每 market 一次)
  ├─ flush (INSERT transactions / DELETE positions / UPDATE outcomes)
  ├─ Qe: SELECT user FOR UPDATE (decrease_debt 内部又锁一次！)
  └─ flush (INSERT liquidation_event)
```

**总 query 数**：4 + 1 + 30 × 5 + 5 × (3~5) ≈ **170+ 个 query 串行**。

## 核心洞察：所有瓶颈都在 DB 交互

Python 端计算（LMSR + Decimal + 比较）加起来 < 5ms，可完全忽略。瓶颈是
**DB roundtrip 数**和**持锁窗口长度**两件事。

写操作 SQLAlchemy 已经在 flush 时自动 batch（`executemany`），不是瓶颈。
真正能压的是：
- **read query 数**：用 JOIN 合并多次 read
- **持锁窗口**：锁内只做必要的写、避免重复锁同一行
- **跨用户并发**：阶段 2 越线用户用 `asyncio.gather`

写已经是「一次搞定」，read 和 lock 才是真问题。

## 愚蠢点清单（按收益排）

| # | 愚蠢点 | 当前 | 改后 | 收益 | 难度 |
|---|---|---|---|---|---|
| 🥇 1 | `compute_users_holdings_value` 每用户独立调，30 × (3-4) query | 90-120 query | 3 query | **省 ~1500ms** | 中 |
| 🥈 2 | `liquidate_user` 内多次串行 read + per-market 锁 | 8-10 roundtrip | 1 个大 JOIN | **持锁 80→25ms** | 中 |
| 🥉 3 | 安全用户也 `FOR UPDATE` 锁 user 行 | 25 行锁 | 0 行锁 | 不阻塞 BUY/SELL | 中 |
| 4 | 4 个 site_config 串行查 4 次 | 4 query | 1 query | 省 ~10ms | 低 |
| 5 | `liquidate_user` 同 user 多 position 同 market，重复 lock outcomes | N 次 | group → 1 次 | 省 N query/强平 | 低 |
| 6 | `decrease_debt` 对已锁 user 又 SELECT FOR UPDATE + 多 1 次 flush | 重锁 1 次 + 1 flush | 复用 user 对象 | 省 ~10ms 持锁 | 低（动 loan_service） |
| 7 | 每安全用户独立 session/transaction | 30 sessions | 1 session（阶段 1） | 省 connect 开销 | 低（#1 副产品） |
| 8 | 越线用户串行强平 | 5 × 80ms = 400ms | gather + semaphore | **5x → ~80ms** | 中 |
| 9 | tick 高频时 site_config 也高频查 | 高频读 | 进程内 30s TTL | 省 ~5ms/tick | 低（仅 tick→秒级时做） |

## 不在清单上的优化（明确不做）

- **numpy 向量化 LMSR**：Python 端 600 次 LMSR ≈ 3-6ms，整 sweep 460ms 里占 < 1%。
  动 `lmsr.py`（高敏感）+ scipy 依赖 + 测试负担。**ROI 太低**。
- **同 market `old_cost` cache**：边际优化 ~3ms。除非顺手做。
- **内存 margin cache / 优先队列**：一致性灾难。hobby 站不值。
- **DB raw SQL 跳 ORM**：维护性差。
- **跨用户合并 LMSR 卖出**：理论上更公平（同 market 多人同时被强平按平均价
  而非"先平先占便宜"），但跨用户原子事务复杂度爆炸 + attribution 模糊。
  公平性增益小于复杂度代价。
- **单用户多 outcome 合并 LMSR**：LMSR path-independent，合并 vs 串行总 proceeds
  完全相同，只是每笔 Transaction 的 sub-proceeds attribution 改语义。0 实质收益。
- **`.env` 加层灰度总闸**：现有 `site_config.liquidation_enabled` 默认 false
  已经够安全，加 .env 闸门只是多一道认知负担。
- **乐观锁 / CAS retry**：LMSR 热门市场上 `outcomes.total_shares` 高度争用，
  optimistic 会无限 retry，**只能用悲观 FOR UPDATE**。

## 改造方案（按 step 独立可回滚）

### Step 1：`site_config.get_many` 批量取

`backend/app/services/site_config.py`:
```python
async def get_many(
    session: AsyncSession, keys: list[str]
) -> dict[str, str]:
    result = await session.execute(
        select(SiteConfig).where(SiteConfig.key.in_(keys))
    )
    return {c.key: c.value for c in result.scalars()}
```

`liquidation_sweep.py` 启动阶段改成一次拿全：
```python
cfg = await site_config.get_many(session, [
    "liquidation_enabled", "liquidation_hard_threshold",
    "liquidation_soft_threshold", "loan_daily_rate",
])
# 解析后类型转换分别交 helpers
```

### Step 2 (核心)：sweep 两阶段化 + 批量 holdings_value

`wealth.py` 的 `compute_users_holdings_value(user_ids=[...])` 已经支持批量调用，
但 sweep 当前在循环里每次只传一个 uid → 完全没用上批量能力。

**新流程**：

```python
# 阶段 1 — read-only，零锁
async with async_session_maker() as session:
    user_rows = (await session.execute(
        select(User.id, User.cash, User.debt).where(User.debt > 0)
    )).all()
    uids = [r.id for r in user_rows]

    # 一次性算所有人的 holdings_value
    hvs = await compute_users_holdings_value(session, user_ids=uids)

    over_hard: list[int] = []
    over_soft: list[tuple[int, Decimal]] = []
    for r in user_rows:
        hv = hvs.get(r.id, Decimal("0"))
        nw = r.cash - r.debt + hv
        margin = nw / r.debt
        if margin < hard_thr:
            over_hard.append(r.id)
        elif margin < soft_thr:
            over_soft.append((r.id, margin))

# 阶段 2 — 仅越线用户，独立 session 加锁
for uid in over_hard:
    if _recently_attempted.get(uid, 0) + _STUCK_COOLDOWN_SEC > now:
        continue
    async with async_session_maker() as session:
        async with session.begin():
            user = await lock_user(session, uid)
            # 关键：lock 后必须重算 margin 防止 stale read
            hv_now = (await compute_users_holdings_value(
                session, user_ids=[uid]
            )).get(uid, Decimal("0"))
            margin_now = (user.cash - user.debt + hv_now) / user.debt
            if margin_now >= hard_thr:
                continue  # 已恢复
            await liquidation_service.liquidate_user(...)
```

**关键设计点**：
- 阶段 1 read-only，对 BUY/SELL 零阻塞
- 阶段 2 lock 后**必须重算 margin** 防 staleness（读 → 锁之间可能被并发交易改）
- 阶段 2 也可以 `asyncio.gather` 并发（每个独立 session），但要注意 `DB_POOL_SIZE`
  和同 market 锁竞争——并发度限 3-5

### Step 3：`liquidate_user` 按 market 分组 positions

`liquidation_service.py:80-95`:
```python
# 先按 market 分组，每 market 只锁一次 outcomes
positions_by_market: dict[int, list[Position]] = {}
for pos in positions:
    positions_by_market.setdefault(pos.outcome.market_id, []).append(pos)

for market_id, pos_group in positions_by_market.items():
    all_outcomes = await lock_outcomes_for_market(session, market_id)
    for pos in pos_group:
        # ...现有平仓逻辑...
```

收益：用户在同 market 有 N 个 position 时，N 次 outcomes 锁 → 1 次。

### Step 4：`liquidate_user` 内 read 合成 1 个大 JOIN（持锁窗口压缩）

当前 `liquidate_user` 内部 read 链路：

```
1. SELECT user FOR UPDATE
2. SELECT positions selectinload(outcome→market)    ← 3 query
3. compute_users_holdings_value pre-snapshot       ← 又跑 3-4 query
4. for each market: SELECT outcomes FOR UPDATE     ← N query
```

合成一个大 JOIN，同时锁所有相关 outcomes：

```sql
SELECT p.*, o.id as oid, o.market_id, o.total_shares, m.liquidity_b
FROM position p
JOIN outcome o ON o.id = p.outcome_id
JOIN market m ON m.id = o.market_id
WHERE p.user_id = :uid AND p.amount > 0 AND m.status = 'TRADING'
ORDER BY o.market_id, o.id
FOR UPDATE OF o    -- 同时锁所有 outcomes，按 market_id 排序避死锁
```

**关键收益**：read roundtrip **8-10 → 1**，持锁时间 **80ms → ~25ms**（3x 压缩）。

这意味着：
- 同 market 其他用户的 BUY/SELL 阻塞窗口减半
- 跨用户并发强平时连接池压力减半
- 死锁概率降低

成本：要写复杂的 SQLAlchemy DSL 或 raw SQL；要测试 ORM relationship 是否
还能正常用（或显式 from_statement + populate_existing）。

### Step 5：`decrease_debt` 接受已锁 user 对象

当前在 `liquidate_user` 内会：`session.flush()` 把 user.cash 落库 → 调
`decrease_debt(session, user.id, ...)`，后者**又 SELECT FOR UPDATE 同一 user**。

改：`decrease_debt(session, user, ...)` 直接传 in-memory 对象：

```python
async def decrease_debt(
    session: AsyncSession,
    user: User,         # 改：直接传对象，调用方保证已 lock
    amount: Decimal,
    *,
    consume_cash: bool,
    daily_rate: Decimal,
) -> tuple[User, Decimal]:
    # 不再 SELECT FOR UPDATE；直接用传入的 user
    now = _compat_now(user)
    accrue_interest(user, daily_rate, now)
    ...
```

**收益**：省 1 个 SELECT FOR UPDATE + 1 次 flush ≈ **10ms 持锁时间**。

**风险**：动 `loan_service.py`（接近高敏感）；要保证所有调用方都已 lock user，
或者重载支持两种签名。建议改成显式新函数 `decrease_debt_for_locked_user()`
保留原 API，逐步迁移。

### Step 6：跨用户并发强平

阶段 2 当前是 `for uid in over_hard: ...` 串行。5 用户 × 80ms = 400ms。

改 `asyncio.gather` + Semaphore 限流：

```python
sem = asyncio.Semaphore(3)
async def liquidate_one(uid: int):
    async with sem:
        async with async_session_maker() as session:
            async with session.begin():
                # ... lock_user → 重算 margin → liquidate_user ...

await asyncio.gather(*[liquidate_one(uid) for uid in over_hard])
```

**收益**：5 用户场景 400ms → ~80ms（max + semaphore overhead），**5x 加速**。

**风险**：
- 不同用户持仓重叠到同 market 时，outcomes 锁会串行化 → semaphore 防止
  过多用户堆在同一锁等待
- 连接池：`DB_POOL_SIZE=10`，并发度 ≤3 安全
- 单个用户出错（如 deadlock）不影响其他用户的并发处理（gather 用
  `return_exceptions=True`，逐个收集 result）

### Step 7（可选，仅秒级 tick 才需要）：进程内 site_config cache

```python
_cfg_cache: dict[str, tuple[Any, float]] = {}
TTL_SEC = 30
```

仅在 tick 缩短到秒级、且 site_config 读真的成 hotspot 时再做。

## 预期效果对照

| 阶段 | 当前 | +Step 1+2 | +Step 3+5 | +Step 4 (JOIN) | +Step 6 (并发) |
|---|---|---|---|---|---|
| 启动配置 | ~20ms | ~5ms | 同 | 同 | 同 |
| 30 安全用户扫描 | ~600ms | ~50ms | 同 | 同 | 同 |
| 5 强平（串行）| ~400ms | ~400ms | ~200ms | ~125ms (持锁 80→25) | — |
| 5 强平（并发）| — | — | — | — | **~30ms (max + sem)** |
| **总** | **~1020ms** | **~455ms** | **~255ms** | **~180ms** | **~85ms** |

**最终：100 用户 sweep < 100ms，强平本身持锁 25ms**。
- 支持 1s tick 占 ~10% CPU 时间
- BUY/SELL 阻塞窗口从 80ms → 25ms，正常用户基本无感

## 风险和注意事项

1. **阶段 1 read-only 看到的 user state 是 snapshot**：阶段 2 lock 后必须重算
   margin，不能直接相信阶段 1 的越线结论。代价：阶段 2 多一次
   `compute_users_holdings_value(user_ids=[uid])`，但只对 ~5 个越线用户，可忽略。
2. **缩短 tick interval 时**：`max_instances=1` 已防重叠；建议加 tick 内超时
   保护（>800ms 主动 abort + 告警），防止某次 sweep 卡住堆积下一个 tick。
3. **AB/BA 死锁仍然存在**：`liquidate_user` 锁顺序是 `user → outcomes`，
   BUY/SELL 是 `market → outcomes → user`。当前 sweep 有 deadlock catch + 跳过
   逻辑保留即可。**如果将来做实时检查（per-market debounce）**，建议把
   liquidate 路径锁顺序改成 `market → outcomes → user` 跟 BUY/SELL 一致——
   这是单独的高敏感改动，需独立设计 + spec。
4. **`compute_users_holdings_value` 批量调用的 fee_rate**：已统一为
   "立即清算口径"（`SELL_FEE_RATE`），sweep / API / liquidate 同源，无分裂。
5. **测试**：性能 PR 必须有 before/after benchmark。建议加一个
   `tests/perf/test_liquidation_sweep_perf.py`，用 100 用户合成数据测一次
   sweep 耗时，前后对比。

## 相关延伸方向（独立 spec）

按优先级排：

1. **Partial Liquidation**：现在 margin < 0.2 全清；改成循环平到 margin ≥ 0.3
   就停。避免一次性大卖单把价格打穿，避免级联爆仓。**最高 ROI 的下一步**。
2. **成交后实时检查 + per-market debounce**：详见对话记录。要在此 perf 优化
   之上做，且需把 liquidate 锁顺序对齐 BUY/SELL。
3. **Tiered threshold by debt**：按 debt 大小分档阈值（< 1k → 0.15 / 1k-10k
   → 0.2 / > 10k → 0.3）。大仓位天然滑点深需要更厚缓冲。
4. **Bad debt reserve**：强平手续费的 X% 留作坏账储备，将来负权益核销。

明确不要做：mark price / EWMA 平滑、Oracle price aggregation、ADL。LMSR 没
订单簿薄盘操纵入口，这些在订单簿市场是必需品，在 LMSR 是过度工程。

## 实施顺序建议

明天开 `perf/liquidation-sweep-batched` 分支后，按这个顺序做，每步都要有
独立 commit + before/after benchmark：

1. **Step 1** (site_config 批量) —— 5 分钟，先暖手
2. **Step 2** (两阶段化批量 holdings_value) —— 核心，**最大单次收益**
3. **Step 3** (按 market group positions) —— 顺手做
4. **Step 5** (decrease_debt 接受已锁 user) —— 比 Step 4 简单先做
5. **Step 4** (read 合 1 个 JOIN) —— 最复杂，最后做
6. **Step 6** (跨用户并发) —— 上线前 benchmark 决定是否要

每步独立 PR/commit，可单独回滚。

性能基准测试位置：`backend/tests/perf/test_liquidation_sweep_perf.py`，
合成 100 用户 × 平均 3 positions × 5 个市场，跑 5 次取中位数。

## 参考

- 调研简报源：本次对话中 subagent 调研主流交易所（Binance/OKX/dYdX）的强平机制
- spec：`docs/superpowers/specs/2026-05-18-forced-liquidation-design.md`
- 实施 plan：`docs/superpowers/plans/2026-05-18-forced-liquidation.md`
