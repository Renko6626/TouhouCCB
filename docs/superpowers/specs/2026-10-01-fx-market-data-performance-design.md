# FX 行情性能：复用 LMSR 增量行情路线

## 目标与范围

用户已确认采用 LMSR 已验证的技术路线：成交后增量处理、多周期 K 线、内存 ring、批量落库、封存历史缓存、SSE 尾段与实时续写。目标是消除主页读取及后台发布反复加载一天成交对象的 CPU 开销。

沿用 FastAPI、SQLModel/SQLAlchemy async、PostgreSQL、SQLite 测试、Vue/TypeScript 和现有单写者。此次调整行情读模型及其派生数据，不改变 AMM 定价、资金账、空头债务、风控准入和现有锁序，也不增加 Redis、进程或部署拓扑。

## 诊断依据

- 线上一次主页式六请求并发测量，三个快照和三个走势图的首字节等待均约 11.55 秒；响应分别只有约 350 B 和 14 KB。
- 隔离 PostgreSQL，每 pair 18,000 条合成成交：快照中位耗时/应用 CPU 为 568/565 ms；FX 走势为 735/724 ms；LMSR 查询约 101 根已物化 K 线为 2.7/2.5 ms，后者不含写时聚合成本。
- 三个 pair 的真实 FX tick 中位应用 CPU 约 1.66 秒，其中发布快照约 1.63 秒。无人订阅时复用现有 publisher 的跳过逻辑，对照 tick 耗时约 32 ms。
- 数据读取、解码、完整 ORM 对象构建是主要热点；图表还有逐笔排序和分桶。上述局部剖析不是生产调用栈采样，也不是上线性能承诺。

## 数据流和模块

```text
FxTrade + 资金/审计事务提交
  → 轻量 notify_committed(pair_id)
  → FX 行情服务按 pair 游标增量读取已提交成交
  → 更新 ring / 待落库 K 线 / 待发布增量
  → 每 5 秒原子落库 K 线与恢复游标
  → 查询物化 K 线或封存段；SSE 发送首包尾段与实时增量
```

复用 `history_ring.py` 的桶宽、窗口、封存边界和合并思路；复用 `candle_flusher.py` 的批量、失败恢复和停机 flush 模式。FX 使用薄适配层及自己的存储、游标，LMSR 的现有接口与数据不迁移。实时发布继续使用现有 FX publisher，聚合可靠性与推送队列的丢弃策略分开。

### 派生存储

- `FxCandle`：主键 `(pair_id, interval, bucket_start)`；OHLC、金侧成交量、成交数及首末成交排序键。价格用 `Numeric(24,8)`，成交量用 `Numeric(30,6)`，覆盖现有 FX 价格范围及累计量；非负量、非负成交数、`high >= low` 和周期 CHECK。
- `FxMarketDataState`：每 pair 的 `last_trade_id`（已可靠落库的消费游标）与 `history_version`（历史版本标识）。游标不假设全站 ID 连续；历史版本首次生成、全量重建时换新，用于隔离缓存。
- 周期与 LMSR 一致：`10s / 1m / 15m / 1h`。现有 FX 图表继续使用 `1m / 15m / 1h`，不顺便添加 UI 周期。
- 原始 `FxTrade` 是重建来源；新增表是派生数据。迁移只建表/约束，不在迁移中扫描全部生产成交。回填由单独脚本负责。

### FX 口径

1. 按 `(created_at, id)` 决定桶内先后；OHLC 沿用现有 FX 的 `post_price` 口径，不直接采用 LMSR 首笔 `pre_price` 的规则。首末排序键保证跨批次合并也得到正确 O/C。
2. 金侧成交量：buy 取 `input_amount`，sell 取 `output_amount`，与现有快照一致。包括现货、开空、回补、强平、系统 target/noise/event 成交；充值、撤资和计息不伪造成交。
3. 聚合全程 Decimal。历史列式结构复用 LMSR，但 FX OHLC 用十进制字符串；不能直接复制 LMSR 的 `price × 1e8` JS 整数编码，因为 FX 价格范围可能超过安全整数。只在图表适配层转 number。
4. `volume_24h` 保持精确滑动 24 小时，不改为自然日或近似整桶求和。首阶段用数据库 `SUM(CASE ...)`；后续用分钟桶汇总加窗口两端原始成交的 SQL 补算，不构建全部成交对象。实时量也会随时间退出窗口，不能仅按 `pool_version` 缓存。

