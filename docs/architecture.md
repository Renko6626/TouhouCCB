# TouhouCCB 系统架构总览

> 面向贡献者的中文架构速查文档。更深层专题见同目录下 `api.md` / `migrations.md` / `holdings-value-semantics.md` / `development.md`。

---

## 1. 仓库地图

| 目录 | 职责 |
|---|---|
| `backend/` | FastAPI 后端：HTTP API、ORM 模型、业务服务、定时 sweep、SQLAdmin 管理面板 |
| `thccb-frontend/` | Vue 3 + Vite 前端 SPA：页面、组件、Pinia store、SSE 客户端 |
| `quant/` | 独立 Python 量化 bot（`thccb_quant`），通过公开 REST API 自动交易 |
| `docs/` | 专题设计文档（api、migrations、style、holdings-value-semantics 等） |
| `deploy/` | Nginx 配置（`nginx.conf`）与部署脚本（`deploy.sh`） |
| `loadtest/` | k6 负载测试场景与结果 JSON |

顶层 `docker-compose.yml` 编排两个 service：`postgres`（Postgres 16）和 `backend`（FastAPI 容器，监听 `127.0.0.1:8004`）。前端由独立构建产物静态托管，通过 Nginx 反向代理到后端。

---

## 2. 后端分层

### 2.1 层次概览

```
thccb-backend
├── app/api/v1/        HTTP 路由层（FastAPI Router）
├── app/core/          基础设施层
├── app/models/        数据库模型层（SQLModel / SQLAlchemy ORM）
├── app/schemas/       Pydantic 数据契约层（请求体 & 响应体）
└── app/services/      业务逻辑层
```

**`app/api/v1/`**：纯 HTTP 层——声明路由、鉴权依赖、限速注解、参数校验，把请求委托给 services 处理并返回响应。不包含业务计算。

**`app/core/`**：基础设施——`config.py`（Settings + 环境变量）、`database.py`（async engine + session 工厂 + `managed_transaction`）、`oidc.py`（轻量 OIDC 客户端，通过 `.well-known` 自动发现 Casdoor 端点）、`users.py`（本站 HS256 JWT 签发 & 验证）、`admin.py`（SQLAdmin 管理面板，JWT 鉴权，超管专用）。

**`app/models/`**：SQLModel（SQLAlchemy + Pydantic 合体）定义 DB 表结构。核心表在 `base.py`；`ledger.py`、`audit.py`、`redemption.py`、`title.py` 各管自己的附属表（注册到 SQLModel.metadata 后 alembic 自动迁移）。**重要**：`User.positions`、`User.transactions`、`Outcome.positions`、`Outcome.transactions` 四个反向集合均配置 `lazy="raise_on_sql"`，未显式 `selectinload` 则直接访问会抛异常——这是热路径性能护栏，详见 `CONTRIBUTING.md` 的「ORM 查询守则」。

**`app/schemas/`**：Pydantic 模型定义 API 的入参/出参契约（与 ORM 模型解耦）。Money 字段必须使用带 `serialize→float` 的 `Money` 类型，否则 `Decimal` 会以字符串输出触发前端 `TypeError`（见 `docs/` 内 schema-conventions）。

**`app/services/`**：所有业务逻辑收口于此，各模块职责如下：

