# Ralph Loop 日志

> 格式约定：每轮追加一条；按**时间正序**（最早在上）。
> 规划/调研只要动了脑也要写一条，避免下一轮重复探索。

---

## 0. 起点规划（iteration 1 开头）

任务总目标（ralph prompt）："审阅代码，优化前端页面显示和后端性能，让平台更美观，操作更方便。"

**背景摸底**：
- `docs/frontend-review-2026-04-13.md` / `docs/backend-review-2026-04-13.md` 列了大量历史问题，末尾各有「已修复」清单。实测校对发现：
  - 后端 HIGH「K 线 fill bucket 爆内存」：`chart.py:258-260` 已有 `max_buckets > limit` 检查，且 `limit` Query `le=20000`。**已修，文档漏记**。
  - 后端 MEDIUM「用户资产 N+1 查询」：`user.py:60-69` `outcomes_by_market` 批量查询已落地。**已修，文档漏记**。
  - 后端 CRITICAL「交易无 SELECT FOR UPDATE」：评审"误报"段落已确认代码有 `_lock_*` 使用 `with_for_update()`。
- 前端评审末尾「仍需后端配合的问题」是少数真的没做的，是本次 ralph 的主战场候选。

**接下来若干轮的候选清单（从易到难）**：
1. ✅ 本轮做：交易记录响应带上 `market_title` / `outcome_label`，前端表格新增"市场/选项"列。
2. `MarketListItem` 响应补 `trade_count` / `last_trade_at` → 前端 MarketCard 展示活跃度。
3. `main.py` 的 `@app.on_event("startup")` 弃用 → 改 `lifespan`（MEDIUM）。
4. 添加 `/health` 端点（LOW，同时可配合 docker-compose healthcheck，但 docker-compose 是红线，本轮仅加端点，不改编排）。
5. 添加请求日志中间件（method/path/status/elapsed）（MEDIUM）。
6. 用户称号阈值从硬编码提到配置（MEDIUM）。
7. 前端首页"最近成交"/"涨跌榜"样式/性能 polish。
8. 前端 Leaderboard 排名变化指示（需后端历史数据支持，暂缓）。

**跳过/不做**（触红线或与原则冲突）：
- `Position.avg_cost` 数据库字段 — 要改 `models/base.py` + 数据迁移，属红线级，不做。
- `float → Decimal` 金融字段 — 涉及 `lmsr.py` + `market.py` + 全库迁移，体量过大，不在 ralph 循环范围内。
- 任何 `docker-compose.yml` / `deploy/` / `.github/workflows/` 改动。

---

## 1. 2026-04-23 — 交易记录附带市场名与选项名

**目标**：前端 `/user/transactions` 表格给用户显示「这笔是哪个市场、哪个选项」，并允许点击市场名直达交易页。

**动机**：`docs/frontend-review-2026-04-13.md` 末尾表格明确写「交易记录缺少关键信息：没有关联的市场名称、选项名称」，且标注"前端无法单独修复，需要后端配合"。原 Transactions.vue 只有类型/份额/单价/金额/时间五列，用户翻历史记录完全不知道在交易什么。

**范围**：仅 4 个文件，均不在红线/高敏感清单。
- `backend/app/schemas/user.py`
- `backend/app/api/v1/user.py`（`/transactions` 端点；其它端点不动）
- `thccb-frontend/src/types/user.ts`
- `thccb-frontend/src/pages/user/Transactions.vue`

**改动**：
- `schemas/user.py`：`TransactionRead` 增加 `market_id / market_title / outcome_label` 三个 Optional 字段（默认 None，向后兼容）。
- `api/v1/user.py` 的 `get_my_transactions`：加 `selectinload(Transaction.outcome).selectinload(Outcome.market)`；改为手动构造 `TransactionRead` 列表（原先直接返回 ORM 实例由 Pydantic 映射，新增跨表字段需要显式组装）。
- `types/user.ts`：`Transaction` 接口加三字段（可选）。
- `Transactions.vue`：在「类型」列后插入「市场 / 选项」列；市场名 `<a>` 点击通过 `router.push` 到 `/market/:id/trade`，选项名小字灰色附在下方；未用的 `NCard` / `NTag` 导入清理掉；`NDataTable` 加 `:scroll-x="900"` 避免移动端因新增一列而溢出。

