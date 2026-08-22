# 2026-08-22 全站审计：中低优先级待办

审计范围：交易热路径 / 资金风控一致性 / 实时与图表链路 / 前端状态层。
高优先级 4 项已在分支 `fix/2026-08-22-core-audit-3` 修复（populate_existing、
graceful shutdown、submit 前释放连接、SSE 无限重连 + max_cost 保护 + 精确 pay）。
本文记录其余发现，状态列随修复更新。

状态：`todo` / `done(<commit>)` / `wontfix(原因)` / `verify(需先核实)`

---

## 中优先级

### 一致性

| # | 问题 | 位置 | 修法 | 状态 |
|---|---|---|---|---|
| M1 | `loan_sweep` 每 60s 给每个债务人写一条 `interest_accrual` 审计事件；debt≥10 时每 tick 非零 → 200 债务人/天 ≈ 29 万行，`audit_event` 膨胀并拖慢 replay | `services/loan_sweep.py:46-72` | 定时结息只改 `debt`/`debt_last_accrued_at`，事件按「累计结息满阈值或跨小时」折叠写；借/还/强平路径保持逐笔精确（`interest_factor` 复利可合成，不丢精度） | done(2026-08-23, perf(loan)) |
| M2 | 大赦（amnesty）先 `accrue_interest(now1)` 再 `decrease_debt_locked` 内部用 `now2` 二次结息，毫秒差在大额债务下出 6dp 非零增量 → 留 ~1e-5「灰尘债」、`debt_last_accrued_at` 不清空、ledger `debt_delta` 与快照不等，replay mismatch | `services/admin_user_service.py:328-332` | 同一个 `now` 贯穿；或大赦直接 `debt=0; debt_last_accrued_at=None` 并按差值写 `debt_delta` | done(2026-08-23, fix(admin)) |
| M3 | 强平 stage B 逐市场卖出，不看累计 `repaid` 是否已清债，emergency 模式把后续市场也全卖（过度清算）；legacy 路径同样先卖光再还债 | `services/liquidation_service.py:439-452`、`:166-284` | `LiquidateMarketCmd` 返回 `debt_after`，为 0 即 break；partial 模式据此早停 | done(2026-08-23, fix(liquidation)) |
| M4 | snapshot `history_tail` 与排队中的 tick 帧窗口重叠（subscribe 取 anchor 后才读 ring），前端 `applyTrade` 无 trade_id 去重 → forming 桶 volume/n_trades 翻倍直到下次 gap | `api/v1/stream.py:176-190`、`CandleChart.vue:219-241` | snapshot 附带 tail 覆盖到的最大 `trade_id`，前端对 tick 帧内 `id <= that` 的成交跳过 | done(2026-08-23, fix(chart)) |
| M5 | writer 自愈 `reload_state` 从 DB 镜像重建 ring，但 `CANDLE_FLUSHER._pending` 里 ≤5s 未落库行不会回到 ring → ring 与 DB 永久分叉，且 `/history/` 防线 2 读 ring 被 nginx 30d immutable 固化 | `services/market_writer.py:183` | `reload_state` 先 `await CANDLE_FLUSHER.flush_once()` 再 `_load_one` | done(2026-08-23, fix(writer)) |
| M6 | sqladmin 可直接改 `User.cash` / `Outcome.total_shares` / `Market.status,liquidity_b,closes_at`，绕过 writer 内存态（下一笔成交静默覆盖）与审计事件 | `core/admin.py:72-86`（红线文件） | 这几张表 `can_edit = False` 仅浏览；参数变更走 API 并 `WRITER.reload_state` | todo（需用户授权动红线文件） |
| M7 | legacy buy/sell 锁序 market→outcomes→user，legacy 强平 user→positions→outcomes，互逆可死锁；`market_locks.py` 文档写的 market→user→outcomes 与代码不符 | `api/v1/market.py:573-582,749-758`、`services/market_locks.py:3-8` | writer 开启时休眠。至少改文档与代码一致；保留 legacy 则把 `_lock_user` 提到 `_lock_outcomes_for_market` 之前 | done(2026-08-23, fix(market) 锁序) |
| M8 | 前端 snapshot 重锚定只更新 `pricesByOutcome`，不回写 `marketStore.currentMarket` / `userStore.priceContext` → 重连后到下一笔成交前同页两套价格（报价用新价，OutcomeCard/预估滑点/maxShares 用旧价） | `useMarketRealtime.ts:450-454`、`TradingView.vue:174-205` | watch `snapshotToken`，用 `pricesByOutcome` 反向 `patchAllPricesFromTrade` + `patchMarketPrices` | done(2026-08-23, fix(front) 重连) |
| M9 | seq 回退（服务端重启归零）不触发 gap：`handleSnapshot` 只在 `seq > lastSeq` 时报 gap | `useMarketRealtime.ts:457-460` | `evt.seq < lastSeq` 也视为 gap | done(2026-08-23, fix(front) 重连) |
| M10 | `gapToken` 触发的 `fetchSummary` 与 `applyTradeFill` 竞态：陈旧 summary 响应无条件覆盖本地 apply 后的 cash；且 gap 只刷 summary 不刷 holdings（断线期间被强平则 holdings 陈旧） | `TradingView.vue:216-221`、`stores/user.ts:98` | `fetchSummary` 加请求序号丢弃过期响应；gap 同时 `fetchHoldings` | done(2026-08-23, fix(front) 重连) |
| M11 | buy/sell 10s 超时无幂等键：writer 排队 >10s 时前端报失败、服务端已成交，用户重试双倍加仓 | `api/index.ts:8` | buy/sell 单独更长 timeout（与 writer 10s 对齐 +余量）；长期：`client_order_id` 幂等 | done(2026-08-23, fix(front) 重连) |
| M12 | 断线时页面只显示「未连接」，本地报价继续用冻结价，下单无警示 | `TradingView.vue:227-231` | `!isConnected` 时 TradePanel 显示「行情可能过期」并默认禁用下单（max_cost 保护已加，此项降为 UX） | done(2026-08-23, fix(front) 重连) |

