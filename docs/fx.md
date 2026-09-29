# 幻想外汇 (FX) 运维与部署手册

FX 是预测市场旁边的一个**默认关闭**的子游戏：玩家用金圆券（G）与单一「幻想外币」（F）按恒定乘积
AMM 兑换。它不复用 LMSR 的 `market` / `outcome` / `position` 表，而是拥有独立的
`fx_pair` / `fx_treasury` / `fx_wallet` / `fx_trade` / `fx_event` 表。

后续摩拉参数、杠杆与强平准备见 [FX 摩拉参数与强平准备](fx-margin-preparation.md)；该文档为候选方案，不改变本手册的现货运行规则。

> **当前发布：生产总闸必须保持 `fx_enabled=false`，本轮修复不打开 gate。**
> Task 9 发现的审计日期序列化缺陷已修复；whole-branch review 的 6 个 Important
> 已在一次 bounded fix 中处理（价格口径、engine 配置、锁序、管理端/玩家读取、前端净值；
> 见文末第 11 节与 `.superpowers/sdd/2026-09-28-fx-market/fix-wave-report.md`）。
> 该 review 遗留的 3 个 Minor（公开成交流隐藏系统 `source`、病态费率/极小储备不再 500、
> Task 9 历史计数与措辞）也已在本轮 minor fix 中处理，见
> `.superpowers/sdd/2026-09-28-fx-market/minor-fix-report.md`。
> 开市仍需单独的发布批准。

代码入口：

| 层 | 文件 |
| --- | --- |
| 模型 / schema | `backend/app/models/fx.py`、`backend/app/schemas/fx.py` |
| AMM / 交易 | `backend/app/services/fx/{amm,quantize,trading}.py` |
| 行情 / SSE | `backend/app/services/fx/market_data.py`、`backend/app/api/v1/fx_stream.py` |
| 引擎 / 事件 / 调度 | `backend/app/services/fx/{engine,randomness,scheduler}.py` |
| 净值 / 审计 / 重置 | `backend/app/services/fx/valuation.py`、`backend/app/services/audit_replay.py`、`backend/scripts/season_reset.py` |
| 玩家 / 管理 API | `backend/app/api/v1/fx.py`、`backend/app/api/v1/admin_fx.py` |
| 端到端测试 | `backend/tests/test_fx_end_to_end.py` |

---

## 1. 不可动摇的约束

1. **总闸默认关闭**：`site_config.fx_enabled` 默认 `false`；没有建立并注资货币对之前不得开市。
2. **单货币对**：第一版同一时间只允许一个 `status='trading'` 的货币对；开市后不可再改
   `currency_code` / `currency_name`。
3. **分币种守恒**：系统订单前后 `G + T_G` 与 `F + T_F` 分别不变（G=池子金圆券储备，
   T_G=treasury 金圆券；F/T_F 同理）。系统输入从 treasury 扣、输出进 treasury；玩家交易、
   手续费和注/撤资全部写审计并可回放。
4. **事件支出只算本事件**：`fx_event.parameter_snapshot["spent"]` 只累计该事件实际的金圆券
   名义支出，不能用 `treasury.daily_spend`（全局）代替。
5. **事务与锁**：池子行锁先于用户行锁；系统订单失败用 savepoint 隔离，不中止其他货币对；
   公开 SSE 只在事务提交后广播。
6. **公开字段白名单**：公开 API/SSE 只返回行情与已发布新闻，绝不泄露
   `target_price` / `shock_ratio` / `first_reaction_ratio` / `parameter_snapshot` /
   未来订单 / 随机状态。
7. **净值口径**：FX 按最新 AMM 边际价 MTM 进入展示净值 / 排行榜 / rank；
   **不进入** LCV、借款额度或强平抵押物。`debt > 0` 的用户不能买入，但可以卖出已有外币。
8. **生产 gate 关闭**：生产环境任何阶段都不得打开 `fx_enabled`，除非本次发布明确获批。

---

## 2. 迁移顺序