**不改什么 / 为什么**：
- `Transaction` 数据表不加 `market_id` 字段 — 原表已有 `outcome_id`，`outcome.market_id` 一跳可达，JOIN 代价小；避免表结构变更触发迁移问题（项目无 Alembic）。
- 其他使用 `Transaction` 类型的地方（`stores/user.ts` 的 `transactions.slice(0, 10)`、首页可能用到）不需要改，因为新字段都是**可选**，旧用法完全兼容。

**风险 & 回滚**：
- 风险：`selectinload` 多一次批量 SELECT（outcome 表 + market 表各一次），对最近 50 条记录影响可忽略；前端点击新链接有无 bug 未实测（无运行环境）。
- 回滚：`git revert HEAD` 即可，改动集中在 4 个文件。

**验证**：
- 后端 `python -m py_compile app/api/v1/user.py app/schemas/user.py` ✅
- 后端 `python -c "from app.api.v1 import user; from app.schemas.user import TransactionRead"` ✅；`TransactionRead.model_fields` 确认三个新字段已注册 ✅
- 前端 `npm run type-check` ✅ 无错
- 前端 `npx eslint src/pages/user/Transactions.vue src/types/user.ts`：2 个报错均为**原有问题**（`vue/multi-word-component-names` 文件名 + 原行 77 `Record<string, any>`），非本轮引入；本轮无新增 lint 错误
- **UI 未实测**（无法启动本地后端+前端做浏览器验证），需用户本地验证以下路径：
  1. `/user/transactions` 列表正常渲染，多出「市场 / 选项」列
  2. 点击市场名跳转到对应 `/market/:id/trade`
  3. 移动端滚动正常（scroll-x 生效）
  4. 历史记录中 outcome 或 market 被删的边界情况（显示 "—"）

**下一轮候选**：
- MarketListItem 补 `trade_count` / `last_trade_at` 给首页 MarketCard（同类"后端补字段→前端展示"模式，相对安全）
- 或：`main.py` 的 `on_event("startup")` → `lifespan`（基础设施小改进）

---

## 2. 2026-04-23 iteration 2 — 市场卡片加成交笔数与最后成交时间

**目标**：MarketCard 卡片底部信息栏加「成交」笔数和「活跃」相对时间两项，用户在首页/市场列表一眼能看出市场活跃度。

**动机**：`docs/frontend-review-2026-04-13.md` 末尾「仍需后端配合」表明确列出「MarketCard 缺少成交笔数/最后成交时间」，后端 `MarketListItem` 无对应字段。延续上轮"后端补响应字段→前端展示"模式，同样不改表结构。

**范围**（5 个文件）：
- `backend/app/api/v1/market.py`（`/list` 端点；`market.py` 虽在高敏感清单，但本轮**仅**改 list 的响应组装，不动 buy/sell/quote/settle/resolve 交易核心路径）
- `backend/app/schemas/market.py`（`MarketListItem` 加字段；注意该 schema 目前定义但 list 端点未用 `response_model=` 绑定，改动是前瞻性的）
- `thccb-frontend/src/types/market.ts`
- `thccb-frontend/src/components/market/MarketCard.vue`
- `thccb-frontend/src/utils/formatter.ts`（新增 `formatRelativeTime` helper）

**改动**：
- **后端 `/list`**：批量聚合查询，用一次 `SELECT Outcome.market_id, COUNT(Transaction.id), MAX(Transaction.timestamp) FROM outcome LEFT OUTER JOIN transaction ... GROUP BY Outcome.market_id` 拿到每个市场的总成交数和最后成交时间。
  - 只统计 `buy/sell` 交易（`Transaction.type.in_([BUY, SELL])`），结算产生的 `settle/settle_lose` 视为系统流水不计入"活跃度"。
  - 用 OUTER JOIN 保证无交易的市场也返回 `trade_count=0`、`last_trade_at=None`。
