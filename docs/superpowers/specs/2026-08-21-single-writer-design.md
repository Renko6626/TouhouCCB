# 单写者内存状态机 + 定频广播 —— 性能重构设计

- 日期：2026-08-21
- 分支：`perf/2026-08-21-single-writer`
- 状态：设计已确认（2026-08-21 二审修补 7 处：广播缺口 ×2、数值不动点、阶段矛盾、封存竞态、锁论证、精度口径），待出实施计划

---

## 1. 问题

`loadtest/k6_trade_20260513T043645Z.json`（nginx 限速白名单已开，打的是后端真实极限）：

| 端点 | p50 | p95 | p99 | 错误 |
|---|---|---|---|---|
| buy | 36.3 s | 60.0 s（超时） | 60.0 s | 144 × 500，215 × 连接失败 |
| sell | 36.2 s | 60.0 s | 60.0 s | 71 × 500 |
| quote | 27.4 s | 31.3 s | 32.8 s | 221 × 500 |

峰值吞吐 **17 req/s**。另一次跑（`k6_trade_20260513T093051Z.json`）buy p50 = 52.5 s，峰值 7 req/s。

三条结论：

1. nginx 那条 `api_trade_user` 10 r/s 限速不是在保护体验，**它是唯一挡着后端不塌的东西**。
2. `quote` 是无锁纯读却被拖到 27 秒 —— 不是它自己慢，是 30 个 DB 连接被 `SELECT FOR UPDATE` 全部占死，读请求拿不到连接。**读路径是被写路径的锁竞争勒死的。**
3. 根因是 `backend/app/api/v1/market.py` 的买卖路径：每笔成交在一个 DB 事务里按 `market → 全部 outcome → user → position` 顺序拿四把行锁（`services/market_locks.py`）。热点市场所有人挤同一个 `market` 行 —— 典型的抢锁雪崩，延迟随并发非线性爆炸。

## 2. 目标

把"热点行竞争"这个非线性崩溃模式整个删掉，换成线性可预测的行为：过载表现为**队列满即拒绝**，而不是所有人一起变慢。瓶颈从锁竞争迁移到广播带宽 —— 后者加带宽就能解决。

### 验收指标

| 指标 | 现状 | 目标 |
|---|---|---|
| buy p50 | 36.3 s | < 20 ms |
| buy p99 | 60 s（超时） | < 200 ms |
| 峰值吞吐 | 17 rps | > 300 rps |
| quote p99（200 SSE + 50 trader 并发下） | 32.8 s | < 50 ms |
| 每笔成交的 SSE 序列化次数 | = 订阅者数 | 1 |

最后一行是"广播只序列化一次"这条硬约束的可测量形式。

### 三条 FastAPI 硬约束

- **单进程**：多 worker 会分裂出多个市场状态机。`backend/Dockerfile` 已经是 `--workers 1`（原因注释在文件末尾），本设计把这条从"权宜之计"升级为"架构前提"。
- **事件循环内零阻塞**：任何同步 IO / CPU 密集循环都会让全站排队 —— 现状 quote p99 = 32.8 s 就是这条被违反的后果。
- **广播只序列化一次**：见验收指标最后一行。

---

## 3. 已确认的四个架构决策

| # | 决策 | 排除的选项 |
|---|---|---|
| D1 | 内存**只拥有 q 向量**；`cash` / `position` / `transaction` 权威留在 DB | 内存持有交易资金流；全部状态入内存、DB 降级为日志 |
| D2 | 一笔 buy **等 DB commit 之后才 ack**，零丢失 | 内存 ack + group commit；只做读路径不碰落盘 |
| D3 | 广播帧 = **价格向量 + 该帧内成交数组**，一帧序列化一次 | 长期双通道（定频 tick + 永久保留逐笔 trade）；帧内只有价格向量 |
| D4 | 历史包由**后端 `/history/` 路径吐 + `immutable` + nginx `proxy_cache`** | 预生成磁盘文件由 nginx 直接托管；只给现有 chart 端点加 ETag |