FX 使用的 Alembic revision：

| 顺序 | revision | 文件 | 内容 |
| --- | --- | --- | --- |
| 依赖 | `0d0ac23efa85` | `2026_09_26_2301-0d0ac23efa85_add_administrator_redemption_fulfillment.py` | 兑换履约（FX 的前置） |
| FX | `fx_tables_20260928` | `2026_09_28_1200-fx_tables_add_fx_tables.py` | 创建 5 张 FX 表 |

`fx_tables_20260928` 的 `down_revision = "0d0ac23efa85"`，所以只要按顺序执行即可：

```bash
# 生产：推荐走 deploy.sh（已有库自动 alembic upgrade head）
bash deploy/deploy.sh

# 手动 / 本地
cd backend
alembic current          # 查看当前 revision
alembic history          # 确认 0d0ac23efa85 -> fx_tables_20260928
alembic upgrade head     # 应用兑换履约 + FX 迁移
```

规则：

- **已有数据库绝不能跑 `init_db.py`**（它会按当前模型 DROP/CREATE，等于删库）。
  已有库只走 `alembic upgrade head`。
- 全新空库由 `deploy.sh` 走 `init_db.py` 建表并 `stamp` 到 head；本地 SQLite 开发同理。
- 迁移同时支持 SQLite（测试 / 本地）和 PostgreSQL（生产）。
- FX 迁移只加表，不改既有表；兑换履约是它的前置 revision，不要跳过。

---

## 3. 首次开市 runbook

> 在打开总闸之前先完成前面所有步骤并跑完第 9 节验证命令。本任务范围内总闸保持关闭，
> 开市需要单独的发布批准。

1. **确认 gate 关闭**

   ```bash
   # 管理端读取（需超管 token）
   curl -s -H "Authorization: Bearer <admin>" http://127.0.0.1:8004/api/v1/admin/fx/config
   # 期望 fx_enabled == "false"
   ```

2. **创建货币对（草稿 → 注资 → 开市）**，只走管理 API：

   ```bash
   # 2.1 建草稿（status=draft，初始池子/treasury 都是 1）
   curl -s -X POST http://127.0.0.1:8004/api/v1/admin/fx/pairs \
     -H "Authorization: Bearer <admin>" -H 'Content-Type: application/json' \
     -d '{"currency_code":"MOON","currency_name":"月都元","status":"draft",
          "gold_reserve":"1","foreign_reserve":"1","target_price":"1",
          "initial_price":"1","target_min":"0.5","target_max":"2",
          "buy_fee_rate":"0.01","sell_fee_rate":"0.01"}'

   # 2.2 注资（只能通过 admin service；见第 4 节）
   curl -s -X POST http://127.0.0.1:8004/api/v1/admin/fx/pairs/<id>/fund \
     -H "Authorization: Bearer <admin>" -H 'Content-Type: application/json' \
     -d '{"gold_amount":"1000","foreign_amount":"1000"}'

   # 2.3 开市：唯一一个 trading pair
   curl -s -X PATCH http://127.0.0.1:8004/api/v1/admin/fx/pairs/<id> \
     -H "Authorization: Bearer <admin>" -H 'Content-Type: application/json' \
     -d '{"status":"trading"}'

   # 2.4 确认公开行情可读（此时 gate 仍关闭；成交必须 403）
   curl -s http://127.0.0.1:8004/api/v1/fx/pairs
   curl -s http://127.0.0.1:8004/api/v1/fx/pairs/<id>/snapshot

   # 2.5 管理端只读列表（含草稿 + treasury 余额/今日支出；刷新后运营状态不丢）
   curl -s -H "Authorization: Bearer <admin>" \
     http://127.0.0.1:8004/api/v1/admin/fx/pairs
   ```

3. **设定运营参数**：通过 `PUT /api/v1/admin/fx/config`
   （key 必须是 `FX_DEFAULT_CONFIGS` 中的键），或先在管理前端改。

