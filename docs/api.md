# 东方炒炒币 API 文档

**Base URL**: `/api/v1`
**认证方式**: JWT Bearer Token（通过 Casdoor SSO 获取）

---

## 1. 认证 (Auth)

### POST `/auth/login-start` — 生成 OAuth state/nonce

防 CSRF。后端生成 `state` / `nonce` 并写入 HttpOnly cookie，前端拿返回值拼到 Casdoor authorize URL。

### POST `/auth/callback` — SSO 登录

前端把 Casdoor 返回的 authorization code 发过来，换取本站 JWT。
第一个注册的用户自动成为管理员。

**请求体**:
```json
{
  "code": "authorization_code_from_casdoor",
  "state": "csrf_state_string",
  "redirect_uri": "https://你的域名/auth/callback"
}
```

**响应**:
```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "bearer"
}
```

### POST `/auth/refresh` — 刷新 Token

```json
{ "refresh_token": "eyJ..." }
```

### GET `/auth/me` — 当前用户信息

**响应**:
```json
{
  "id": 1,
  "username": "reimu",
  "email": "reimu@gensokyo.jp",
  "is_superuser": true,
  "is_active": true,
  "cash": 100.00,
  "debt": 0.00,
  "tos_accepted_at": "2026-04-15T12:00:00Z",
  "equipped_title_id": 3
}
```

`tos_accepted_at` 为 `null` 表示尚未同意用户协议；`equipped_title_id` 为 `null` 表示未佩戴称号。

### POST `/auth/accept-tos` — 接受用户协议（幂等）

标记当前用户已同意条款，返回 `{ "tos_accepted_at": "..." }`。已同意者再调返回原时间戳。

---

## 2. 用户 (User)

### GET `/user/summary` — 资产概览

同时返回两套口径（详见 `docs/holdings-value-semantics.md`）：
- 无后缀字段（`holdings_value` / `net_worth` / `unrealized_pnl`）= **MTM 口径**（瞬时市价 × 数量，不含滑点），用于展示与排名。
- `*_liquidation` 字段 = **LCV 口径**（LMSR 全部卖出清算价，含滑点 + 扣 sell_fee），强平 / 借款额度按此算，通常 ≤ MTM。
- `fx_mtm` / `fx_cost_basis` / `fx_unrealized_pnl` = FX 外币持仓的展示估值（按最新 AMM 边际价 MTM）。
  FX **只进入展示净值 / 排行榜 / rank**，不计入 LCV、借款额度或强平抵押物。

```json
{
  "cash": 150.25,
  "debt": 0.00,
  "holdings_value": 92.10,
  "holdings_value_liquidation": 89.75,
  "total_cost_basis": 70.00,
  "unrealized_pnl": 22.10,
  "unrealized_pnl_liquidation": 19.75,
  "net_worth": 242.35,
  "net_worth_liquidation": 240.00,
  "fx_mtm": 31.20,
  "fx_cost_basis": 30.00,
  "fx_unrealized_pnl": 1.20,
  "rank": "人里居民",
  "margin_ratio": null,
  "margin_status": "healthy",
  "equipped_title": null,
  "all_titles": []
}
```

### GET `/user/holdings` — 持仓明细

```json
[
  {
    "market_id": 1,
    "market_title": "灵梦 vs 魔理沙 谁会赢？",
    "outcome_id": 1,
    "outcome_label": "博丽灵梦",
    "amount": 50.50,
    "cost_basis": 25.00,
    "avg_price": 0.4950,
    "current_price": 0.5200,
    "market_value": 24.88,
    "unrealized_pnl": -0.12
  }
]
```

### GET `/user/transactions` — 交易历史（最近 50 条）

```json
[
  {
    "id": 123,
    "outcome_id": 1,
    "type": "buy",
    "shares": 10.00,
    "price": 0.4500,
    "gross": 4.50,
    "fee": 0.00,
    "cost": 4.50,
    "timestamp": "2026-04-15T12:00:00Z"
  }
]
```

`type` 值: `buy` | `sell` | `settle` | `settle_lose`

### PATCH `/user/me/username` — 修改昵称