D2 的直接推论：**"批量落盘"不做**。串行 writer 每笔一个 DB 事务往返的保守上限是 300–1000 tps，相对现状 17 rps 已是 20–60 倍，足够把瓶颈推到广播侧。group commit 留作未来选项，本次不实现。

---

## 4. 第 1 节：单写者内存状态机

### 4.1 状态

```python
@dataclass
class MarketState:
    market_id: int
    b: float
    outcome_ids: list[int]      # 升序 —— 广播价格向量的索引契约
    q: list[float]              # 唯一真相；DB 的 outcome.total_shares 降为镜像
    prices: list[float]         # 由 q 导出并缓存，免去每次重算 exp
    status: MarketStatus
    seq: int                    # 帧序号
    rings: dict[int, HistoryRing]   # outcome_id → 环形缓冲（每 outcome 一份，见 § 7）
```

启动（`lifespan`）时从 DB 一次读入全部 `TRADING` / `HALT` 市场；`SETTLED` 不载入（不可交易）。

### 4.2 writer 粒度：per-market

每个市场一条 `asyncio.Queue` + 一个常驻 consumer task。仍是单线程（同一 event loop），对每个市场的 `q` 而言是严格单写者，但热点市场的排队不拖累其他市场。

**不用全局单 writer** —— 那等于给自己造一个新的全局瓶颈，与"删掉非线性崩溃模式"的目标相悖。

### 4.3 一笔 buy 的生命周期

```
handler   构造 BuyCmd(user_id, outcome_id, shares, slippage, fut)
          queue.put_nowait()  ← 队列满则立即 429（背压：拒绝，不是排队变慢）

writer    1. 内存定价 ΔC = C(q + Δe_i) − C(q)，算新价格向量     微秒，零 IO
          2. 校验滑点 / 市场状态 / 交易截止                      纯内存，失败直接 reject
          3. DB 事务（唯一阻塞点，1–3 ms）：
                SELECT user FOR UPDATE → 检查 cash → 扣款
                UPSERT position
                INSERT transaction
                UPDATE outcome.total_shares          （镜像）
                COMMIT
          4. commit 成功 → 才把新 q 写回 state，推进 seq，投喂广播缓冲
             commit 失败 → 内存 q 一个字节都没动，fut 抛异常
          5. fut.set_result(...) → handler 返回
```

**handler 侧两道背压，缺一不可**：

- 入队时：`put_nowait` 队列满 → 立即 429。管的是"命令太多"。
- 等结果时：`await asyncio.wait_for(fut, timeout=10)` 超时 → 503。管的是"writer 停摆"（DB 卡死、事务挂起）。没有这道，DB 一卡所有请求堆在 futures 上直到 60 s 网关超时，429 背压完全失效。超时后该命令可能仍在 DB 里执行，响应措辞必须是"结果未知，请刷新确认"，不是"失败"。

### 4.4 关键正确性约束

**内存 q 的变更必须发生在 DB commit 之后。** 第 1 步算的是影子值，第 4 步才提交。这样 DB 失败不可能让内存和 DB 分裂 —— 这是本方案能安全上线的支点。

**6 位量化是内存与镜像共同的不动点。** 现状每笔交易从 DB 重读 6dp 的 `total_shares`，量化天然对齐；改成内存常驻后，若内存走 float 增量累加、镜像走 Decimal 6dp 累加，两条路径会逐笔产生 ulp 级漂移——重启从镜像读回时价格微跳，"无损恢复"不再严格成立。所以第 4 步的规则是：**commit 后不做 `q[i] += Δ`，而是把镜像的量化结果回写内存**——`q[i] = float(quantize_cost(new_total))`（`lmsr.py` 内核本来就是 float + 边界量化，这不引入新精度语义）。由此内存 q 与 `outcome.total_shares` 在任意时刻逐字节一致，重启恢复是精确的。

**崩溃恢复**：内存 q 从 `outcome.total_shares` 直接读回（镜像在每笔 commit 里同步更新，且按上条与内存值恒等）。未 commit 的命令对应的客户端收到错误，零静默丢失。