## 一致性与生命周期

- 各交易事务拥有者只在成功 commit 后发 pair 通知；事务内执行器不发通知、推送或更新 ring。同 key replay 不重复通知，拒单/回滚不产生派生行情。所有资金、风控和门闩规则沿用现状。
- 通知是加速信号，后台每 5 秒用索引检查各 pair 的新成交高水位，补偿通知遗漏、异常和合并。新增 `(pair_id, id)` 索引用于增量消费。同 pair 的所有生产写入口仍在 pair 行锁下创建成交，因此按 pair 已提交高水位推进；不得使用全站 `MAX(id)` 跳过未提交的其他 pair。
- 内存有应用游标，数据库有持久游标。批次 K 线与持久游标在同一事务落库；重复 flush、连接中断后提交结果不明，先读取持久游标判断，不盲目重复累加。失败保持或恢复待写批次。无订阅也必须维护 K 线，只有实时帧可以跳过/合并/丢弃。
- 启动先加载持久游标/就绪状态并从物化 K 线预热 ring，再 `catch_up` 补齐持久游标与已提交来源之间的全部差额（随后 `flush_once`），之后才允许经济生产者启动；不以固定一小时窗口代替可靠恢复。消费器遵守 OWNERSHIP，只读实例只预热、不写派生表，读取物化表和权威 pair，并由独立读实例的启动/数据库回退路径提供数据。
- 停机顺序：停止经济生产者/调度器 → 排空行情消费 → 最终 flush → 停 publisher → 释放 ownership。内存处理期间不新增全站经济锁，也不让网络发布延长 pair GATE。
- 重建单 pair 时在该 pair 的维护边界内锁定原始成交截止点，以派生表替换和游标更新的同一事务完成；随后切换 ring 与新历史版本。正常回填/重建不改原始成交及资金审计。
- 赛季重置同步清理派生表和内存状态；历史版本与 FX 命名空间防止 pair ID 重用后命中旧缓存。归档品种仍可查看历史。

## HTTP、历史与 SSE

- `/api/v1/fx/pairs/{id}/snapshot` 保留现有字段与类型；价格和交易状态以权威 pair 为准。资产/风控决策不得消费行情缓存。
- 原 `/chart` 维持当前响应结构，改为物化表查询加最新内存尾部；校验周期和区间后才查库，最多返回 20,000 个桶。历史未完成回填时明确返回可重试的未就绪状态，不退回重扫全部成交。旧客户端可继续使用 `/chart`。
- 新增 `/history/fx/{pair_id}/{history_version}/{interval}/{segment_epoch}.json`。沿用 LMSR 封存三道防线：进行中段不封存；ring 可直接供数；落库回退先检查消费/flush 高水位。未就绪/未封存用非 200 响应，不能缓存不完整结果。
- 历史段使用已有 `/history/` 浏览器 immutable + nginx 缓存和有界进程 LRU；key 含产品、pair、历史版本、周期、段起点。已发布版本下的封存段不能改变；重建必须换 URL 版本。
- SSE 继续用 `fx:{pair_id}` topic 和 `fx` 事件，保留现有报价/新闻字段。首包增加 `history_version`、`history_tail`、`history_tail_at`、`history_tail_through_trade_id`。增量帧增加公开成交列表 `{id, ts, post_price, gold_volume}`，由已提交成交生成，不泄漏账户、债务或未来干预参数。
- 沿用先 subscribe 取 anchor、再读取快照的顺序；尾段覆盖游标与后续 trade ID 去重。按真实成交时间更新桶，不用客户端接收时间。推送合并不丢成交量；帧容量不足时显式要求补尾段，不能仅保留最后一个价格并假装历史完整。
- 桶读取包括已落库数据与内存未落库部分，按桶合并而不是拼接重复计量。broker 断流/客户端 gap 时仅补遗漏尾段；历史版本改变才重新读取对应历史。

### 实施后契约细化（2026-10-01 最终验收固化）

以下为实施后需要明确的既有契约细化，不改变架构、路由、定价或资金规则：