- **响应 dict** 新增 `trade_count` 和 `last_trade_at` 两字段（`None` → 前端显示"—"）。
- **Schema** `MarketListItem` 加对应 Optional 字段（默认 0 / None）。
- **前端 `utils/formatter.ts`**：新增 `formatRelativeTime(input)`，规则：<30s → "刚刚"、<60s → "X秒前"、<60min → "X分钟前"、<24h → "X小时前"、<30d → "X天前"、其余走 `formatDate('short')`。支持 `null/undefined` 返回空串。
- **前端 `MarketCard.vue`**：`card-meta` 区域新增"成交"和"活跃"两项；`card-meta` CSS 改为 `flex-wrap: wrap` + 双轴 gap，避免移动端 4 项水平溢出；"活跃"值用 `meta-value--sm` 较小字号，并给 `title` 属性显示完整 ISO 时间便于 hover 查看精确时刻。

**不改什么 / 为什么**：
- 不 JOIN 把 `/list` 端点完整换成 `response_model=List[MarketListItem]` — 该端点响应 dict 形态与 schema 不一致（有 `closes_at/tags` 等字段 schema 里没有），重绑定属于无关重构，违反 CLAUDE.md "不顺手重构"。
- 不在 Transaction 表加 `market_id` 列 — 无迁移机制，维持现状通过 Outcome→Market JOIN。
- dayjs 有 `relativeTime` 插件但没装配，手写简单 helper 更轻量，也更可控。

**风险 & 回滚**：
- 风险：`/list` 增加一次 GROUP BY 聚合查询，全表 Transaction 扫描在数据量大时有成本——但有 `ix_transaction_outcome_timestamp` 索引（见 `models/base.py`），覆盖 outcome_id + timestamp 列，`WHERE market_id IN (...)` 通过 Outcome 表 JOIN，实际扫描仅限涉及市场对应的 outcomes。可接受。
- 另一风险：若 Transaction.type 未来新增值，`IN ([BUY, SELL])` 白名单语义仍正确（"活跃度"排除结算），不会误伤。
- 回滚：`git revert HEAD`，5 文件集中。

**验证**：
- 后端 `python -m py_compile app/api/v1/market.py app/schemas/market.py` ✅
- 后端 `python -c "from app.api.v1 import market; from app.schemas.market import MarketListItem"` ✅；`MarketListItem.model_fields` 包含新 `trade_count` / `last_trade_at` ✅
- 前端 `npm run type-check` ✅ 无错
- 前端 `npx eslint ...`（改动文件）无新增问题（原有 lint 问题不动）
- 后端 `pytest tests/` **未能执行**：`tests/test_auth.py` 的 `setup_db` fixture 触发 app 启动 → 尝试连接数据库（本地 `.env` 未为本 ralph 环境配置 Postgres），进程 hang。这是既有测试环境问题，非本轮引入；需要用户在本地有 DB 的环境下自行确认。
- **UI 未实测**（同上轮原因）。需要用户本地验证：
  1. 市场列表 / 首页 MarketCard 底部显示"成交"笔数与"活跃"相对时间
  2. 无交易的市场显示"0"和"—"
  3. 小屏幕下 4 项 meta 正常换行不溢出
  4. 首页接通 `/api/v1/market/list` 后 `trade_count` / `last_trade_at` 字段被正确消费

**下一轮候选**：
- `main.py` 的 `@app.on_event("startup")` → `lifespan`（pytest 告警里看到弃用警告，迁移后能消掉项目里的一类 DeprecationWarning 噪音）
- 新增 `/health` 端点（LOW，利于运维，新增不动老路径）
- 添加请求日志中间件（method / path / status / elapsed）
- 首页改用 `formatRelativeTime` 统一「实时成交流」的时间展示（顺手扩展上轮 helper）

---

## 3. 2026-04-23 iteration 3 — main.py 迁移 lifespan + 新增 /health