**writer task 异常策略**：若在 commit 成功之后、内存 apply 之前抛出非预期异常，内存已陈旧。writer 的 consumer loop 外层捕获非预期异常时：记 critical 日志 → 从镜像重读该市场 q 自愈 → 继续消费；重读失败则将该市场标记不可用（后续命令直接 503），绝不带着可能陈旧的 q 继续定价。

### 4.5 锁的去向

`services/market_locks.py` 的四把 `SELECT FOR UPDATE`：

- `lock_market` / `lock_outcomes_for_market` → **新路径不再使用**（物理删除在阶段 5，见 § 8）。内存拥有 q，DB 那列只是镜像，不需要串行化保护。
- `lock_user` / position 行锁 → **保留，且仍会竞争——这是正确性依赖，不是摆设**。writer 是 per-market 的：同一用户在两个市场可以同时各有一笔在飞（quant bot 正是这种用户）；另有 5 条 cash 写路径在 writer 之外（§ 4.7）。跨市场、跨路径的 cash 串行化正是靠这把 user 行锁完成的。**不要因为"单写者了"就顺手删它。**
- 死锁分析：所有事务都按 `user → position` 单向拿锁（market/outcome 锁退出后不再有跨市场的多行锁链），无环，无死锁。

文件顶部那段四级锁序约定注释在阶段 5 随锁一起删，替换为上面这条 `user → position` 两级约定。

### 4.6 已决：liquidation 拆成 per-market 独立提交

`services/liquidation_service.py:222` 直接改 `total_shares`，是 q 的第二个写者，必须走 writer。它跨市场（一个用户在多个市场有持仓），现在是一个 DB 事务全做完、中途失败全回滚。

**决定：拆成"每个市场一笔独立提交"。** 强平本来就是尽力而为的止损；"卖了 3 个市场第 4 个失败就全退回去"并不比"卖了 3 个"更安全，反而让用户在爆仓边缘多等一个 sweep 周期。

`LiquidationEvent` 快照改为在全部子命令返回后统一写一条汇总记录。

被排除的替代方案：按 `market_id` 升序依次占用多个 writer 的协调器 —— 保留全或无原子性，但把跨 market 的等待链又请了回来。

### 4.7 其他必须改走 writer 的写者

- `market.py` 的 `resolve_market`（结算，删 position、发 payout）
- `market.py` 的 `close_market` / `resume_market`（改 `status`）
- `services/liquidation_sweep.py`（经 4.6 的 liquidation_service）

`user.cash` 的另外 5 条写路径（`loan_service`、`redemption`、`danmuku`、`adjust-cash`、`batch-adjust-cash`）**不动** —— 它们不碰 q，继续走各自的 DB 事务。这是 D1 的直接好处。

---

## 5. 第 2 节：广播管线

### 5.1 定频帧

一个全局 broadcaster task，固定 **8 Hz（125 ms）**。每次 tick 扫 dirty 市场（`seq` 变过的），对每个：

```python
frame = {
    "type": "tick",
    "market_id": mid,
    "seq": s,
    "t": ts,
    "status": "TRADING",      # 市场状态，恒有；变更（halt/settle/resume）本身置 dirty
    "prices": [...],          # 全 outcome 当前价，按 outcome_ids 顺序，8 位小数（见下）
    "trades": [{...}, ...],   # 本帧窗口内的成交，逐笔不丢
    "settlement": {...},      # 仅 SETTLED 帧携带：winning_outcome_id, settled_at
}
blob = json.dumps(frame, ensure_ascii=False).encode()   # 整个进程只调一次
for sub in subs:
    sub.q.put_nowait(blob)                              # 投递 bytes，不是对象
```

`api/v1/stream.py` 的 generator 改成直接 `yield blob`。**这是当前"每订阅者各序列化一次"缺陷的根治**：500 订阅者从 500 次 `json.dumps` 变成 1 次。