- `FxMarketDataState.history_ready` 为持久化就绪标记：首次回填的中间页写 `False`、完成页写 `True`，增量页保持原值。`/chart` 与 `/history/fx/` 在未就绪时返回可重试的非 200（503），不退回重扫原始成交。
- 原 `/chart` 响应体保持旧结构（`bucket_start/interval/open/high/low/close/volume`）；单次响应的真实覆盖游标与历史版本经响应头 `X-FX-Through-Trade-ID`、`X-FX-History-Version` 返回（CORS 已暴露），供前端缓存历史回退与 SSE 尾段去重，避免把旧游标盖到新数据上。
- HTTP `/snapshot` 增加两个追加型公开字段：`history_version`（可空 opaque 字符串）与 `history_ready`（bool）。主页无逐卡 SSE 时据此引导缓存历史；缺失或 `False` 时前端回退轻量 `/chart`。
- 滚动 24h 金侧成交量在就绪时以**单条 SQL** 读取：分钟 K 线整段聚合 + 窗口两端原始成交部分和 + 持久游标之上的未落库尾段，共享同一 MVCC 快照；未就绪返回 `None`，调用方保留精确 SQL `SUM` 回退。
- `/chart` 的读取来源是物化 `fx_candle` + 内存 ring + 仅投影列的未落库尾段（不重扫原始 `FxTrade` ORM）。20,000 桶上限同时约束输出桶与细粒度来源桶（legacy rollup 的来源扫描有界），超限返回 422。读取窗口按整桶对齐（`[first_bucket, query_end)`），存储 K 线与原始尾段共用该窗口。覆盖游标为对齐窗口内已提交最大成交 id：**ring 路径**再与 ring 应用游标取较小值，**持久化 DB 路径**直接用窗口最大值（原始尾段以该值为上限，不再被 durable 游标压低）（整桶暂定语义）。
- `/history/fx/` 缓存 MISS 时在短读事务内对 `fx_pair` 行取共享锁（`FOR UPDATE read=True`）完成 EXISTS 未封存证明与 K 线读取，重读版本/就绪后即 `commit` 释放再返回响应；生产者先取该行 `FOR UPDATE` 再写 `FxTrade`。真正只读/standby 连接拿不到锁时返回非缓存 503，客户端回退 `/chart`，不污染不可变缓存。
- SSE 首包在旧报价/新闻字段外追加 `history_version`、`history_ready`、`history_tail`（各周期列式尾段）、`history_tail_at`、`history_tail_through_trade_id`；增量 `fx` 帧追加公开 `trades` 列表 `{id, ts, post_price, gold_volume}`。有界公开缓冲溢出时帧带 `history_invalidated=true`，要求客户端只补尾段，不能仅保留最后价格并假装历史完整。
- 前端不变式：真实 FX 成交的金侧成交量为正，故 `v=0` 唯一对应本地合成空桶（`o=h=l=c=prevClose`）。该空桶收到首笔真实成交时必须用该笔 `post_price` 定义整根 O/H/L/C 并写入真实量，不能保留 `prevClose` 或错误极值。

## 前端与恢复体验

`FxCandleChart.vue` 采用缓存历史段 + SSE 首包尾段 + 实时成交增量；用 FX 适配器复用 `useCandleHistory.ts` 的流程与可复用工具，保持 LMSR 行为。`FxOverview.vue` 同样从聚合历史读取近 24h 趋势；报价、走势分别更新，刷新期间保留上次成功结果，单项失败不挡另一项。默认不为主页每张卡片新增一个 SSE 长连接。

补齐 nginx `/api/v1/fx/stream/` 的关闭缓冲与长连接设置；已有 `/history/` 缓存 location 可以继续使用。不扩展浏览器自动交互验证。

## 实施阶段与验收

1. 先改 SQL 快照聚合和系统发布队列，独立降低 CPU；旧接口保持可用。
2. 增量存储、成交接入、恢复游标、回填、ring/flusher 完整后，切换 `/chart` 和历史/SSE 读取。
3. 前端切换后复测主页六请求与无人订阅 tick；完成发布准备，不自动授权推送或部署。

验收使用固定的三个 pair、每 pair 18,000 条合成成交和真实异步 PostgreSQL；对照记录应用 CPU、端到端耗时、事件循环延迟、查询/加载行数。最低要求：主页重复读取不构建全日成交对象；无人订阅发布不查询完整快照；tick 成本不随已有全日成交数增长；同样输出的走势图读取成本与桶数相称。

正确性重点是各种真实成交的精确 OHLCV、提交/回滚/幂等、flush 重试、重启补齐、回填接缝、SSE 去重及缓存封存。优先复用现有测试，仅在这些独立故障缺覆盖时加简短行为测试；UI 默认仅构建、已有单测和必要截图。生产 profiling 尚未取得，局部测量不能当作生产验收。