4. **最后才开闸**：`PUT /api/v1/admin/fx/config {"key":"fx_enabled","value":"true"}`。
   本任务范围内总闸保持 `false`，开市需要单独的发布批准。Task 9 已修复审计日期序列化缺陷。

---

## 4. 注资 / 撤资只能走 admin service

**禁止**直接 `UPDATE fx_pair SET gold_reserve=...`、`INSERT fx_treasury`、或任何绕过
service 的 SQL 改池子。所有池子/treasury 变更必须经由：

```
POST /api/v1/admin/fx/pairs/{id}/fund      # 站内发行：池子与 treasury 同时增加
POST /api/v1/admin/fx/pairs/{id}/withdraw  # 站内回收：池子与 treasury 同时减少
```

- `fund_pair` / `withdraw_pair` 在 `scheduler.py` 内执行，带池子/treasury 行锁并写
  `fx_fund` / `fx_withdraw` 审计（含 pool/treasury 前后快照），可在 `audit_replay` 中对账。
- 语义注意：`fund` 会把金额同时加进**池子**和 **treasury**，因此单币种系统总量
  （池子 + treasury）按 `2*fund - 2*withdraw` 变化；这是「发行到池子 + 发行到缓冲」的设计。
- `withdraw` 若会使池子储备 `<= 0` 或超过 treasury 余额，返回 409，不产生副作用。
- 货币对创建时的初始发行也会写 `fx_fund`（`action=initial_issuance`）。

---

## 5. 预算与储备护栏

`site_config` 中的 FX 默认值（`app/services/site_config.py` 的 `FX_DEFAULT_CONFIGS`）：

| key | 默认 | 含义 |
| --- | --- | --- |
| `fx_enabled` | `false` | 总闸；关闭时成交 403，调度器不发计划事件、不做噪声/干预 |
| `fx_hourly_sigma` | `0.002` | 目标价对数随机游走的小时波动率（0.2%） |
| `fx_step_max_ratio` | `0.001` | 每个 tick 目标价相对变动上限（0.1%） |
| `fx_noise_interval_sec` | `30` | 噪声订单平均间隔（指数分布） |
| `fx_noise_pool_ratio` | `0.0001` | 单笔噪声不超过池子的 0.01% |
| `fx_system_half_life_sec` | `600` | 正常目标干预半衰期 |
| `fx_default_price_move_limit` | `0.005` | 正常（非事件）干预的单步价格变动上限 0.5%，engine 每 tick 实时读取，同时用作系统下单 size 上限 |
| `fx_daily_budget` | `100000` | 每个 treasury 每日系统支出上限（按 UTC 日期重置） |

DB / service 层护栏：

- 池子储备、treasury 余额必须 `> 0` / `>= 0`（check constraint + service 校验）；
  系统订单拒绝零输出和非正储备。
- `target_min <= target_price <= target_max`；目标还会被夹到
  `[max(target_min, 0.5*initial_price), min(target_max, 2*initial_price)]`。
- 费率 `0 <= rate < 1`（精确 `1` 被拒绝，避免手续费吃掉全部输入）；金额 6 位小数，
  手续费向上量化、输出向下量化。
- 活动事件参数：`shock_ratio` ≤ 5%（`black_swan` ≤ 20%）、
  `first_reaction_ratio ∈ [0.1, 0.9]`、`window_sec ∈ [30, 1800]`、`budget > 0`。
- 事件 `spent` 达到 `budget` 后事件完成；`daily_spend` 达到 `fx_daily_budget` 后
  系统订单停手并记录原因。
- 单货币对约束由 admin API 强制。

**公开价格口径**：`GET /fx/pairs/{id}/snapshot` 的 `price`（边际价）、`buy_price`、
`sell_price` 统一为「金圆券 / 1 外币」。`buy_price` 是买入外币的有效 ask
（金入 / 外币出），`sell_price` 是卖出外币的有效 bid（金出 / 外币入），
`spread = buy_price − sell_price ≥ 0`（含手续费时为正）。玩家个人持仓/成交历史走
`GET /fx/pairs/{id}/wallet` 与 `GET /fx/pairs/{id}/my-trades`（需登录，只含本人）。