**`market_status` 事件并入 tick 帧。** 现状 halt / settle / resume 各发一个独立的 `market_status` 事件（`market.py` 三处 publish）；新管线里**状态变更 = 置 dirty**，下一帧带着新 `status`（settle 帧附 `settlement`）出去，占用正常帧 seq，gap 检测天然覆盖。终态 tick 是唯一的市场数据帧；`ping` 保活帧不变。迁移期老 `market_status` 事件与老 `trade` 事件走同一个 `legacy_trade_events` 开关双发（§ 5.4）。

**帧内 `prices` 是 8 位小数量化值**（`quantize_price` 的输出）——这就是服务端的权威价格精度，不是有损压缩。客户端一切计算的输入精度以此为准（§ 6.2）。

无成交且状态无变更的市场不发帧，靠现有 25 s ping 保活。帧率恒定，带宽跟实际活跃度走。

### 5.2 队列语义

`MarketEventBroker.QUEUE_MAXSIZE` 从 2000 降到 **32**。定频之后队列深度**就是"落后几帧"**，32 帧 = 4 秒落后容忍。慢消费者的发现速度从"攒够 2000 条"变成 4 秒。

现有的慢消费者踢出 + `kicked` Event 机制原样保留。

### 5.3 seq 与 gap 检测

`seq` 语义从"每笔成交 +1"变成"**每帧 +1**"。`subscribe()` 的 anchor 原子性、snapshot 锚定、前端 `useMarketRealtime.ts` 的 gap 检测、`api/stream.ts` 的重连逻辑**全部不变**。

### 5.4 quant bot 迁移

`quant/docs/sse-contract.md` 明确写了"主站改这些字段 = 破坏 bot"。

**双发是限期迁移手段，不是 D3 排除掉的"长期双通道"。** 终态只有 `tick` 一种帧；下面的开关在阶段 5 连同代码一起删除。迁移期**双发**：

- 新 `tick` 帧（前端和迁移后的 bot 吃这个）
- 老 `trade` + `market_status` 事件保留，由 `site_config.legacy_trade_events` 热开关统一控制

老事件仍是每订阅者序列化，但届时只剩 bot 订阅，量级完全不同。bot 改完后关掉开关，阶段 5 删代码。同步更新 `quant/docs/sse-contract.md`。

---

## 6. 第 3 节：客户端计算契约

### 6.1 闭式公式

设当前价向量 `p`，流动性 `b`。买入 outcome `i` 共 `Δ` 份：

```
ΔC   = b · log1p( p_i · expm1(Δ/b) )                成本
D    = 1 + p_i · expm1(Δ/b)
p'_i = p_i · exp(Δ/b) / D                           成交后价格
p'_j = p_j / D                        (j ≠ i)
```

卖出把 `Δ` 换成 `−Δ`，`ΔC` 为负，收入 = `−ΔC`。**只需要 `p_i` 和 `b`，不需要 q。**

持仓估值同样闭式：

```
MTM_j = amount_j × p_j
LCV_j = −b · log1p( p_j · expm1(−amount_j / b) ) × (1 − sell_fee_rate)
```

### 6.2 三个必须落实的数值细节

1. **用 `log1p` / `expm1`，不用 `log` / `exp`。** 小额交易时 `Δ/b` 极小，`exp(x) − 1` 会丢掉大部分有效位。6 位份额精度下 1 份对 `b=100` 就是 `Δ/b = 0.01`，朴素写法直接掉 2–3 位有效数字。
2. **`Δ/b > 700` 走渐近分支** `ΔC → Δ + b·ln(p_i)`，否则 `expm1` 溢出成 `Infinity`。正常交易碰不到，恶意输入能碰到。
3. **与服务端的偏差分两层，都是已知且接受的。** (a) 数学层：同为全精度输入时，客户端从 `p` 算与服务端从 `q` 算（`services/lmsr.py`）数学等价，浮点路径不同带来 ~1e-15 相对误差。(b) 线上层：客户端的实际输入是帧内 8 位量化的 `p`（§ 5.1），预览偏差由输入量化主导，量级 ~1e-7 相对误差——仍远小于服务端 6 位资金量化（绝对 1e-6）在典型成交额上的粒度。**两层都不是 bug，不要当 bug 报**；成交以 writer 返回为准（§ 6.3），预览偏差不会变成资金误差。