| 模块 | 职责 |
|---|---|
| `lmsr.py` | LMSR 做市商定价核心：`calculate_lmsr_cost`、`get_current_price`、`calculate_lmsr_with_prices`；内部用 float 计算，边界处通过 `quantize_cost`/`quantize_price` 转 Decimal（6/8 位精度） |
| `realtime.py` | 内存 pubsub broker（`MarketEventBroker`）：SSE 订阅/取消/发布，per-market 序号递增，慢消费者踢出；`IpConcurrencyLimiter` 控制同 IP 并发 SSE 数 |
| `market_locks.py` | 并发锁助手：`lock_market` / `lock_user` / `lock_outcomes_for_market`，通过 `SELECT FOR UPDATE` 实现数据库行级锁；锁顺序约定：market → user → outcomes，违者引发死锁 |
| `wealth.py` | 持仓估值双口径：`compute_users_holdings_value`（LCV，含滑点+fee，用于强平/风控）和 `compute_users_holdings_value_mtm`（MTM，按瞬时价×数量，用于 UI 显示） |
| `wealth_stats.py` | 平台净值分布统计（纯 math，无 DB 依赖）：均值、分位数、基尼系数、按称号档位分桶 |
| `audit_service.py` | 审计事件写入助手：`record` / `record_trade` / 快照构造；与业务变更同事务 |
| `audit_replay.py` | 审计事件折叠（T 时刻状态）、独立增量校验、与线上表比对；`scripts/audit_verify.py` / `audit_export.py` 的核心 |
| `ledger_service.py` | 资金账本写入助手：`record_entry` 构造 `LedgerEntry`，与资金变动同事务提交，调用方负责 user 字段已反映操作后的最终值 |
| `liquidation_service.py` | 强制平仓原子操作：按 LCV 判定保证金不足后逐持仓卖出，写 `LiquidationEvent` 快照和 LIQUIDATE 类型 `Transaction` |
| `liquidation_sweep.py` | APScheduler 定时扫描全体借贷用户的保证金水位，触发 `liquidation_service.liquidate_user` |
| `loan_service.py` | 借款业务原子操作：复利结息（`accrue_interest`）、借款/还款资金变动，写 `LedgerEntry` |
| `loan_sweep.py` | APScheduler 定时结息：扫全体 `debt > 0` 用户，按 `(1+r)^(Δt/天)` 按日复利增加 debt（分段精确可合成，与 tick 间隔无关；增量不足 1 LSB 不推进锚点） |
| `title_service.py` | 称号目录 admin 端 CRUD + 状态查询 |
| `title_code_service.py` | 称号激活码：batch 创建/列表/CSV 解析、用户兑换激活码 |
| `market_title_gating.py` | 市场准入 title 门槛（ANY-of 语义），仅 gate buy，sell/quote/settle 不调用 |
| `redemption.py` | 实物兑换码：CSV 批量解析、购买事务（单用户单批次上限 5 张）、库存查询 |
| `danmuku.py` | 弹幕系统兑换：HMAC 激活码生成（base64url + HMAC-SHA256），站内 cash → yuan/huo 汇率 1:1 |
| `rank.py` | 净值称号映射（`rank_title`）：按 net_worth 区间返回东方主题称号（"人类灵(已爆仓)" → "ZUN"） |
| `candle_writer.py` | OHLCV K 线物化：每笔 buy/sell 在事务内同步 UPSERT 四档（10s/1m/15m/1h）candle 行 |
| `anti_bot.py` | L2 HMAC client_token 验证（30s 时间窗），buy 请求携带前端生成的 HMAC 才能通过 |
| `bot_detection.py` | L4 行为监控（APScheduler 每 30min 扫近 2h 交易），触发信号写 `BotSuspicion` |
| `site_config.py` | 站点运行时 key-value 配置读写（借款利率、强平阈值等，超管可热改） |

### 2.2 lifespan 启动顺序

`main.py` 的 `lifespan` 依次执行：

1. `init_db()` — 建表（SQLModel.metadata.create_all）
2. `auto_migrate()` — LoanV1 幂等补列 + 种默认 site_config
3. `setup_admin(app, engine)` — 挂载 SQLAdmin 管理面板
4. `_resync_recent_candles()` — 清重建近 1h 的 candle，覆盖 migration→新代码上线间的 race window
5. 启动三个 APScheduler：`loan_sweep` → `liquidation_sweep` → `bot_detection`
6. 优雅停机时逆序停止各 scheduler，最后 `engine.dispose()` 释放连接池

### 2.3 已注册的 API 路由