---

## 6. 事件运营（草稿 / 计划 / 发布 / 恢复）

事件状态机：`draft → scheduled → published → completed`，另有 `cancelled`。

- 创建：`POST /api/v1/admin/fx/events`（带 `scheduled_at` 即同时排期）。
- 排期：`schedule_event` 会锁 `fx_pair` 行并拒绝与同货币对已排期/已发布事件窗口重叠
  （409），且不修改草稿。
- 发布：`POST /api/v1/admin/fx/events/{id}/publish`，失败时事件置 `cancelled` 并写
  `fx_event_cancel` 审计；已发布/已完成重复调用是幂等的，不重复首轮干预。
- 取消：仅 `draft` / `scheduled` 可取消；已发布不可撤销。
- 恢复：调度器每 5 秒扫描一次，以**数据库状态为唯一权威**；重启后按 UTC 时间顺序补发
  逾期 `scheduled` 事件，重复扫描不重复首轮干预。
- 事件参数（shock / first_reaction / target / parameter_snapshot）只出现在管理 schema
  `FxEventAdmin`；公开 schema `FxEventPublic` 只有标题、正文、kind、时间。

> 事件发布 / 恢复路径由 `tests/test_fx_end_to_end.py`、`tests/test_fx_events.py` 与
> `tests/test_admin_fx.py` 在隔离数据库上验证通过（含已修复的审计日期序列化）。

---

## 7. 暂停与赛季重置

**暂停**：`PATCH /api/v1/admin/fx/pairs/{id}` `{"status":"paused"}`。
暂停后公开行情仍可读，但成交返回 403，系统干预停止。

**赛季重置**：使用现有停服单事务入口，清 FX 数据并关闭 gate：

```bash
cd /home/deploy/TouhouCCB
bash deploy/season_reset.sh preview                      # 只读预览
bash deploy/season_reset.sh backup                       # 备份 + 恢复验证
SEASON_CONFIRM=RESET bash deploy/season_reset.sh execute # 执行
```

`reset_fx_state(session)` 在调用方事务内：

1. 删除全部 FX 审计（`fx_trade/fx_fund/fx_withdraw/fx_event_*`）；
2. 按子表→父表删除 `fx_wallet → fx_trade → fx_event → fx_treasury → fx_pair`；
3. 将 `site_config.fx_enabled` 置为 `false`。

保留项：

- `redemption_transaction`、`danmuku_exchange` 与全部兑换核销/撤销审计**保留**；
- 不主动重置 PostgreSQL 自增序列（避免旧 K 线缓存串季）；
- dry-run 零写入；自检失败整次重置回滚。

---

## 8. 禁止的操作

- **禁止直接改生产池 / treasury**：不要 `UPDATE fx_pair`、`UPDATE fx_treasury`、
  `INSERT fx_wallet/fx_trade`。所有变更走 admin service / 玩家成交服务。
- **禁止在已有库跑 `init_db.py`**，禁止 `DROP` / `TRUNCATE` FX 表；
  赛季清理只走 `season_reset` 的停服事务。
- **禁止打开生产 gate**：除非本次发布明确获批。第 11 节的审计日期序列化缺陷已在当前
  分支修复并写入文档，不再是待修阻断项；开市仍是单独的发布动作。
- **禁止手工改事件状态**：只能 publish / cancel，且不可编辑/撤销已发布事件。
- **禁止把隐藏字段加进 public schema / SSE**；新增公开字段前先确认白名单。

---

## 9. 回滚与验证命令

### 9.1 回滚

```bash
# 先停后端并备份（迁移回滚会丢 FX 表数据）
docker compose stop backend
docker compose exec -T postgres pg_dump -U thccb thccb > backups/thccb_pre_fx_rollback_<ts>.sql
# 或 SQLite：cp backend/data/thccb.db backups/thccb_pre_fx_rollback_<ts>.db

cd backend
alembic downgrade 0d0ac23efa85   # 删除 5 张 FX 表（数据丢失，仅应急）
# 如需重新部署旧镜像：
# docker compose pull && docker compose up -d
```