### 6.3 权威边界

- 客户端算的**永远是预览**；实际成交以 writer 返回为准。
- 执行机制沿用现有滑点保护（`max_cost` / `max_slippage_bps` / `accept_any_slippage`），不新建设施。
- `margin_status`、强平判定、排行榜排序**仍是服务端算的**。前端本地那个 margin 比例是显示用估算；真正触发强平的是 sweep，权威值随 summary / SSE 下来。

### 6.4 `/user/summary` 降级

现在每次调用跑两遍全仓 LMSR（`compute_users_holdings_value_mtm` + `compute_users_holdings_value`），且每次成交后必被调用。

**新契约只返回客户端算不出来的东西：**

```
cash, debt,
positions[{outcome_id, market_id, amount, cost_basis}],
margin_hard_threshold, margin_soft_threshold, sell_fee_rate,
rank_thresholds[{min_net_worth, title}],   ← 见下
margin_status（服务端权威）, liquidation_protected, last_liquidated_at,
equipped_title, all_titles
```

**删除**：`holdings_value`、`holdings_value_liquidation`、`net_worth`、`net_worth_liquidation`、`unrealized_pnl`、`unrealized_pnl_liquidation`、`total_cost_basis`、`rank`。

其中 `total_cost_basis` 由客户端对 `positions[].cost_basis` 求和得到。

`rank` 字段（`services/rank.py::rank_title` 按净值区间映射东方主题称号）需要特别处理：它依赖 `net_worth`，而 `net_worth` 现在是客户端算的，所以 `rank` 也必须在客户端算 —— 否则服务端算出来的 rank 会和客户端显示的净值对不上。**做法：基线里下发 `rank_thresholds` 区间表（一次，极小），客户端本地映射。** 排行榜接口 `/market/leaderboard` 的 rank 仍由服务端算并保持权威（§ 6.3）。

`/user/holdings` 同样瘦身：删 `current_price`、`market_value`、`unrealized_pnl`、`unrealized_pnl_liquidation`、`avg_price`（客户端由 `cost_basis / amount` 算）。

**调用时机**：登录 1 次 + 手动刷新 + gap reconcile。**成交后不再调用** —— buy 响应已返回 `new_cash`，前端本地 apply（`amount += shares`、`cost_basis += pay`）。这直接删掉 `TradingView.vue` 现在每次成交后的 4 个 REST 往返。

两条配套修正：

- **buy / sell 响应的 `new_cash` 改为 6 位全精度 Money**。现状是 `quantize(0.01)` + float（`market.py:665,829`），当年只是展示用；现在它成了客户端 cash 基线，2dp 舍入会每笔累积最多 0.005 的漂移。改法遵守既有 Schema Decimal 规则（Money 序列化）。
- **admin `adjust-cash` / `batch-adjust-cash` 改别人余额后，目标用户前端 cash 陈旧到下次 summary 调用为止（登录 / 手动刷新 / gap reconcile）。这是已知且接受的**：调账是罕见管理操作，不为它保留轮询。管理员调账后口头知会用户刷新即可。

`services/wealth.py` 的两个函数**保留** —— 强平 sweep、`/admin/wealth`、排行榜仍需要服务端权威口径。只是不再进 `/user/summary` 的请求路径。`docs/holdings-value-semantics.md` 需同步更新"谁在算 MTM / LCV"这一节。

### 6.5 quote 端点

`/market/quote` **保留但前端不再调用**（留给 bot 和对账，零维护成本）。`market.py` 的 `_QUOTE_CACHE` / `_QUOTE_CACHE_TTL` / `_quote_cache_key` / `_quote_cache_gc_if_full` 及其注释全部删除。

### 6.6 前端改动面

`utils/lmsr.ts`（新增，闭式公式单一实现）+ golden case 单测，与 § 6.2 第 3 条的两层偏差一一对应：

