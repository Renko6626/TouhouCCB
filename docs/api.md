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

## 12. Transaction 模型

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