**目标**：消除 `@app.on_event("startup")` 弃用警告；提供标准 `/health` 端点（含 DB ping）便于容器/nginx/外部监控探活。

**动机**：
- 上轮 `pytest` 日志出现两条 `DeprecationWarning: on_event is deprecated, use lifespan event handlers instead`，FastAPI 0.93+ 推荐用 `lifespan` context manager。
- 部署架构图里已有"nginx 反代 → backend"链路，但后端没有独立 `/health` 端点，根路径 `/` 返回欢迎信息不适合当 healthcheck（任何错误下也能返 200，没有 DB 维度的健康信号）。
- `docs/backend-review-2026-04-13.md` LOW #15 明确点名"缺少 /health"。

**范围**：仅 `backend/app/main.py`，安全区。

**改动**：
- 引入 `asynccontextmanager` + `lifespan(app)`：startup 段内部调用 `await init_db()` 与 `setup_admin(app, engine)`；yield 之后 shutdown 段调用 `await engine.dispose()`（顺手修的小风险点——原代码从无 shutdown 钩子，优雅停机时连接池未显式释放，Docker `stop_grace_period: 8s` 下有可能留连接）。
- `FastAPI(title=..., lifespan=lifespan)` 替换原 `FastAPI(title=...)` + `@app.on_event("startup")`。
- 新增 `GET /health`：用 `engine.connect()` 执行 `SELECT 1`；成功返回 `{"status": "ok", "db": "ok"}`；失败抛 503 + 异常类型名（不泄露栈/SQL）。
- `tags=["Meta"]` 让 `/health` 在 `/docs` 里单独分组，不与业务接口混在一起。

**不改什么**：
- 不碰 `docker-compose.yml` 的 `backend` service 配置（红线），因此 compose 层 healthcheck 仍缺失——后续由用户按需启用（`test: curl -f http://localhost:8004/health`）。
- 不动 `deploy/nginx.conf` —— 红线；用户若想让 nginx upstream 探活，可加 `location = /health`。
- 不一并修 `backend/app/schemas/market.py:14` 的 `min_items` 弃用警告（另一类 Pydantic V2 迁移），非本轮范围。

**风险 & 回滚**：
- 风险：`engine.dispose()` 在 lifespan shutdown 里调用，若 uvicorn 被 SIGKILL 前没走到 shutdown 段，仍然是残留连接——没比以前更差，只是更好。
- 风险：`/health` 会为每次调用开一条新连接（`engine.connect()`），高频调用时可能对连接池压力略增；但 healthcheck 的典型频率（10s/次）完全可吞。
- 回滚：`git revert HEAD` 即可，单文件改动。

**验证**：
- `python -m py_compile app/main.py` ✅
- `python -c` 导入 `app.main`，`warnings.catch_warnings` 过滤 `"on_event is deprecated"`：**结果为 0 条** ✅（迁移成功）
- 路由登记确认：`'/health' in routes == True` ✅、`'/' in routes == True` ✅、`app.router.lifespan_context is not None == True` ✅
- 前端 `npm run type-check` ✅（不受影响）
- **端到端未实测**：需要跑起 backend + DB，`curl http://127.0.0.1:8004/health` 应返回 `200 {"status":"ok","db":"ok"}`；DB 故障时应返回 503。

**下一轮候选**：
- 请求日志中间件：method / path / status / elapsed_ms 写到 logger（同 `main.py`，纯增量 middleware）
- 首页 `Home.vue` 接通 `formatRelativeTime` 统一"实时成交流"的时间展示
- 可选：修 `backend/app/schemas/market.py:14` 的 `min_items → min_length` 等 Pydantic V2 弃用警告（一组小改，纯前向兼容）

---

## 4. 2026-04-23 iteration 4 — RecentTrades 接通 formatRelativeTime（DRY + hover 完整时间）

**目标**：首页「实时成交」组件的时间展示统一走 `utils/formatRelativeTime`，删除组件内本地重复实现；鼠标悬停可看完整 ISO timestamp。