- **数学层**：喂全精度 `p`，与后端 `services/lmsr.py` 对拍，断言相对误差 < 1e-12；
- **线上层**：喂 8 位量化后的 `p`（模拟真实线上输入），断言相对误差 < 1e-6。

只写第一层的话，测试喂的输入和生产喂的输入不是同一个东西。

受影响：`pages/user/Portfolio.vue`、`components/user/MarginStatusCard.vue`、`components/market/TradePanel.vue`、`pages/market/TradingView.vue`、`components/layout/AppHeader.vue`、`stores/user.ts`、`stores/market.ts`、`types/`。

`stores/` 是 `CLAUDE.md` 的高敏感区，本节是整个重构改动面最大的一节。

### 6.7 轮询处置

| 位置 | 现状 | 处置 |
|---|---|---|
| `pages/market/TradingView.vue` sanity refresh | 60 s | **删**，tick 帧取代 |
| `pages/market/TradingView.vue` 成交后 | `loadMarketData()` + `loadUserData()` = 4 个 REST 往返 | **删**，本地 apply（§ 6.4） |
| `components/home/RecentTrades.vue` | 5 s | **保留** |
| `components/home/Movers.vue` | 30 s | **保留** |
| `components/home/RecentLiquidationsPanel.vue` | 60 s | **保留** |

**首页三处轮询保留的原因**：SSE 订阅是 per-market 的（`stream.py` `/market/{market_id}`），这三个组件是跨全站视图，tick 帧覆盖不了；给每个市场开一条 SSE 不现实（HTTP/1.1 同域连接上限 6 条），且 liquidation 目前没有任何 SSE 事件。阶段 1 之后这些读接口不再被锁竞争拖累，轮询很便宜。**全局 home 聚合频道（broadcaster 顺带聚合全站成交 + movers + 强平事件）留作未来增量，不进本次范围**（§ 10）。

`gapToken` 触发的 reconcile 保留（那是正确性机制，不是轮询）。

---

## 7. 第 4 节：历史是静态资源

### 7.1 内存多分辨率环形缓冲

每 outcome 一份：

| 档 | 窗口 | 根数 |
|---|---|---|
| 10s | 1 h | 360 |
| 1m | 24 h | 1440 |
| 15m | 7 d | 672 |
| 1h | 90 d | 2160 |

合计 4632 根/outcome。列式编码（`{t0, step, o:[…], h:[…], l:[…], c:[…], v:[…]}`，价格按 8 位定点转整数），单 outcome 约 200 KB 未压缩；nginx 已开 gzip，线上约 50 KB。

### 7.2 分段封存

只有过去的段才不可变：10s 档按 10 分钟切段、1m 按小时、15m 按天、1h 按 7 天。

```
GET /history/o/{outcome_id}/{interval}/{segment_epoch}.json
    → Cache-Control: public, max-age=31536000, immutable
```

URL 含段起点，内容永不变化，`immutable` 真实成立。不在 `/api/v1/` 下，所以 `main.py` 那个无条件打 `no-store` 的中间件（`_set_no_store_for_api`）碰不到它。

**封存竞态防线——错一个 200 会被 nginx 钉 30 天、浏览器钉 1 年**：

1. 未到封存边界的段：**404**。尾巴数据只走 SSE snapshot（§ 7.3），`/history/` 永不吐进行中的段。
2. 段在 ring 窗口内：**一律从 ring 供数**，不读 DB。ring 是 writer 实时写的，跨过封存边界即完整，无需等 flusher。
3. 段超出 ring 窗口、需从 DB 读：必须确认该段时间范围的 flush 已完成（flusher 高水位 ≥ 段末尾）才发 200，否则 404。防的是"边界刚过、flusher 5 s 批次未落库、恰好又不在 ring 里"的窗口把不完整段固化。

### 7.3 尾巴走 SSE snapshot

最后封存边界 → now 之间的数据（最多一个段长）由 **SSE snapshot 首包携带**。零额外请求，且与后续帧共享同一条 `seq`，现成的 gap 检测直接覆盖。

这是选 D4 而非"预生成磁盘文件"的关键理由 —— 磁盘托管拿不到这个，必须另开一个 tail 端点。