> 生产数据回滚通常不安全；回滚只作应急，正常应「加一个向前的修正 migration」。

### 9.2 验证命令

```bash
# 后端语法 + 导入
cd backend
python -m compileall -q app scripts
python -c "import app.main"

# Task 9 隔离端到端（不依赖 conftest / 全局 async fixture）
python -m pytest -q --noconftest tests/test_fx_end_to_end.py

# 相关 FX focused suites（隔离数据库）
python -m pytest -q --noconftest \
  tests/test_fx_amm.py tests/test_fx_models.py tests/test_fx_migration.py \
  tests/test_fx_trading_service.py tests/test_fx_api.py \
  tests/test_fx_market_data.py tests/test_fx_stream.py tests/test_fx_stream_chart.py \
  tests/test_fx_engine.py tests/test_fx_events.py tests/test_fx_scheduler.py \
  tests/test_admin_fx.py tests/test_fx_valuation.py \
  tests/test_fx_audit_replay.py tests/test_fx_season_reset.py

# 事件 / 引擎 / 管理专项（Task 9 的日期序列化与测试隔离修复后全绿）
python -m pytest -q --noconftest tests/test_fx_events.py tests/test_fx_engine.py tests/test_admin_fx.py

# 工作区空白/冲突检查
git diff --check

# 前端（若改了 Task 8/9 文件）
cd ../thccb-frontend && npm run type-check && npm run test:unit && npm run build
```

本轮 bounded fix 的 focused 验证（`TMPDIR=/dev/shm`，`--noconftest`，逐文件运行避免退出期挂起）：

```bash
cd backend
TMPDIR=/dev/shm python -m pytest -q --noconftest \
  tests/test_fx_trading_service.py tests/test_fx_engine.py tests/test_fx_scheduler.py \
  tests/test_admin_fx.py tests/test_fx_api.py tests/test_fx_end_to_end.py
```

覆盖：I1 snapshot 价格单位/非负 spread、I2 `fx_default_price_move_limit` 行为、
I3 事件路径 pair→event 锁序、I4 管理端只读 pairs+treasury、I5 玩家 wallet/个人成交、
I6 前端 `summary.fx_mtm` 净值/rank 接线。`test_fx_trading_service.py` 已把提交后广播的
session factory 指向隔离库，不再有默认库缺表失败。

`test_fx_end_to_end.py` 覆盖：admin/player 权限、TOS、bot 禁单、债务买/卖、quote/trade、
过期 `min_out`、幂等重放、公开字段白名单、SSE 白名单、gate 关闭、分币种守恒、
注/撤资，以及赛季重置清理与兑换保留。

---

## 10. 生产开市前的模拟校准

在真实开市前，用确定性随机源在隔离数据库上跑一遍引擎，确认参数与预算：

```bash
cd backend
python -m pytest -q --noconftest tests/test_fx_engine.py tests/test_fx_scheduler.py
```

人工检查清单：

- [ ] `fx_enabled=false`（默认即关闭）。
- [ ] 初始目标价 `target_price` 在 `[target_min, target_max]` 且等于 `initial_price`；
      第一版只允许 `initial_price` 附近 ±（0.5x–2x）的安全夹取。
- [ ] 费率、6 位小数、每日预算与事件预算符合运营计划。
- [ ] 用一个 `shock_ratio=0.01`、`first_reaction_ratio=0.25` 的事件验证：
      首轮 / 后续 tick 朝保存目标推进且不超调，`spent` 只累计本事件。
- [ ] 确认 gate 关闭时引擎完全静默（无噪声、无干预）。
- [ ] 备份 + `season_reset preview` 可用。

---

## 11. 已修复的审计日期序列化缺陷（Task 9 发现）

