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