> 财富 / 消费排行榜见 §3 市场的 `GET /market/leaderboard`；我的称号见 §9 `GET /title/me`。

> 管理员对用户的操作（列表 / 快照 / 调现金 / 放贷免债 / 封禁 / 管理员 / 批量）已统一到 §10 的 `/admin/users`。

---

## 3. 市场 (Market)

### GET `/market/list` — 市场列表

**查询参数**:
- `keyword` — 按标题搜索
- `tag` — 按标签过滤
- `include_halt` — 包含熔断市场 (bool)
- `include_settled` — 包含已结算市场 (bool)

### GET `/market/{id}` — 市场详情

返回市场信息 + 所有选项的当前价格、24h 涨跌幅。

### POST `/market/buy` — 买入

```json
{ "outcome_id": 1, "shares": 10 }
```

`shares` 类型为 Decimal。LMSR 非线性定价，实际成本高于 瞬时价格 x 份额。

### POST `/market/sell` — 卖出

同 buy 格式。卖出时会按比例减少 cost_basis。

### POST `/market/quote` — 报价预估（不成交）

```json
{ "outcome_id": 1, "shares": 10, "side": "buy" }
```

返回 avg_price、gross、fee、net、交易后各选项价格。

### GET `/market/{id}/trades` — 单个市场最近成交记录

### GET `/market/recent-trades` — 跨市场最近成交（首页用）

### GET `/market/movers` — 涨跌榜（按时间窗口）

**查询参数**: `window`（如 `1h` / `24h`）、`limit`

### GET `/market/leaderboard` — 财富 / 消费排行榜

**查询参数**: `limit`（默认 20）、`mode`（`net_worth` = 按 cash-debt+持仓 / `spending` = 按兑换消费-当前债务）。按 MTM 口径排序，返回含每位用户的自动判定称号（见规则书）。

### POST `/market/create` — 创建市场（仅管理员）

```json
{
  "title": "灵梦 vs 魔理沙",
  "description": "谁会赢？",
  "liquidity_b": 100,
  "outcomes": ["博丽灵梦", "雾雨魔理沙"],
  "closes_at": "2026-05-01T00:00:00Z",
  "tags": ["东方", "对战"]
}
```

### POST `/market/{id}/close` — 熔断 / 暂停交易（仅管理员）

无请求体。市场进入 HALT 状态（**不结算**），买卖按钮变灰。

### POST `/market/{id}/resume` — 恢复交易（仅管理员）

### POST `/market/{id}/resolve` — 结算市场（仅管理员）

```json
{ "winning_outcome_id": 1, "payout_per_share": 1.0 }
```

`payout_per_share` 默认 `1.0`（范围 0–1）。赢家仓位按该比例兑付现金（创建 `settle` 交易），亏损仓位份额归零（创建 `settle_lose` 交易），结算后清理 Position。已结算市场再调返回 409。

---

## 4. 图表 (Chart)

### 架构说明

LMSR 交易任何选项会改变**所有**选项的价格。图表 API 不是只查目标选项的交易记录，而是查**整个市场所有交易**，逐笔重放 shares 状态，计算目标选项在每笔交易后的瞬时价格。

> 注：旧的 `GET /chart/price` 已下线，价格走势统一由 `/chart/candles` 提供（取较小 `interval` 如 `10s`，用 `c` 字段即得逐点价格曲线）。

### GET `/chart/candles` — K 线（OHLCV）

**参数**:
- `outcome_id`（必填）
- `interval`：`10s` / `30s` / `1m` / `5m` / `15m` / `1h` / `1d`，默认 `1m`
- `from_ts`、`to_ts`（必填，ISO 时间）
- `fill`：默认 `false`，为 `true` 时空桶用上一根 close 补平
- `limit`：默认 `5000`，范围 1–20000（按预计 K 线根数上限校验）

返回 `[{ t, o, h, l, c, v, n }, ...]`

- `o` (open) = bucket 内第一笔交易前的市场价；`c` (close) = bucket 内最后一笔交易后的市场价
- 数据直接读 `outcome_candle` 物化表（物化缓存，告别 5000 笔逐笔重放上限）

---

## 5. 实时推送 (Stream)

### GET `/stream/market/{id}` — SSE