- **现象**：`audit_service.record_fx_trade` 把 `treasury.spend_date`（`datetime.date`）
  放进 JSON payload，而 `audit_service._j` 只把 `datetime` 转 ISO 字符串、不处理 `date`。
- **后果（修复前）**：任何系统成交审计 flush 抛
  `StatementError: Object of type date is not JSON serializable`。
  `publish_event` 因此把事件置为 `cancelled`；`engine.tick` 的干预同样失败；
  一旦 `spend_date` 被写成 date，之后的玩家成交审计也会失败。
- **修复（已授权的最小改动，仅 `backend/app/services/audit_service.py`）**：
  `_j` 在 `datetime` 分支之后增加 `date` 分支，返回 `v.isoformat()`。
  因为 `datetime` 是 `date` 子类，顺序必须先 `datetime` 后 `date`；
  该改动是严格新增能力，不影响任何原本可用的 JSON 值。
- **验证（Task 9 当时）**：`test_fx_end_to_end.py`（10 passed，含 scheduled event publish/
  recovery/per-event spent）、`test_fx_events.py`（8 passed）、`test_fx_engine.py`
  （14 passed）、`test_admin_fx.py`（6 passed）全部通过。该缺陷当前已修复并有回归测试，
  不再是待修阻断项；后续 count 变化见 11.3。

### 11.1 whole-branch fix wave（I1–I6）

follow-up 一轮 bounded fix 处理了 whole-branch review 的 6 个 Important：

- **I1 价格口径**：`get_public_snapshot` 的 `buy_price`/`sell_price`/`spread` 统一为
  gold-per-foreign：`buy_price = 金入 / 外币出`（ask）、`sell_price = 金出 / 外币入`（bid）、
  `spread = buy_price − sell_price ≥ 0`；前端标签同步为金/外币口径。
- **I2 engine 配置**：正常干预每 tick 实时读取 `fx_default_price_move_limit` 作为价格移动
  与系统下单上限；事件路径 `max_ratio`（无上限）+ budget 语义保持不变。
- **I3 锁序**：`schedule_event`/`publish_event` 改为先锁 `fx_pair` 再锁 `fx_event`，
  与 `engine.tick` 一致；补了基于 ORM `do_orm_execute` 的锁顺序行为测试
  （SQLite 只能证明语句顺序，不能证明并发无死锁）。
- **I4 管理读取**：新增 `GET /api/v1/admin/fx/pairs`（`FxPairAdminDetail`，含草稿、
  treasury `gold_balance`/`foreign_balance`/`daily_spend`），`FxManage.vue` 用它初始化与刷新。
- **I5 玩家读取**：新增 `GET /api/v1/fx/pairs/{id}/wallet` 与 `.../my-trades`；
  `Fx.vue` 展示真实持仓与个人成交历史，公开 schema 仍不含任何隐藏字段。
- **I6 前端净值**：`stores/user.ts` 把 `summary.fx_mtm` 并入 `netWorth`/`unrealizedPnl`/
  `rankTitle`；`netWorthLcv` 与服务端 LCV margin 口径仍不含 FX。

### 11.2 仍未覆盖的环境限制

- **PostgreSQL 并发/锁**：所有隔离测试基于 SQLite；行锁与 `pair→event` 锁序只做了代码层
  与 ORM 语句顺序验证，未在真实 PostgreSQL 上做并发死锁/隔离级验证。
- **APScheduler 真实重启**：E2E 直接调用 `scheduler._tick_safe()` 用数据库状态模拟恢复，
  没有拉起真实 APScheduler 进程，也未验证多进程 `max_instances` 行为。
- **真实 SSE 网络生命周期**：E2E 直接驱动 `fx_stream.stream` 生成器读取首帧，
  未做浏览器断线/重连/代理 buffering/IP 限速联调。
- **浏览器交互**：按仓库偏好不做自动浏览器点击/填写；前端只跑 type-check / 单测 / build，
  UI 变更未做浏览器实测。