**动机**：iteration 2 新增了 `formatRelativeTime` helper 放进 `src/utils/formatter.ts`，当时只接到 `MarketCard`。本轮摸到 `RecentTrades.vue` 里发现它**自己写了一份** `relativeTime(iso)`（行 40-50），逻辑几乎等价、但档位不同（无"刚刚"、无"X天前"），既是重复代码，也导致全站相对时间口径不一致。合并既减行数也统一体验。

**范围**：仅 `thccb-frontend/src/components/home/RecentTrades.vue` 一个文件。

**改动**：
- 导入 `formatRelativeTime from '@/utils/formatter'`。
- 删除本地 `relativeTime(iso)` 函数（-14 行）。
- 模板 `{{ relativeTime(t.timestamp) }}` → `{{ formatRelativeTime(t.timestamp) }}`；同时给 `.rt-time` 加 `:title="t.timestamp"`，悬停显示完整 ISO 时间戳（与 MarketCard 的"活跃"列统一做法）。
- 净差：+2 insert / −14 delete，代码减少。

**顺手没做什么**：
- 不改 `.rt-time` 的 CSS/布局/阈值。
- 不抽 `truncate(s, n)` helper — 它也是小工具，但只此组件用，抽离属于无关重构。
- Movers.vue 已确认**不涉及**时间展示，跳过。

**行为差异**（用户需留意）：
- 本地旧版：30 秒内显示 "X秒前"；新版：<30s 显示 "刚刚"、30-59s 显示 "X秒前"。体验改善。
- 本地旧版：>24h 显示 "MM-DD HH:MM"（含时分）；新版：<30d 显示 "X天前"、>=30d 显示 "YYYY-MM-DD"。对"实时成交"这类"最近几分钟/小时"为主的场景，日期精确到分钟没必要；实在需要精确时间，hover title 可见完整 ISO。

**风险 & 回滚**：
- 风险：仅 UI 展示层语义变化，无逻辑/数据影响。
- 回滚：`git revert HEAD` 即可；或直接把 utils helper 也回退。

**验证**：
- `npm run type-check` ✅ 无错
- `npx eslint src/components/home/RecentTrades.vue` ✅ 无输出（无错无警告）
- **UI 未实测**；需要用户验证：
  1. 首页"实时成交"列表第一列相对时间正确（刚成交显示"刚刚"、一小时前显示"1小时前"）
  2. 鼠标悬停时间格子能显示完整 ISO timestamp

**下一轮候选**：
- 请求日志中间件（`main.py`，纯增量）
- Pydantic V2 弃用清理：`schemas/market.py:14` `min_items → min_length` 等
- 首页 Movers / Transactions 继续寻找可以统一格式化的点

---

## 5. 2026-04-23 iteration 5 — MarketList 搜索加 300ms 防抖

**目标**：消除市场列表搜索框每敲一键就触发一次 `/api/v1/market/list` 的问题。

**动机**：`docs/frontend-review-2026-04-13.md` MEDIUM #8 "搜索无防抖（MarketList.vue:36-48）" 明确列出，但既有"已修复"表里没有这条。亲自 grep 代码确认：`watch([searchQuery, statusFilter], () => loadMarkets())` 同步触发，输入"东方"4 字符 → 4 次请求。既无谓占用 nginx 20r/s 通用限速桶，也让后端每次都 GROUP BY 聚合 `trade_count/last_trade_at`（iteration 2 新增的 JOIN）。

**范围**：仅 `thccb-frontend/src/pages/market/MarketList.vue` 一个文件。

**改动**：
- 拆 `watch([searchQuery, statusFilter], …)` 为两个独立 watch：
  - `watch(searchQuery)`：300ms 防抖触发 `loadMarkets`；每次 reset `currentPage` 到 1。
  - `watch(statusFilter)`：下拉框切换立即生效（无防抖）。
- 新增 `onBeforeUnmount` 清理防抖 timer 避免内存泄漏。
- 防抖模式参考 `TradingView.vue:259-263` 的 `debouncedGetQuote`（400ms）—— 项目里已有范式，保持一致。搜索取稍短的 300ms，输入更敏感。