事件类型:
- `snapshot` — 市场当前状态快照
- `trade` — 新成交
- `market_status` — 状态变更（熔断/恢复/结算）
- `ping` — 心跳（30s）

---

## 6. 借贷 (Loan)

保证金交易，详见规则书附录 B 与 `docs/holdings-value-semantics.md`。

### GET `/loan/quota` — 借款额度与杠杆状态

```json
{
  "enabled": true,
  "cash": 100.00,
  "debt": 0.00,
  "net_worth": 100.00,
  "leverage_k": 5,
  "daily_rate": 0.01,
  "max_borrow": 400.00,
  "last_accrued_at": null
}
```

> `net_worth` 为 **LCV 口径**（含 LMSR 滑点 + 扣 sell_fee），比 `/user/summary.net_worth`（MTM）更保守，借款额度按此算，避免虚高估值过度杠杆。

### POST `/loan/borrow` — 借款

```json
{ "amount": 100.00 }
```
响应：`{ "cash", "debt", "max_borrow", "effective" }`（`effective` 仅 repay 有意义）。

### POST `/loan/repay` — 还款

同 borrow 格式。`amount` 超过真实债务或现金时，服务层会封顶，实际生效值见响应 `effective`。

### POST `/loan/repay-all` — 按最新负债一键还款

需要登录，无请求体。后端在用户行锁内计息，再扣减 `min(最新负债, 最新现金)`；现金充足时负债精确清零，现金不足时返回剩余负债。响应与 `/loan/repay` 相同，`effective` 为实际扣款。已无负债时不扣款；有负债但无现金时返回 400。

### GET `/loan/liquidation-policy` — 强平规则（公开只读）

### GET `/loan/recent-liquidations` — 最近强平事件（公开只读，脱敏）

---

## 7. 兑换码 (Redemption)

用站内现金购买合作方发放的兑换码（周边 / 优惠码等），拿到 code 后到合作方处使用。

### GET `/redemption/batches` — 可购买的批次列表

### GET `/redemption/batches/{batch_id}` — 批次详情

### POST `/redemption/purchase` — 用现金购买兑换码

```json
{ "batch_id": 1 }
```
每次购买一个码，响应：`{ "code_id", "code_string", "batch_name", "partner_name", "partner_website_url", "paid_amount", "cash_after" }`。

必须先还清全部借款（含利息）；最新债务大于 0 时返回 403，拒绝购买，不扣款或消耗兑换码库存。判断与借款/还款共享用户行锁。

### GET `/redemption/my` — 我购买的兑换码

### GET `/redemption/my/{code_id}` — 单个兑换码详情（含 `code_string`）

### POST `/redemption/my/{code_id}/mark-used` — 标记已用 / 取消标记

```json
{ "used": true }
```

---

## 8. 弹幕 (Danmuku)

### POST `/danmuku/exchange` — 现金兑换弹幕额度

```json
{ "qq_user_id": "10001", "room_id": "弹幕群", "yuan": 0, "huo": 10 }
```
扣减站内 cash = `yuan + huo`（1:1）。响应：`{ "id", "code_string", "yuan", "huo", "amount", "cash_after", "timestamp" }`（与朋友的 danmuku 服务端约定 HMAC 签名）。

同样要求借款全部还清；最新债务大于 0 时返回 403，不扣款、不生成激活码。

### GET `/danmuku/my` — 我的弹幕兑换记录

---

## 9. 称号 (Title)

可佩戴称号系统（区别于按净值自动判定的 rank）。

### GET `/title/catalog` — 全部称号目录（公开）

### GET `/title/me` — 我的称号与当前佩戴

响应：`{ "equipped_title_id", "titles": [TitleOut, ...] }`。

### POST `/title/redeem` — 兑换码兑换称号

```json
{ "code": "XXXX" }
```

### POST `/title/me/equip` — 佩戴 / 卸下称号

```json
{ "title_id": 3 }
```
`title_id` 为 `null` 表示卸下。

### GET `/title/users/{user_id}/equipped` — 查看某用户当前佩戴的称号（chip）

---

## 10. 站点配置 (Site Config)