- **生产序列**：PostgreSQL 自增序列跨赛季行为无法用 SQLite 复现，赛季重置只验证了 SQLite
  rowid 不复用。
- `test_fx_trading_service.py` 的隔离缺口已在 final review fix 中修复（提交后广播指向
  隔离 session factory），`--noconftest` 下不再报 `no such table: fx_pair`。
- 未运行仓库全量 `pytest -q`（含 conftest）；FX focused 用 `TMPDIR=/dev/shm --noconftest`
  隔离运行，不宣称为 full pytest green。

### 11.3 minor fix（M1/M2/M3）

whole-branch review 遗留的 3 个 Minor（见 `fix-wave-review.md`）已在本轮处理：

- **M1 公开 source**：`FxTradePublic` 不再包含 `source`；玩家 `GET /pairs/{id}/trades`、
  `POST .../trades`、`GET .../my-trades` 都不再返回 `system_event` / `system_noise` /
  `system_target`。管理员 `GET /admin/fx/pairs/{id}/interventions` 的 `Intervention`
  schema 仍保留 `source`。新增真实 ASGI 行为测试：写入 `system_target` 成交后，公开 feed
  与个人历史都不含 `source`，而管理端可见 `system_target`。
- **M2 费率/病态数据安全**：管理员 `PairCreate` / `PairPatch` 费率校验收紧为
  `0 <= rate < 1`（`0.99999999` 仍接受，精确 `1` 拒绝）；AMM `_fee` 同步拒绝 `rate >= 1`；
  public snapshot/quote 对已存在的病态 pair（费率 1、储备小到单笔产出向下量化为 0）返回
  带原因的可操作 **HTTP 422**，不再 500。新增真实测试覆盖两条路径。
- **M3 文档同步**：本文件、`task-9-report.md`、`fix-wave-report.md` 的历史计数与
  date-blocker 措辞已对齐；保留 PG/APScheduler/SSE/浏览器真实限制。

当前 FX focused 合计 **111 passed**（fix wave 107 + 本轮新增 4）；
前端 `type-check`、`test:unit`、`build` 结果见 `minor-fix-report.md`。

---

## 12. 相关文档

- API 细节：`docs/api.md` 第 12 节「幻想外汇 (FX)」。
- 部署 / 迁移：`docs/deploy.md`、`docs/migrations.md`。
- 净值双口径：`docs/holdings-value-semantics.md`。
- 赛季重置范围：`docs/season-reset-2026-09-27.md`。
- Task 9 验收报告：`.superpowers/sdd/2026-09-28-fx-market/task-9-report.md`。
- whole-branch fix wave 报告：`.superpowers/sdd/2026-09-28-fx-market/fix-wave-report.md`。
- 本轮 minor fix 报告：`.superpowers/sdd/2026-09-28-fx-market/minor-fix-report.md`。

## 统一信贷接入（2026-10，需独立启用）

启用统一信贷后，多 FX 钱包与 LMSR 共用单一金圆券贷款、初始率和维持率。
账面市值仍按现价展示；授信与强平改用各 pair AMM 真实卖出净额，扣各自卖出费率。
无债也不能把账面市值当成真实清算价值。默认 paused 继续全停；显式 reduce-only 允许卖出和强平，但不允许买入或系统干预。
每次定时扫描最多为同一玩家卖一个组的配置比例（`liquidation_partial_pct`，默认 10%；清算净值不大于零时卖该组全部），可在不同扫描轮次处理不同产品；没有行情或成交额外触发。
首期最多 3 个 trading pair，账户页按币种列出钱包。

20 倍杠杆需要运营显式设定，维持率建议 `0.04`；迁移本身保留旧授信水平。
存在 FX 抵押债务时不得回退到只处理 LMSR 的旧强平。
发布门槛、费率公示、启动缓存和完整备份回退流程见 [统一信贷操作手册](unified-credit-risk-2026-10.md)。
本节是启用后的规则；此前描述的 FX 不计入贷款抵押仅适用于统一信贷关闭时。