| 前缀 | 模块 | 限速（nginx） |
|---|---|---|
| `/api/v1/auth` | `auth.py` | 5 r/s |
| `/api/v1/user` | `user.py` | — |
| `/api/v1/market` | `market.py` | buy/sell/quote 10 r/s |
| `/api/v1/chart` | `chart.py` | — |
| `/api/v1/stream` | `stream.py`（SSE） | — |
| `/api/v1/loan` | `loan.py` | — |
| `/api/v1/redemption` | `redemption.py` | — |
| `/api/v1/danmuku` | `danmuku.py` | — |
| `/api/v1/title` | `title.py` | — |
| `/api/v1/admin` | `site_config.py` + `admin_redemption.py` + `admin_stats.py` + `admin_liquidation.py` + `admin_bot.py` + `admin_title.py` | 2 r/s |

SQLAdmin 管理面板挂载在 `/api/v1/admin`（由 `core/admin.py` 的 `setup_admin` 完成），通过粘贴本站 access token 登录，只有 `is_superuser=True` 的用户可用。

---

## 3. 前端结构

前端位于 `thccb-frontend/src/`，Vue 3 + TypeScript + UnoCSS，状态管理用 Pinia，路由用 Vue Router 4。

| 目录 | 职责 |
|---|---|
| `pages/` | 路由页面组件（按功能域分子目录：home / market / user / admin / auth / loan / redemption / danmuku / redeem） |
| `components/` | 复用组件（chart / global / home / layout / legal / market / title / user 分类） |
| `composables/` | 组合式函数；`useMarketRealtime.ts` 封装 SSE 订阅、per-market seq 跟踪、gap 检测、`pricesByOutcome` 反应式价格 map |
| `stores/` | Pinia 状态仓库（auth / market / user / loan / redemption / notification / title），持久化业务状态并暴露 actions 调 API |
| `api/` | 后端 HTTP 接口封装（axios / fetch 调用）；`stream.ts` 封装 `EventSource`（`MarketStream` 类） |
| `router/` | Vue Router 路由表（`routes.ts`）+ 全局导航守卫（`guards.ts`，鉴权重定向） |
| `types/` | TypeScript 接口定义（api / chart / stream / trade / market / user 等）；前端类型来源，与后端 Pydantic schema 对应 |
| `utils/` | 工具函数（`formatter.ts` 数值/时间格式化、`palette.ts` 涨绿跌红配色、`errors.ts` 错误处理、`clientToken.ts` HMAC 生成、`validation.ts`） |

---

## 4. 核心概念

### 4.1 LMSR 做市商定价

本站采用 **对数做市规则（LMSR，Logarithmic Market Scoring Rule）** 定价。系统总成本函数：

```
C(q) = b · ln(Σ exp(q_i / b))
```

其中 `q_i` 是选项 i 的累计份额，`b` 是流动性参数（每个市场独立设置，`Market.liquidity_b`）。

**关键特性**：某一选项被买入/卖出时，全市场所有选项的瞬时价格均会联动变化，因为价格 `P_i = exp(q_i/b) / Σ exp(q_j/b)` 取决于所有选项的份额。图表必须按全市场逐笔重放，不能只看目标选项的成交数据。

实现见 `backend/app/services/lmsr.py`。内部计算用 float（math 运算），边界处量化为 `Decimal`（资金/份额 6 位，价格 8 位）。

单笔交易对价格的影响可达 ±10%，远超传统交易所，quant 策略参数（滑点上限/网格步长）需据此校准，详见 `docs/` 内 LMSR 相关记录。

### 4.2 SSE 实时广播

