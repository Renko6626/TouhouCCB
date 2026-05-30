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
- `holdings_value` / `net_worth`：LMSR 清算价值（LCV，含卖出滑点），用于风控。
- `holdings_value_mtm` / `net_worth_mtm`：瞬时市价口径（MTM），用于展示。

```json
{
  "cash": 150.25,
  "debt": 0.00,
  "holdings_value": 89.75,
  "holdings_value_mtm": 92.10,
  "total_cost_basis": 70.00,
  "unrealized_pnl": 19.75,
  "net_worth": 240.00,
  "net_worth_mtm": 242.35,
  "rank": "人里居民"
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

### GET `/user/wealth-leaderboard` — 财富排行榜

按净值（LCV 口径）排序，含每位用户的自动判定称号（见规则书）。

### GET `/user/consume-leaderboard` — 消费排行榜

按累计兑换消费排序。

### GET `/user/profile/{user_id}` — 用户公开主页

返回某用户的公开资料（净值、佩戴称号等）。

### GET `/user/loan-status` — 当前借贷状态

返回当前用户的现金 / 债务 / 保证金概览（借贷页用）。

### GET `/user/me/titles` — 我的称号

当前用户已拥有的称号与当前佩戴。

### GET `/user/me/danmuku-exchanges` — 我的弹幕兑换记录

### GET `/user/list` — 用户列表（仅管理员）

### GET `/user/admin/{user_id}` — 用户详情（仅管理员）

### POST `/user/{user_id}/adjust-cash` — 调整用户现金（仅管理员）

```json
{ "amount": 100.00, "reason": "活动奖励" }
```

正数加钱，负数扣钱。操作后现金不能为负。

### POST `/user/{user_id}/set-titles` — 设置用户称号（仅管理员）

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

> 财富 / 消费排行榜不在 market 下，见 `GET /user/wealth-leaderboard`、`GET /user/consume-leaderboard`。

### GET `/market/{id}/movers` — 选项涨跌幅排行（仅管理员）

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

### POST `/market/{id}/halt` — 熔断 / 暂停交易（仅管理员）

买卖按钮变灰，市场进入 HALT 状态。

### POST `/market/{id}/resume` — 恢复交易（仅管理员）

### POST `/market/{id}/close` — 结束市场并结算（仅管理员）

```json
{ "winning_outcome_id": 1 }
```

### POST `/market/{id}/settle` — 结算市场（仅管理员）

```json
{ "winning_outcome_id": 1 }
```

赢家仓位按 1.00 元/张兑付现金（创建 `settle` 交易），亏损仓位份额归零（创建 `settle_lose` 交易），结算后清理 Position。

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
- 数据直接读 `outcome_candle` 物化表（物化缓存，告别 5000 笔逐笔重放上限；见 `docs/superpowers/specs/2026-05-17-candle-cache-design.md`）

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
  "cash": 100.00,
  "debt": 0.00,
  "net_worth": 100.00,
  "holdings_lcv": 0.00,
  "leverage_k": 5,
  "max_total_debt": 400.00,
  "available_to_borrow": 400.00,
  "daily_rate": 0.01,
  "maintenance_margin": 0.10,
  "current_margin": null,
  "liquidation_price_distance": null
}
```

### POST `/loan/borrow` — 借款

```json
{ "amount": 100.00 }
```
响应：`{ "cash", "debt", "net_worth", "message" }`。

### POST `/loan/repay` — 还款

同 borrow 格式。

### GET `/loan/liquidation-policy` — 强平规则（公开只读）

### GET `/loan/recent-liquidations` — 最近强平事件（公开只读，脱敏）

---

## 7. 兑换码 (Redemption)

### POST `/redemption/redeem` — 兑换码换现金

```json
{ "code": "XXXX-XXXX" }
```
响应：`{ "success", "amount", "new_balance", "message" }`。

### POST `/redemption/purchase` — 用现金购买第三方兑换码

```json
{ "batch_id": 1, "quantity": 1 }
```
响应：`{ "success", "codes": [...], "total_cost", "new_balance", "message" }`。

### GET `/redemption/batches` — 可购买的批次列表

### GET `/redemption/batches/{batch_id}` — 批次详情

### GET `/redemption/my-codes` — 我购买的兑换码

### GET `/redemption/my-codes/{code_id}` — 单个兑换码详情

---

## 8. 弹幕 (Danmuku)

### POST `/danmuku/exchange` — 现金兑换弹幕额度

```json
{ "amount": 10.00 }
```
响应：`{ "code", "amount", "new_balance", "message" }`（与朋友的 danmuku 服务端约定 HMAC 签名）。

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

### POST `/title/equip` — 佩戴 / 卸下称号

```json
{ "title_id": 3 }
```
`title_id` 为 `null` 表示卸下。

---

## 10. 站点配置 (Site Config)

### GET `/site-config` — 获取站点配置（公开只读）

### PUT `/site-config` — 更新站点配置（仅管理员）

借贷利率、强平阈值、活动模式（反作弊总开关）等运行时参数。

---

## 11. 管理端 (Admin)

均需管理员权限。完整请求/响应见各路由源码。

**兑换码** `/admin/redemption`
- `GET /partners`、`POST /partners`
- `GET /batches`、`POST /batches`（CSV 导入）
- `GET /batches/{batch_id}/codes`
- `DELETE /batches/{batch_id}`（作废整个批次）

**称号** `/admin/title`
- `GET /titles`、`POST /titles`、`PATCH /titles/{title_id}`
- `POST /titles/{title_id}/codes`（批量生成兑换码）、`GET /titles/{title_id}/codes`
- `GET /market-gating`、`POST /markets/{market_id}/required-title`（市场称号门槛）

**统计** `/admin/stats`
- `GET /`（财富 / 活跃总览）、`GET /timeseries`、`GET /distribution`

**强平** `/admin/liquidation`
- `GET /preview`（预览将被强平的用户）、`POST /run`（手动触发扫描）

**反作弊** `/admin/bot`
- `GET /suspicions`、`POST /ban`、`POST /unban`、`GET /stats`

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