**不改什么**：
- 不修 `buildParams()` 里的 `Record<string, any>`（eslint 提示的是**原有**代码，非本轮引入；不顺手重构）。
- 不抽通用 `useDebouncedWatch` composable——单处使用，抽出属于过度设计。
- `sortBy` 变化已在第 96 行单独处理（只重置页码，不重拉后端），逻辑正确，不动。

**风险 & 回滚**：
- 风险：用户 300ms 内快速敲入多字符时，会经历"等待→一次请求"而非"每字符一次"，体感上可能感觉"搜索不即时"。300ms 是业内常见值，实测应无感。
- 回滚：`git revert HEAD` 即可。

**验证**：
- `npm run type-check` ✅ 无错
- `npx eslint src/pages/market/MarketList.vue`：1 个 `any` 报错为**原有问题**（行 35 `Record<string, any>`），非本轮引入
- **UI 未实测**；用户验证路径：
  1. `/market/list` 页面敲"东方炒炒币"5 个字符，应只触发 1 次后端请求（快速敲字时）
  2. 敲完停 300ms 后列表刷新
  3. 切换"交易中/已暂停/已结算"下拉应即时响应
  4. 切换到其它路由再回来，无 console 报"leaked timer"之类

**下一轮候选**：
- 请求日志中间件 `main.py`（观测性）
- Pydantic V2 弃用清理：`schemas/market.py:14` `min_items` + 可能其他文件同类
- 抽 `useDebouncedRef` 或 `useDebouncedCallback` composable 复用（如后续还需要多处防抖再做，现在是一处，不做）

---

## 6. 2026-04-23 iteration 6 — 请求日志中间件（method/path/status/elapsed）

**目标**：给后端加一个极简的访问日志中间件，方便排查慢请求、错误突增、异常路径探测。

**动机**：`docs/backend-review-2026-04-13.md` MEDIUM #10 "缺少请求日志中间件" 未修；部署到生产后如果遇到 "用户说卡"，没有逐请求的 path+耗时数据会很被动。同时 iteration 2 新增的 GROUP BY 聚合和本轮会继续叠加的改动都需要观测性兜底。

**范围**：仅 `backend/app/main.py`，非红线/非高敏感。

**改动**：
- 新增 `access_logger = logging.getLogger("thccb.access")`，独立 logger 名便于 uvicorn/nginx 组合部署时单独设等级或输出到独立 handler。
- `@app.middleware("http")` 注册 `log_requests`：
  - `time.perf_counter()` 计时（比 `time.time()` 更稳）。
  - `INFO` 级常规记录；`status >= 500` 升到 `WARNING`，exception 走 `access_logger.exception` 且 re-raise 不吞。
  - 日志格式：`METHOD PATH STATUS ELAPSED_ms`（例：`GET /api/v1/market/list 200 18.4ms`）。
- 跳过清单 `_LOG_SKIP_PREFIXES`：
  - `/health`（10s 探活会淹没日志）
  - `/api/v1/stream`（SSE 长连接，结束时的一条记录毫无用处且时长失真）
  - `/docs`、`/redoc`、`/openapi.json`（开发态用）
  - `/favicon.ico`（浏览器自动请求）

**不改什么**：
- 不改 `settings` 的 `DB_ECHO` / logging 根配置——保持 uvicorn 默认 handler，格式与已有警告（SECRET_KEY 未配置等）一致。
- 不加 `client_ip`——项目前有 nginx 反代，`request.client.host` 会恒为 127.0.0.1，未处理 `X-Forwarded-For` 时加 ip 会误导；后续如需可按任务独立做。
- 不记录请求体/响应体——涉及敏感数据（密码、token）且 body 可能是 SSE 流，风险大收益小。
- Admin 面板请求暂不 skip：sqladmin 静态资源访问量有限，先留着观测。