- **服务端**：`services/realtime.py` 中的 `MarketEventBroker`（进程内 pubsub，单进程适用；多进程部署需引入外部 broker）维护 per-market 订阅者集合和单调递增序号（`seq`）。buy/sell 成交后，`market.py` 调用 `BROKER.publish(market_id, "trade", data)` 广播。
- **SSE 端点**：`GET /api/v1/stream/market/{market_id}`（`api/v1/stream.py`）。连接建立时先发 `snapshot`（当前市场状态 + 所有选项现价），之后从 broker 队列拉事件推送；每 25s 发 `ping` 心跳保活；单连接最长 1 小时（`MAX_SSE_DURATION=3600`）。连接期间不占用 DB 连接（snapshot 完成后立即归还）。
- **前端**：`api/stream.ts` 中的 `MarketStream` 封装 `EventSource`；`composables/useMarketRealtime.ts` 在其上叠加 seq gap 检测（断线期间漏事件触发 `gapToken` 递增，图表 watch 它做静默重拉）和 `pricesByOutcome` 反应式价格 map。
- **推送内容**：价格变动（`trade` 事件，含 `market_prices_post` 全市场价格快照）、市场状态变更（`market_status`，如 HALT/SETTLED）、弹幕消息。

### 4.3 认证体系

认证采用 **Casdoor SSO（OAuth2 / OIDC）**，流程：

1. 前端调 `POST /api/v1/auth/login-start`，后端生成随机 state + nonce 写入 HttpOnly cookie，返回同值供前端拼 Casdoor authorize URL。
2. 用户在 Casdoor 登录后重定向回前端，前端将 code + state 提交给 `POST /api/v1/auth/callback`。
3. 后端通过 `.well-known/openid-configuration` 发现端点（`core/oidc.py`），用 code 换取 id_token，JWKS 验证签名 + iss + aud + nonce。
4. 首次登录自动创建本站用户记录；**当时全站用户数为 0（即第一个登录者）自动获得 `is_superuser=True`，没有独立的管理员创建接口**（见 `auth.py` L165–L185）。
5. 后端签发本站 HS256 JWT（access token 短期 + refresh token 长期），前端后续请求带 `Authorization: Bearer <token>`，`core/users.py` 验证并从 DB 加载用户。

### 4.4 持仓估值双口径

持仓估值存在两套语义，**绝对不能交叉使用**：

| 口径 | 全称 | 计算方式 | HALT 持仓 | 使用场景 |
|---|---|---|---|---|
| **MTM** | Mark-to-Market | 瞬时价 × 持仓数量（`Outcome` LMSR 边际价 × `Position.amount`） | **计入** | UI 显示净值、排行榜、`/user/summary` |
| **LCV** | Liquidation Cash Value | LMSR cost diff（全部卖出实得），含卖出滑点 + fee | **不计入** | 强平判定、保证金计算、借款额度上限 |

两者对大持仓用户差距可达 20%+（`b=100` 量级时单笔滑点约 10%）。实现见 `backend/app/services/wealth.py`；完整设计见 `docs/holdings-value-semantics.md`。

### 4.5 资金账本

`LedgerEntry`（`models/ledger.py`）记录 cash/debt 发生变动的流水，用于事后追溯：

- **覆盖场景**：`borrow`（借款）、`repay`（还款）、`admin_adjust_cash`（调整现金）、`admin_force_loan`（强制放贷）、`admin_forgive_debt`（免债）、`admin_amnesty`（大赦天下：批量清债 + 现金还原，一人一条，cash_delta 与 debt_delta 同时非零）
- **不覆盖**：buy/sell/结算/强平/兑换/弹幕（各有自己的 `Transaction` / `LiquidationEvent` / 兑换 Event 表记录）；利息（可由公式 + 快照重算）
- 写入由 `services/ledger_service.py` 的 `record_entry` 完成，与资金变动**同一事务**提交，调用方须确保 `user.cash`/`user.debt` 已更新为操作后的值

### 4.5.1 审计事件流（audit_event）

`AuditEvent`（`models/audit.py`）是覆盖**全部**改钱 / 改仓 / 改市场 / 改配置路径的全序事件流，每条带操作后快照（`user_after` / `position_after` / `market_after` 全市场 q 向量），供事后审计、时间点回溯与离线回测。`LedgerEntry` / `Transaction` / `LiquidationEvent` 仍是各自业务的明细表，`audit_event` 通过 `ref_table/ref_id` 指回它们。**新增任何改 `user.cash/debt`、`position.amount`、`outcome.total_shares`、`market.status`、`site_config` 的写路径，必须在同事务内调用 `audit_service.record(...)`**，否则 `scripts/audit_verify.py` 会报线上表与事件流不一致。详见 `docs/audit-events.md`。

