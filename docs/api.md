# 东方炒炒币 API 文档

**Base URL**: `/api/v1`
**认证方式**: JWT Bearer Token（通过 Casdoor SSO 获取）

---

## 1. 认证 (Auth)

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
  "debt": 0.00
}
```

---

## 2. 用户 (User)

### GET `/user/summary` — 资产概览

持仓市值使用 LMSR 清算价值（含滑点），非瞬时价格 x 数量。

```json
{
  "cash": 150.25,
  "debt": 0.00,
  "holdings_value": 89.75,
  "total_cost_basis": 70.00,
  "unrealized_pnl": 19.75,
  "net_worth": 240.00,
  "rank": "人间之里的小商贩"
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

### GET `/user/list` — 用户列表（仅管理员）

### POST `/user/{user_id}/adjust-cash` — 调整用户现金（仅管理员）

```json
{ "amount": 100.00, "reason": "活动奖励" }
```

正数加钱，负数扣钱。操作后现金不能为负。

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

### GET `/market/{id}/trades` — 市场成交记录

### GET `/market/leaderboard` — 财富排行榜

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

### POST `/market/{id}/close` — 熔断（仅管理员）

### POST `/market/{id}/resume` — 恢复交易（仅管理员）

### POST `/market/{id}/resolve` — 结算（仅管理员）

```json
{ "winning_outcome_id": 1, "payout": 1.0 }
```

赢家仓位按 payout 兑付现金（创建 `settle` 交易）。
亏损仓位份额归零（创建 `settle_lose` 交易）。
所有 Position 删除。

---

## 4. 图表 (Chart)

### 架构说明

LMSR 交易任何选项会改变**所有**选项的价格。图表 API 不是只查目标选项的交易记录，而是查**整个市场所有交易**，逐笔重放 shares 状态，计算目标选项在每笔交易后的瞬时价格。

### GET `/chart/price` — 价格走势

**参数**: `outcome_id`, `from_ts`, `to_ts`, `limit`, `bucket`(可选)

返回 `[{ ts, price }, ...]`，每个点是一笔交易后的瞬时市场价。

### GET `/chart/candles` — K 线

**参数**: `outcome_id`, `interval`(10s/30s/1m/5m/15m/1h/1d), `from_ts`, `to_ts`, `fill`, `limit`, `max_trades`

返回 `[{ t, o, h, l, c, v, n }, ...]`

- `o` (open) = bucket 内第一笔交易前的市场价
- `c` (close) = bucket 内最后一笔交易后的市场价
- `fill=true` 时空桶用上一根 close 补平

---

## 5. 实时推送 (Stream)

### GET `/stream/market/{id}` — SSE

事件类型:
- `snapshot` — 市场当前状态快照
- `trade` — 新成交
- `market_status` — 状态变更（熔断/恢复/结算）
- `ping` — 心跳（30s）

---

## 6. Transaction 模型

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