### 7.4 数据来源与红线

`OutcomeCandle` 物化表**仍是权威**（K 线永远是服务端聚合的，客户端不自行从流里聚合）。ring 窗口内的段直接从 ring 供数；更老的段第一次被请求时从 DB 读一次进内存 LRU（受 § 7.2 防线 3 约束），之后零 DB；叠 nginx `proxy_cache` 后回源次数 ≈ 段数，与在线人数无关。

### 7.5 candle 写入移出 hot path

现状：每笔成交在事务内 UPSERT `N 个 outcome × 4 档` 行（2 选项市场 8 行，多选 16+ 行）。

改为：writer 在内存 ring 里更新 OHLCV（微秒），持久化交给独立 flusher task 每 5 秒批量 UPSERT dirty 桶。

**hot path 的 DB 语句数从 `N×4 + 4` 降到 `4`。**

崩溃最多丢 5 秒的 K 线精度，用现成的 `main.py::_resync_recent_candles` 从 Transaction 表重放补齐 —— 那套逻辑已幂等，只需调整窗口参数。**重放必须确定性**（同一批 Transaction 重放出逐字节相同的 OHLCV）：崩溃前该段可能已从 ring 以 immutable 发出去过，修复后的 DB 内容若与之不同，就违反了 immutable 承诺。实施时给 resync 补一个确定性断言测试。

### 7.6 nginx 改动

```nginx
proxy_cache_path /var/cache/nginx/thccb levels=1:2 keys_zone=thccb_hist:10m
                 max_size=1g inactive=30d;

location /history/ {
    proxy_pass http://127.0.0.1:8004;
    proxy_cache thccb_hist;
    proxy_cache_valid 200 30d;
    proxy_cache_use_stale error timeout updating;
    add_header X-Cache-Status $upstream_cache_status;
}
```

前缀 `/history/` 比 `/` 长，nginx 最长前缀优先，不会被 SPA 的 `try_files` 吃掉。

`deploy/nginx.conf` 是 `CLAUDE.md` 红线文件，用户已在本次设计中明确授权修改。

---

## 8. 第 5 节：分阶段迁移与回滚

生产站在跑，**不做大爆炸切换**。六个阶段，每个独立可上线、可回滚。

### 阶段 0 · 修序列化（独立收益，可先行）

`sse_pack` 挪进 broker，投 `bytes` 不投对象；`stream.py` 直接 `yield blob`。不依赖任何后续阶段，风险极低。

### 阶段 1 · 单写者 + 内存 q（API 契约零变更）

- 新增 `backend/app/services/market_writer.py`：`MarketState` + per-market queue + writer task
- buy / sell / resolve / close / resume / liquidation 全改走 writer
- `market_locks.py` **一行不动**——旧路径还要靠它裸奔期兜底；物理删除在阶段 5
- candle 写入移出 hot path → flusher（§ 7.5）
- `site_config` 开关 `single_writer_enabled`，新旧路径并存一个版本周期。**开关翻转需重启进程生效**（关→开必须在无在途旧路径事务时重读 q 入内存，热翻转会漏掉翻转瞬间旧路径已 commit 未入内存的变更；重启从镜像读回天然正确，且回滚场景本来就伴随重启）
- **验收**：pytest 全绿 + 白名单重跑 k6，buy p50 从 36 s 掉到 20 ms 以内

### 阶段 2 · 定频广播帧（契约变更，双发兼容）

- broadcaster task + `tick` 帧（含 `status` 并入，§ 5.1）
- 老 `trade` + `market_status` 事件由 `site_config.legacy_trade_events` 控制保留
- 前端改吃 tick 帧
- **前端加 build 版本自刷机制**（SSE snapshot 携带后端启动时注入的前端 build hash，不匹配则提示/强制刷新）——这是阶段 3 的前置：阶段 3 删 summary 字段时，部署瞬间已打开的旧 tab 拿到砍过的响应会 NaN/炸，靠这个机制把旧 tab 赶去刷新。本阶段先上线机制本身
- `quant` bot 改造 + `quant/docs/sse-contract.md` 更新（可并行做）