### 性能

| # | 问题 | 位置 | 修法 | 状态 |
|---|---|---|---|---|
| P1 | `_get_prices_24h_ago` 用 `row_number() OVER (PARTITION BY outcome_id ORDER BY timestamp DESC)` 对 cutoff 前全量成交排序；`/list`（含 Docker healthcheck 每 30s）与详情页每次都跑；`movers` 同构 | `api/v1/market.py:124-156`、`:1451-1472` | `DISTINCT ON (outcome_id) … ORDER BY outcome_id, timestamp DESC` 走 `ix_transaction_outcome_timestamp` 尾部（SQLite 测试环境需兼容写法：关联子查询 `LIMIT 1`） | done(2026-08-23, perf(market) 24h) |
| P2 | 排行榜匿名可访问、每次全量重算 MTM（全 User + 全 Position + 全 Outcome + 每仓位 LMSR），无缓存 | `api/v1/market.py:1254-1265` | 进程内 5–10s TTL 缓存（同 `/quote` 的做法） | done(2026-08-23, perf(market) 排行榜) |
| P3 | writer 开启时 `/quote` 仍查 3 次 DB，而 `WRITER.get_state(mid)` 零 IO 已有权威 `q/b/status/closes_at` | `api/v1/market.py:1110-1124` | `WRITER.enabled` 时直接用内存 state 算，DB 只留 fallback | done(2026-08-23, perf(market) /quote) |
| P4 | 结算循环每个持仓/赢家各调一次 `record_trade` → 每次 `session.flush()`，大市场 O(N²) 且全程持锁 | `services/writer_ops.py:489-530`、`api/v1/market.py:1003-1042`、`services/audit_service.py:124` | `record_trade(flush=False)`，循环末统一 flush | done(2026-08-23, perf(settle)) |
| P5 | 前端安静市场每笔成交触发全量 `loadFull()`（≤50 次 `/history/` GET + 全量 setData + MA 重算）：`bucketStart > currentCandle.t + step` 即整页重载，1Hz ticker 不推进空桶 | `CandleChart.vue:261-267` | 本地按 `fillCandles` 语义合成中间空桶（prev close 平推），只有跨段才重载 | done(2026-08-23, perf(chart)) |
| P6 | `loadFull` 用的 `historyTail` 是连接时 snapshot 快照，在同一段内停留后再 reload（切 tab / 切 interval / P5）必然过期 → 已画的 K 线回退成横线 | `useCandleHistory.ts:279-280`、`useMarketRealtime.ts:461` | tail 记录 snapshot 时刻，距今 ≥1 step 时改走 `chartApi.getCandles` 补尾巴 | done(2026-08-23, perf(chart)) |
| P7 | SSE per-IP 限流取 XFF 首段，nginx 用 `$proxy_add_x_forwarded_for`（追加），客户端自带 XFF 即可伪造 → 500 连接上限可被单人打满 | `api/v1/stream.py:125-132`、`deploy/nginx.conf:134` | 后端取 `X-Real-IP`（nginx 由 `$remote_addr`/ESA 头设置）或 XFF 末段 | verify（线上 nginx 是独立副本，需核对是否已用 `ali-real-client-ip` 覆盖） |