借贷利率、强平阈值、活动模式（反作弊总开关）等运行时参数，挂在 `/api/v1/admin` 前缀下。

### GET `/admin/site-config` — 读取全部站点配置（仅管理员）

### PUT `/admin/site-config/{key}` — 更新单项配置（仅管理员）

```json
{ "value": "0.01" }
```

---

## 11. 管理端 (Admin)

均需管理员权限。完整请求/响应见各路由源码。

**兑换码** `/admin/redemption`
- `GET /partners`、`POST /partners`、`PATCH /partners/{partner_id}`
- `GET /batches`、`POST /batches`、`PATCH /batches/{batch_id}`
- `POST /batches/{batch_id}/import/preview`、`POST /batches/{batch_id}/import/commit`（CSV 导入：先预览后提交）

**称号** `/admin`（admin_title）
- `GET /titles`、`POST /titles`、`PATCH /titles/{title_id}`
- `GET /title-batches`、`POST /title-batches`、`POST /title-batches/{batch_id}/import-codes`（批量导入兑换码）
- `GET /users/{user_id}/titles`、`POST /users/{user_id}/titles`、`DELETE /users/{user_id}/titles/{title_id}`
- `GET /markets/{market_id}/required-titles`、`PUT /markets/{market_id}/required-titles`（市场称号门槛）

**用户 / 资金 / 贷款 / 账号** `/admin/users`（admin_users，逻辑在 `services/admin_user_service.py`）
- `GET /`（用户列表，最多 200）、`GET /{user_id}`（资产快照 + 装备称号）
- `POST /{user_id}/cash` `{amount, reason}`（正加负扣，不能扣成负；写 `admin_adjust_cash` 流水）
- `POST /{user_id}/loan`、`POST /{user_id}/forgive-debt` `{amount, reason}`（强制放贷 / 免债，免债先结息，超额自动截断）
- `PATCH /{user_id}/role` `{is_admin}`（不能改自己、不能取消最后一个管理员）
- `PATCH /{user_id}/ban` `{reason?, related_suspicion_id?}`、`PATCH /{user_id}/unban`
- `POST /batch/adjust-cash` `{filter, amount, reason, dry_run}`（先 dry_run 预览再执行；单批上限 500；操作后为负的用户跳过）
- `POST /batch/amnesty` `{filter, reset_cash_to?, forgive_debt, reason, dry_run}` — **大赦天下**：匹配用户债务清零（先结息）+ 现金设为 `reset_cash_to`（默认 `site_config.initial_balance`，高于目标的同样降下来）；持仓不动；每人一条 `admin_amnesty` 流水
- `filter` 字段：`user_id_min/max`、`cash_min/max`、`debt_min/max`、`is_active`、`include_superuser`（默认 false）

**统计** `/admin/stats`
- `GET /wealth`（平台资产分布）

**强平** `/admin/liquidation`
- `POST /run-now`（立即跑一次强平 sweep）

**反作弊** `/admin/bot`
- `GET /suspicions`、`PATCH /suspicions/{suspicion_id}/review`、`GET /banned-users`、`GET /stats`
- 封号 / 解封走 `/admin/users/{user_id}/ban`、`/unban`

---

## 12. 幻想外汇 (FX)

FX 是默认关闭的独立子游戏；完整运维手册见 [`docs/fx.md`](fx.md)。
总闸 `site_config.fx_enabled` 默认 `false`：关闭时成交 / 发布返回 403，公开行情仍可只读。
所有金额为 6 位 `Decimal`，REST 以 **JSON 字符串** 返回。

**公开字段白名单**：公开 schema 只含行情与已发布新闻。任何公开响应都**不含**
`gold_reserve` / `foreign_reserve` / `target_price` / `target_min` / `target_max` /
`initial_price` / `buy_fee_rate` / `sell_fee_rate` / `shock_ratio` / `first_reaction_ratio` /
`parameter_snapshot` / `idempotency_key` / `min_out` / 成交来源 `source` / 成交用户身份。
内部系统来源（`system_event` / `system_noise` / `system_target`）只在管理员
`GET /admin/fx/pairs/{id}/interventions` 暴露。

### GET `/fx/pairs` — 货币对列表（公开）