### 4.6 Decimal 精度约定

| 数值类型 | Decimal 精度 | DB 类型 |
|---|---|---|
| 资金（cash / debt / cost / gross / fee） | 6 位（`COST_QUANT = 0.000001`） | `Numeric(16, 6)` |
| 份额（shares / amount） | 6 位 | `Numeric(16, 6)` |
| 价格（price / market_price） | 8 位（`PRICE_QUANT = 0.00000001`） | `Numeric(16, 8)` |

前端不能用 `Number()` 直接转 API 返回的价格字段（会丢精度），应使用 `toFixed` 配合 `Money` 序列化类型（序列化为 float）。

---

## 5. 典型数据流：用户买入

```
前端 TradePanel
  │  POST /api/v1/market/buy  (带 X-Client-Token HMAC + Authorization JWT)
  ▼
market.py (buy 路由)
  ├─ anti_bot.verify_client_token()         // L2 HMAC 校验
  ├─ assert_user_can_trade_market()         // 市场 title 门槛
  ├─ 限速：nginx 10 r/s 限速在上游已拦截
  ├─ managed_transaction(db)               // 数据库事务
  │    ├─ market_locks.lock_market()        // SELECT FOR UPDATE market
  │    ├─ market_locks.lock_user()          // SELECT FOR UPDATE user
  │    ├─ market_locks.lock_outcomes_for_market()  // SELECT FOR UPDATE outcomes
  │    ├─ lmsr.calculate_lmsr_cost()        // 交易前总成本
  │    ├─ lmsr.calculate_lmsr_cost()        // 交易后总成本（含新份额）
  │    ├─ 滑点保护：(cost_after - cost_before) vs max_cost
  │    ├─ 更新 outcome.total_shares + user.cash
  │    ├─ 写 Transaction（buy 类型）
  │    ├─ 更新 Position（amount / cost_basis）
  │    └─ candle_writer.upsert_candles()    // UPSERT 10s/1m/15m/1h 四档 K 线
  │
  ├─ BROKER.publish(market_id, "trade", {...})  // 事务提交后广播
  │    └─ 所有 /stream/market/{id} SSE 连接接收推送
  │
  └─ 返回 TradeResponse（cost / shares / pre_price / post_prices）

前端接收 SSE trade 事件
  ├─ useMarketRealtime 更新 pricesByOutcome（用 market_prices_post 全量刷新）
  ├─ market store 追加到 marketTrades 列表
  └─ PriceChart / CandleChart watch latestTrade 增量 append 数据点
```

---

## 6. 关键限制与设计决策

- **单进程 pubsub**：`MarketEventBroker` 是进程内对象，多 uvicorn worker 会各自独立，订阅者只能收到同 worker 处理的交易事件。当前单 worker 部署无问题；横向扩展需引入 Redis pub/sub。
- **Schema 迁移**：加列/改字段必须走 `alembic revision --autogenerate` 流程，不允许裸改模型；见 `docs/migrations.md`。
- **交易手续费**：`SELL_FEE_RATE = Decimal("0")`，当前硬编码为 0，待后续决策后才开启。
- **初始余额**：新用户注册获得 `settings.INITIAL_BALANCE` 的初始现金（由 `.env` 配置，`.env.example` 示例为 100）。注：`User.cash` 模型层另有 `default=500`，与 `INITIAL_BALANCE` 存在双重定义不一致（已知待清理项）。
- **强平死锁处理**：`liquidation_sweep` 的锁顺序（user → outcomes）与 `market.py` hot path（market → user → outcomes）存在潜在环形等待；sweep 层捕获 `DeadlockError` 后跳过该用户本轮，下次 sweep 再试。