### 阶段 3 · 客户端计算 + summary 降级（改动面最大）

- `utils/lmsr.ts` + golden case 单测先行
- `/user/summary` 与 `/user/holdings` 瘦身
- 前端本地算 MTM / LCV / 浮盈 / 成交预览
- 删 quote 调用、删成交后 4 个往返、删 TradingView 两处轮询（首页三处保留，§ 6.7）

### 阶段 4 · 历史包 + nginx

- ring buffer + `/history/` 端点 + snapshot 带 tail
- nginx `proxy_cache`
- 前端图表改成"加载一次 + 广播续写"

### 阶段 5 · 清理

关双发（`trade` + `market_status`）、删旧买卖路径、删 feature flag、删 `_QUOTE_CACHE`、删 `market_locks.py` 的 market / outcome 锁与四级锁序注释（换成 `user → position` 两级约定，§ 4.5）。

### 每阶段必跑

- 前端：`npm run type-check` + `npm run lint`（涉及构建/依赖时加 `npm run build`）
- 后端：`python -m py_compile $(find app -name '*.py')` + `python -c "import app.main"` + `pytest -x`
- UI 改动：浏览器实测主路径 + 边界态（空 / 加载 / 错 / 未登录 / 移动端）

---

## 9. 已知风险

| 风险 | 缓解 |
|---|---|
| 单进程是架构前提，进程挂掉 = 全站不可用 | 现状已是 `--workers 1`，风险量级不变；`stop_grace_period: 8s` 保留 |
| 阶段 3 触碰 `stores/` 等高敏感前端文件 | 独立阶段、独立验证；golden case 单测保证公式与后端一致 |
| liquidation 语义从"全或无"变为"逐市场提交" | § 4.6 已论证；`LiquidationEvent` 改为汇总记录，公示墙展示不变 |
| quant bot 契约破坏 | 双发 + `site_config` 热开关，bot 迁移完成后才关 |
| candle 落盘延迟 5 s，崩溃丢失 K 线精度 | `_resync_recent_candles` 从 Transaction 表重放补齐，已幂等 |
| 内存 q 与 DB 镜像分裂 | § 4.4：commit 之后才 apply 内存，且 apply 值取镜像量化结果（6dp 不动点）；writer 异常自愈从镜像重读；重启从 `outcome.total_shares` 读回 |
| DB 卡死时请求堆积、背压失效 | § 4.3：handler 等 fut 加 10 s 超时 → 503，与队列满 429 双道背压 |
| 不完整历史段被 immutable 固化 | § 7.2 三道防线：进行中段 404、ring 窗口内从 ring 供数、超窗段确认 flush 高水位后才 200 |
| 阶段 3 部署瞬间旧 tab 拿到砍过的 summary 响应 | 阶段 2 先上线 build 版本自刷机制（§ 8） |
| SQLAdmin 面板直接改 `Market.liquidity_b/status/closes_at` 或 `Outcome.total_shares`，writer 内存看不到（成交侧与估值侧用两个 b；手工修数会被下一笔绝对值 SET 覆盖） | **flag 开启后禁止在 admin 面板编辑市场/选项字段；确需修改必须改完立即重启后端**。更硬的防线（`MarketAdmin`/`OutcomeAdmin` 设 `can_edit=False`）动红线文件 `core/admin.py`，留待用户决定 |

## 10. 不在本次范围

- **group commit / 批量落盘**：D2 已排除，见 § 3。Phase 1 的 300–1000 tps 足够。
- **全局 home 聚合频道**：首页三处跨市场轮询本次保留（§ 6.7）；broadcaster 顺带聚合全站成交 + movers + 强平事件的 home 频道留作未来增量。
- **Redis pubsub / 多进程**：单进程是本设计的前提，不是待解决的债。
- **交易手续费**：现状 buy/sell fee 全 0 硬编码，属独立待决事项，本次不动。
- **依赖主版本升级、新框架 / 新 UI 库**：`CLAUDE.md` 栈约束。