非 `draft` 的货币对，返回 `FxPairPublic`：
`id`、`currency_code`、`currency_name`、`status`、`pool_version`、`created_at`、`updated_at`。

### GET `/fx/pairs/{pair_id}/snapshot` — 行情快照（公开）

返回 `FxSnapshot`：`pair`（`FxPairPublic`）、`price`（边际价）、`buy_price`、`sell_price`、
`spread`、`volume_24h`、`history_version`、`history_ready`。三个价格字段统一为「金圆券 / 1 外币」口径：`buy_price` 是
买入外币的有效 ask（金入 / 外币出），`sell_price` 是卖出外币的有效 bid（金出 / 外币入），
`spread = buy_price − sell_price`，含手续费时为正、深池取整时可能为 0。`volume_24h` 是精确滚动
24 小时金侧成交量。`history_version`（可空 opaque 字符串）与 `history_ready`（bool）是给无逐卡
SSE 的主页做缓存历史引导用的追加公开字段：未回填时 `history_version=null`、`history_ready=false`，
客户端据此回退轻量 `/chart`，不伪造覆盖。
若库中已存在病态 pair（费率为 1 或储备小到单笔产出向下量化为 0），该接口返回带原因的
**422**，不会 500。

### POST `/fx/pairs/{pair_id}/quote` — 报价（公开）

请求 `{ "side": "buy"|"sell", "amount": "10.000000" }`，返回 `FxQuote`：
`pair_id`、`side`、`input_amount`、`output_amount`、`fee_amount`、`effective_price`、
`post_price`、`expires_at`。报价不校验 gate、不锁定价格；成交时以事务内重新报价为准。

### POST `/fx/pairs/{pair_id}/trades` — 成交（需登录）

请求：`{ "side", "amount", "min_out", "idempotency_key" }`。

- 仅 `status='trading'` 且 gate 开启时可成交；成交时重新报价，`output_amount < min_out`
  返回 409（过期报价保护）。
- 幂等：`(user_id, idempotency_key)` 唯一。同键同参重放返回原成交且不重复扣款；
  同键换参数返回 409。
- 拒绝条件（403）：bot 账号、未接受 TOS、货币对非 trading、gate 关闭；
  `debt > 0` 禁止买入（卖出已有外币不受影响）。
- 金额必须有限、正、最多 6 位小数（否则 422）。
- 返回 `FxTradePublic`：`id`、`pair_id`、`side`、`input_amount`、`output_amount`、
  `fee_amount`、`post_price`、`created_at`（不含内部 `source`）。

### GET `/fx/pairs/{pair_id}/trades` — 公开成交流（公开）

最近成交（`limit` 1–100，默认 50），返回 `FxTradePublic` 列表；不含用户身份与内部参数。

### GET `/fx/pairs/{pair_id}/wallet` — 个人钱包（需登录）

当前用户在该货币对的 `FxWalletPublic`：`pair_id`、`foreign_amount`、`cost_basis`、
`updated_at`。从未交易时返回零值且 `updated_at=null`，不返回 404。不含其他用户数据或
pair 的隐藏参数。

### GET `/fx/pairs/{pair_id}/my-trades` — 个人成交历史（需登录）

当前用户在该货币对的成交（新到旧，`limit` 1–100，默认 50），返回 `FxTradePublic` 列表；
只含本人成交，不含其他用户身份。

### GET `/fx/pairs/{pair_id}/chart?interval=1m&from=&to=` — K 线（公开）

返回旧版 OHLCV body **结构不变**：`bucket_start`、`interval`、`open`、`high`、`low`、`close`、
`volume`。数据来源是物化 `fx_candle` + 内存 ring + 仅投影列（`id/pair_id/created_at/post_price/side/input_amount/output_amount`）
的未落库尾段，**不再重扫原始 `FxTrade` ORM**。响应头 `X-FX-Through-Trade-ID`（该 body 精确覆盖到
的已提交成交 id 上界）与 `X-FX-History-Version`（body 所属历史版本）由 CORS 暴露，供前端缓存历史
回退与 SSE 尾段去重。
`from >= to`、非法 interval、**输出桶或细粒度来源桶超过 20,000** 返回 **422**；历史尚未回填
（`FxMarketDataState.history_ready=false`）返回可重试 **503**，绝不退回全量原始成交重扫。
支持的 period 为 `10s/1m/15m/1h`，以及能被某个原生周期整除的 legacy `Nm/Nh/Nd`（rollup）。