---

## 低优先级

| # | 问题 | 位置 | 状态 |
|---|---|---|---|
| L1 | sweep 阶段 1 预筛只排 HALT，未按 `market_is_open` 排过期 TRADING → 持过期市场仓位的用户每 tick 进阶段 2 抢 user 行锁再被 skip | `services/liquidation_sweep.py:245-254` | todo |
| L2 | writer 路径 sweep 与 `liquidate_user_split` 阶段 A 重复做一遍 lock_user + LCV 判定 | `liquidation_sweep.py:94-109`、`liquidation_service.py:396-426` | todo |
| L3 | stage B 非 HTTP 异常（含死锁）穿出时已成交的强平没有 `LiquidationEvent`/`liquidation` 汇总事件 | `liquidation_service.py:442-452` | todo |
| L4 | 借款额度用结息前 debt（超额 = k×未结利息，日利率 1% 可忽略） | `api/v1/loan.py:294-296` | todo |
| L5 | 管理员调账金额未 6dp 量化，>6dp 时 DB HALF_UP 截断与 audit 原始字符串差半个 LSB | `admin_user_service.py:58-61,255-264,335-336`；`admin_users.py:79/218` | todo |
| L6 | payout=0 的赢家仓位被删但无 `settle_win` 事件与 Transaction | `api/v1/market.py:975-978`、writer 对应段 | todo |
| L7 | writer 路径强平跳过不在 WRITER 的 TRADING 市场（`get_state is None → continue`），该仓位计入 LCV 却永远不卖 | `liquidation_service.py:440-441` | verify |
| L8 | `/quote` 不检查 `closes_at`、份额不量化（`float(req.shares)`），与成交校验不同源 | `api/v1/market.py:1118-1131` | done(2026-08-23, perf(market) /quote) |
| L9 | 买入/卖出成交额都 HALF_UP；方向上买入应 ROUND_UP、卖出 ROUND_DOWN（经济影响 ~4e-4/天可忽略） | `lmsr.py:13`、`writer_ops.py:78/229` | wontfix（影响可忽略，改舍入方向会让 golden 测试/前端对拍全改） |
| L10 | `_require_trading_state` 对 naive `closes_at` 抛 TypeError（仅 SQLite 测试环境） | `writer_ops.py:41` | todo |
| L11 | 两市场并发结算时用户行锁按持仓顺序而非 user_id 排序，可互相死锁（管理员几乎不会并发结算） | `writer_ops.py:489-508` | done(2026-08-23, perf(settle)) |
| L12 | `get_market_detail` 的 `last_trade_at` 倒扫 `ix_transaction_timestamp`，冷市场扫过所有更新的成交 | `api/v1/market.py:442-450` | todo |
| L13 | `_build_prices_from_shares`/detail/movers 逐 outcome 调 `get_current_price`，O(N²) exp（N≤10） | `api/v1/market.py:173,414,1484` | todo |
| L14 | `/history/` 防线 3 的 flush 高水位是全局 min：任一市场有 pending 时所有不在 writer 的市场本小时封存段 404（间歇，nginx 不缓存 404） | `history.py:106-111`、`candle_flusher.py:51-60` | todo |
| L15 | 进程内内存只增不减：`BROKER._seq`、`TICK._pending`、`WRITER._states/_queues/_tasks` 结算后不释放（每历史市场常驻 consumer task + 4 档 ring） | `realtime.py:62`、`tick_broadcaster.py:61`、`market_writer.py:117-119` | todo |
| L16 | writer consumer task 异常静默死亡无日志（无 `add_done_callback`），该市场所有请求 10s 后 503 | `market_writer.py:204-206` | todo |
| L17 | SSE 断连时 `get_task` 未 cancel → "Task was destroyed but it is pending" 噪声 | `api/v1/stream.py:211,241-246` | todo |
| L18 | `chart.py:109-260` 约 150 行逐笔重放死代码，numpy 仅为它引入 | `api/v1/chart.py` | todo |
| L19 | K 线桶时间用 `tx.timestamp`，tick 帧 `timestamp` 用 `datetime.now()`，跨桶边界毫秒级不一致 | `writer_ops.py:150,177` | todo |
| L20 | 前端 `applyTradeFill` 缺 `outcomeLabel` 时静默不 push holdings 行 → `maxShares=0` 无法平仓直到刷新 | `stores/user.ts:183-190` | done(2026-08-23, fix(front) 重连) |
| L21 | 前端 `loadFull` 无在途守卫，切 outcome 时慢的旧响应覆盖新图 | `CandleChart.vue:121-144`、`PriceChart.vue:149-203` | done(2026-08-23, perf(chart)) |
| L22 | `priceContext` 每帧 `new Map` 换引用 + `ctx.prices = [...]` 双重触发 holdings 重算 | `stores/user.ts:79-84` | todo |
| L23 | `fetchMarketDetail`/`fetchMarketTrades` 无 AbortController，快速切市场时旧响应覆盖 `currentMarket` | `stores/market.ts:37-61` | todo |
| L24 | 本地预览 `sell_fee_rate`/`margin_status` 取自页面加载时 summary，连续买入压进 danger 后按钮仍可点（服务端会拒） | `TradingView.vue:289`、`TradePanel.vue:185` | todo |
| L26 | `test_liquidation_sweep_perf` 用墙钟中位数 <300ms 断言，机器 load 300+ 时随机红（单跑也会）；改为相对基准（如对比同一进程内的无-sweep 基线）或标 `@pytest.mark.perf` 默认跳过 | `tests/test_liquidation_sweep_perf.py` | todo |
| L25 | 多标签页 access token 不同步（无 `storage` 监听），多一次 401→refresh 往返 | `stores/auth.ts:228-229` | wontfix（refresh token 不轮换，无正确性影响） |