**风险 & 回滚**：
- 风险：中间件对每个请求加一次 `perf_counter()` + 一次 log 写入，成本 ~1μs 级，影响可忽略。
- 风险：中间件异常会影响所有请求——已用 try/except 捕获下游异常并 re-raise，中间件自身逻辑极简。
- 回滚：`git revert HEAD` 单文件。

**验证**：
- `python -m py_compile app/main.py` ✅
- **端到端测试**（用 `fastapi.testclient.TestClient` + 捕获 logger handler）：
  - `GET /` 200 → 记录 `INFO thccb.access GET / 200 1.2ms` ✅
  - `GET /health` 200 → **跳过**（无输出）✅
  - `GET /docs` 200 → **跳过** ✅
  - `GET /api/v1/nonexistent-xyz` 404 → 记录 `INFO thccb.access GET /api/v1/nonexistent-xyz 404 0.3ms` ✅
- 无需前端检查；`npm run type-check` 可略。

**下一轮候选**：
- Pydantic V2 弃用清理：扫一遍 `schemas/*.py` 中所有 `min_items` / `max_items` / `regex`
- 请求日志升级（可选、看反馈）：加 user_id（若已认证）、加 trace id
- 其他 UI 改进：首页 Movers 点击跳转、Transactions 空状态 polish

---

## 7. 2026-04-23 iteration 7 — Pydantic V2 弃用清理 + Home 热门市场按成交排序

**目标 A**：清 Pydantic V1 残余 `min_items` 弃用（V3 会移除）。
**目标 B**：收割 iteration 2 铺好的 `trade_count`/`last_trade_at` 基建，让首页"热门市场"真的按活跃度排而不是时间倒序前 4 个。

---

### A. `min_items → min_length`（1 行改动）

**扫描结果**（`grep -rE "min_items|max_items|\bregex\s*=|allow_mutation|const\s*=\s*True"`）：全仓仅 `backend/app/schemas/market.py:14` 一处，其它 schema 干净。一击即中。

**改动**：`MarketCreate.outcomes: List[str] = Field(..., min_items=2, ...)` → `min_length=2`。

**验证**：
- `python -m py_compile` ✅
- Python 运行时捕 DeprecationWarning：`min_items DeprecationWarnings: 0` ✅
- 行为等价性：`MarketCreate(outcomes=["only_one"])` 仍抛 `List should have at least 2` ValidationError ✅

**独立 commit**：`00d5bf2 fix(backend): MarketCreate outcomes 用 min_length 替代弃用的 min_items`

---

### B. Home 热门市场按成交笔数排序（UX）

**动机**：`docs/frontend-review-2026-04-13.md` "仍需后端配合" 表里最后一条 "热门市场按热度排序"。iteration 2 已把 `trade_count`/`last_trade_at` 塞进 `/market/list` 响应，但前端 `Home.vue:33` 还在用 `marketStore.activeMarkets.slice(0, 4)`——`activeMarkets` 的源 `markets` 后端返回是按 `created_at desc` 排的，即"热门市场"其实是"最新 4 个"。

**范围**：仅 `thccb-frontend/src/pages/home/Home.vue`，1 个 computed。

**改动**：
- `featuredMarkets`：先 `[...activeMarkets]` 拷贝避免原地排序污染 store，然后 `sort` 规则：
  1. 主键：`trade_count` 降序
  2. tie-breaker：`last_trade_at` 时间戳降序（更近的成交在前）
  3. 两者都缺的市场自动沉底
- `slice(0, 4)` 取前 4。

**顺带不改什么**：
- 不改 MarketList 的排序下拉，那里用户可显式控制，不强加"热度"默认。
- 不加"活跃度"专用 UI 标识——留给用户自己看 MarketCard 的"成交"列。

**风险 & 回滚**：
- 风险：早期市场全无成交时，`trade_count=0` 全部相等，此时 tie-breaker 让 `last_trade_at=null` 的也全相等 → 回退到原数组顺序（`created_at desc`），与旧行为一致，无回归。
- 回滚：`git revert HEAD`。