### GET `/history/fx/{pair_id}/{history_version}/{interval}/{segment_epoch}.json` — 不可变历史段（公开）

已封存段的列式 OHLCV：`t0`、`step`、`n_buckets`、`t`、`o`、`h`、`l`、`c`、`v`、`trades`；价格与量
以十进制字符串返回（不用 LMSR 的 `price × 1e8` 整数编码）。响应带 immutable 缓存头，走现有
`/history/` 的 nginx 缓存与有界进程 LRU，cache key 含产品、pair、历史版本、周期、段起点。
段未封存、`interval` 不支持、`segment_epoch` 未对齐段长或历史版本已失效返回 **404**；历史未就绪、
对应 flush 未完成，或真正只读/standby 连接无法取得一致性共享锁时返回 **503**（客户端回退
`/chart`），绝不把不完整段固化成 immutable 结果。

### GET `/fx/stream/{pair_id}` — SSE（公开）

先发 `snapshot` 帧（`seq=0`），随后推送 `fx` 命名事件。旧行情字段只允许
`price`、`buy_price`、`sell_price`、`spread`、`volume`，新闻只允许
`title`、`body`、`kind`、`published_at`。漏帧可用 snapshot 恢复。
首包在此之外追加 `history_version`、`history_ready`、`history_tail`（`10s/1m/15m/1h` 的列式尾段）、
`history_tail_at`、`history_tail_through_trade_id`；增量 `fx` 帧追加公开 `trades` 列表
`{id, ts, post_price, gold_volume}`（由已提交成交生成，不含账户/债务/未来干预参数）。有界公开
缓冲溢出时帧带 `history_invalidated: true`，客户端应只补尾段而不是把部分增量当作完整历史。

### 管理端 `/admin/fx`（仅超管）

- `GET /pairs`：管理端只读列表（**包含草稿**），返回 `FxPairAdminDetail` =
  `FxPairAdmin` + `gold_balance`、`foreign_balance`、`daily_spend`、`spend_date`；
  供管理页刷新后仍显示池子与系统 treasury，不再依赖写操作响应。
- `POST /pairs`、`PATCH /pairs/{id}`：创建 / 修改货币对；同时只允许一个 `trading`；
  开市后不可改币种代码/名称。费率必须 `0 <= rate < 1`（`1` 会被 422 拒绝，避免 AMM
  吃掉全部输入）。返回 `FxPairAdmin`（含储备与目标等私有字段）。
- `POST /pairs/{id}/fund`、`POST /pairs/{id}/withdraw`：**注资 / 撤资唯一入口**，
  请求 `{ "gold_amount", "foreign_amount" }`；不越过储备安全下限，写审计。
- `GET /config`、`PUT /config`：读取 / 更新 FX 配置（仅 `FX_DEFAULT_CONFIGS` 键）。
- `GET /events`、`POST /events`、`POST /events/{id}/publish`、`POST /events/{id}/cancel`：
  事件草稿 / 排期 / 发布 / 取消；管理响应为 `FxEventAdmin`（含 `shock_ratio`、
  `first_reaction_ratio`、`window_sec`、`budget`、`parameter_snapshot` 等私有字段）。
- `GET /pairs/{id}/interventions`：该货币对的系统干预成交。

---

## 13. Transaction 模型

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `buy` / `sell` / `settle` / `settle_lose` |
| `shares` | Decimal(16,6) | 交易份额 |
| `price` | Decimal(16,8) | 执行均价 (pay/shares) |
| `pre_market_price` | Decimal(16,8) | 交易前瞬时市场价 |
| `post_market_price` | Decimal(16,8) | 交易后瞬时市场价 |
| `gross` | Decimal(16,6) | 手续费前总额 |
| `fee` | Decimal(16,6) | 手续费 |
| `cost` | Decimal(16,6) | 净现金流（buy=+, sell=-） |