---

## 已核对无问题（供后续审计跳过）

- `lmsr.py` cost/price 均 log-sum-exp，数值稳定；前端 `utils/lmsr.ts` 有 golden 对拍（1e-12 / 8dp 1e-6）
- writer/legacy 路径余额、持仓、outcome 镜像、Transaction、AuditEvent 同事务；强平回款同事务还债
- `check_buy/sell_slippage` 单一实现两路径共用；fee 公式 quote/sell 同源
- LCV 四处调用（summary / loan / sweep / admin wealth）同源 `wealth.compute_users_holdings_value`；legacy `liquidate_user` 内联复制了同公式（改 LCV 时两处同改）
- 强平定价与用户 sell 同用 `calculate_lmsr_with_prices` cost diff，差异仅 fee=0（有意）
- 审计事件：所有 cash/debt/amount/total_shares 写点均同事务落事件，replay 规则逐字段对应；唯一缺口是 M6 sqladmin
- SSE：序列化一次、有界队列 32 + 踢出、anchor 与 subscribe 原子、publish 无 await 点不乱序、history LRU ≤10MB、flusher 失败回炉不 double-count
- 单 uvicorn worker（`Dockerfile`），进程内状态正确
- 前端：`Number()` 用法无精度丢失、无 deep watch、列表有 key、401 刷新单飞锁正确