**验证**：
- `npm run type-check` ✅ 无错
- `npx eslint src/pages/home/Home.vue`：1 个既有 `vue/multi-word-component-names` 问题（`Home` 单词），非本轮引入
- **UI 未实测**；验证路径：
  1. 首页"热门市场"区 4 张卡按 `成交` 字段从大到小排列
  2. 打开两个市场各做一笔 buy，刷新首页后"热门市场"会把这两个顶到前面
  3. 并列时最近成交的市场排前

**下一轮候选**：
- 首页 `Movers` 单项加点击跳转到该市场交易页
- Admin `MarketManage` 的创建/结算表单 polish
- 后端：Transaction 接口加分页（现在硬编码 `limit 50`，评审 LOW #13）
- 抽通用 `useDebounce(callback, ms)` composable（如本轮/下轮再出现一次防抖需求才做）

---

## 8. 2026-04-23 iteration 8 — 交易历史接口开放 limit 参数，前端默认取 100 条

**本轮一次误判**：先去看了 `Movers.vue` 想加点击跳转，结果**已经实现**了（`@click="goToMarket"` + `tabindex` + `@keyup.enter` + `.movers-row:hover` 全套）。说明上轮写候选时没来得及验证，是"规划记忆"过时。下一轮写候选清单前**要先 grep 验证**再写进日志。

**改做**：评审 LOW #13 「交易历史缺少分页」。

**动机**：`/api/v1/user/transactions` 硬编码 `.limit(50)`。活跃用户两三天就看不到最初的成交了，既没 UI 翻页也没 API 参数。最小改动让客户端能拿到更多。

**范围**：2 个文件。
- `backend/app/api/v1/user.py`（非红线）
- `thccb-frontend/src/api/user.ts`（store 和 UI 不动）

**改动**：
- **后端** `get_my_transactions` 加 `limit: int = Query(50, ge=1, le=200, ...)`；响应 schema 不变；默认 50 → 现有外部调用者无感。
- **前端** `userApi.getTransactions(limit = 100)` 默认值从无参改为 **100**，通过 `{ params: { limit } }` 透传。
  - 选 100 是因为：1) 比原 50 多一倍，活跃用户体感显著；2) 远低于后端上限 200，前端分页 UI 出来之前留有扩展空间；3) 前端只渲染表格，一次渲染 100 条对 NDataTable + 浏览器无压力。
- **Store / UI 不动**：`stores/user.ts::fetchTransactions` 调 `userApi.getTransactions()` 无参，会自动走新的默认 100；`Transactions.vue` 不用改一行。

**不改什么**：
- 不做游标/偏移分页（`before_id`/`offset`），那是完整特性，留给后续迭代（前端要加"加载更多"按钮 + 边界态）。
- 不改 `.limit` 以外的 schema/响应字段。
- 后端默认值保持 50，**避免**改成 100 同时修改响应语义——现有从 API 直调的脚本/curl 行为不变。

**风险 & 回滚**：
- 风险：50 → 100 条，前端初次加载多拖 50 条 Transaction 记录 + JOIN（上轮 iteration 1 的 `selectinload(outcome→market)`），对 SQL 压力微增但可忽略（`ix_transaction_user_timestamp` 索引覆盖）。
- 回滚：`git revert HEAD` 2 文件。

**验证**：
- 后端 `python -m py_compile app/api/v1/user.py` ✅
- 后端 `inspect.signature` 确认新参数：`limit default=50, metadata=[Ge(ge=1), Le(le=200)]` ✅
- 前端 `npm run type-check` ✅ 无错
- 前端 `eslint src/api/user.ts` 无输出 ✅
- **UI 未实测**；用户验证：`/user/transactions` 刷新后如果该用户历史 > 50 条，应看到最多 100 条；网络面板里 `/api/v1/user/transactions?limit=100`。

**下一轮候选**（先 grep 验证再写）：
- Transactions.vue 加"加载更多"按钮（已铺好后端，UI 层面真正的分页）
- Admin `MarketManage` 表单 polish（先读再决定做什么）
- 抽 `useDebounce` composable（只此一处暂不抽）
