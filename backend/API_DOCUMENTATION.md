# 东方炒炒币 (Touhou Exchange) API 文档

## 概述

东方炒炒币是一个基于LMSR（对数市场评分规则）的预测市场交易平台。本API文档为前端开发人员提供完整的接口说明。

**基础URL**: `http://localhost:8000` (开发环境)
**API版本**: `v1`
**认证方式**: JWT Bearer Token

## 1. 认证模块 (Auth)

### 1.1 用户注册
**POST** `/api/v1/auth/register`

注册新用户账户。

**请求体**:
```json
{
  "email": "user@example.com",
  "password": "securepassword123",
  "username": "touhou_trader",
  "is_active": true,
  "is_superuser": false,
  "is_verified": false
}
```

**响应**:
```json
{
  "id": 1,
  "email": "user@example.com",
  "username": "touhou_trader",
  "cash": 100.0,
  "debt": 0.0,
  "is_active": true,
  "is_superuser": false,
  "is_verified": false
}
```

### 1.2 用户登录
**POST** `/api/v1/auth/jwt/login`

获取JWT访问令牌。

**请求体**:
```json
{
  "username": "touhou_trader",
  "password": "securepassword123"
}
```

**响应**:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

### 1.3 获取当前用户信息
**GET** `/api/v1/auth/me`

获取当前登录用户信息（需要认证）。

**响应**:
```json
{
  "id": 1,
  "email": "user@example.com",
  "username": "touhou_trader",
  "cash": 150.25,
  "debt": 0.0,
  "is_active": true,
  "is_superuser": false,
  "is_verified": true
}
```

### 1.4 使用激活码激活账号
**POST** `/api/v1/auth/activate`

使用一次性激活码激活用户账号。

**请求体**:
```json
{
  "code": "ABCDEFGH12345678"
}
```

**响应**:
```json
{
  "message": "激活成功",
  "username": "touhou_trader"
}
```

### 1.5 管理员：批量生成激活码
**POST** `/api/v1/auth/admin/activation-codes/batch`

批量生成激活码（仅管理员）。

**请求体**:
```json
{
  "count": 10,
  "length": 16
}
```

**响应**:
```json
{
  "count": 10,
  "codes": [
    "ABCDEFGH12345678",
    "IJKLMNOP87654321",
    ...
  ]
}
```

### 1.6 管理员：查看激活码列表
**GET** `/api/v1/auth/admin/activation-codes`

查看激活码列表（仅管理员）。

**查询参数**:
- `used`: boolean (可选) - 筛选已使用/未使用
- `limit`: int (默认200) - 返回数量限制

**响应**:
```json
[
  {
    "id": 1,
    "code": "ABCDEFGH12345678",
    "is_used": false,
    "used_by_user_id": null,
    "used_at": null,
    "created_at": "2024-01-01T00:00:00Z"
  }
]
```

### 1.7 管理员：作废激活码
**DELETE** `/api/v1/auth/admin/activation-codes/{code_id}`

作废未使用的激活码（仅管理员）。

## 2. 用户资产模块 (UserAssets)

### 2.1 获取资产概览
**GET** `/api/v1/user/summary`

获取用户资产概览，包括现金、负债、持仓市值和净值。

**响应**:
```json
{
  "cash": 150.25,
  "debt": 0.0,
  "holdings_value": 89.75,
  "net_worth": 240.0,
  "rank": "人间之里的小商贩"
}
```

**称号规则**:
- 净值 > 50000: "大天狗的座上宾"
- 净值 > 10000: "守矢神社的VIP"
- 净值 > 2000: "命莲寺的赞助者"
- 净值 > 500: "人间之里的小商贩"
- 其他: "初入幻想乡的无名氏"

### 2.2 获取持仓明细
**GET** `/api/v1/user/holdings`

获取用户当前持仓明细。

**响应**:
```json
[
  {
    "market_id": 1,
    "market_title": "博丽灵梦能否在下次异变中获胜",
    "outcome_id": 1,
    "outcome_label": "能",
    "amount": 50.5
  }
]
```

### 2.3 获取交易历史
**GET** `/api/v1/user/transactions`

获取用户最近50条交易记录。

**响应**:
```json
[
  {
    "id": 123,
    "type": "buy",
    "shares": 10.0,
    "price": 0.45,
    "cost": 4.5,
    "timestamp": "2024-01-01T12:00:00Z"
  }
]
```

## 3. 市场交易模块 (Market)

### 3.1 获取所有活跃市场
**GET** `/api/v1/market/list`

获取所有处于交易状态的市场列表。

**响应**:
```json
[
  {
    "id": 1,
    "title": "博丽灵梦能否在下次异变中获胜",
    "description": "预测博丽灵梦在下次异变中的表现",
    "liquidity_b": 100.0,
    "status": "trading",
    "outcomes": [
      {
        "id": 1,
        "label": "能",
        "shares": 150.5,
        "current_price": 0.45
      },
      {
        "id": 2,
        "label": "不能",
        "shares": 89.5,
        "current_price": 0.55
      }
    ]
  }
]
```

### 3.2 获取市场详情
**GET** `/api/v1/market/{market_id}`

获取指定市场的详细信息。

**响应**:
```json
{
  "id": 1,
  "title": "博丽灵梦能否在下次异变中获胜",
  "description": "预测博丽灵梦在下次异变中的表现",
  "status": "trading",
  "liquidity_b": 100.0,
  "created_at": "2024-01-01T00:00:00Z",
  "winning_outcome_id": null,
  "settled_at": null,
  "settled_by_user_id": null,
  "outcomes": [
    {
      "id": 1,
      "label": "能",
      "total_shares": 150.5,
      "current_price": 0.45,
      "payout": null,
      "is_winner": null
    }
  ],
  "last_trade_at": "2024-01-01T12:00:00Z"
}
```

### 3.3 创建新市场（仅管理员）
**POST** `/api/v1/market/create`

创建新的预测市场。

**请求体**:
```json
{
  "title": "新市场标题",
  "description": "市场描述",
  "liquidity_b": 100.0,
  "outcomes": ["选项A", "选项B", "选项C"]
}
```

**响应**:
```json
{
  "status": "success",
  "market_id": 2,
  "title": "新市场标题",
  "outcomes": ["选项A", "选项B", "选项C"],
  "created_by": "admin_user"
}
```

### 3.4 买入胜券
**POST** `/api/v1/market/buy`

买入指定选项的胜券。

**请求体**:
```json
{
  "outcome_id": 1,
  "shares": 10.0
}
```

**响应**:
```json
{
  "shares": 10.0,
  "cost": 4.5,
  "new_cash": 145.75,
  "message": "成功买入 10 张 能（均价≈0.450000）"
}
```

### 3.5 卖出胜券
**POST** `/api/v1/market/sell`

卖出指定选项的胜券。

**请求体**:
```json
{
  "outcome_id": 1,
  "shares": 5.0
}
```

**响应**:
```json
{
  "shares": 5.0,
  "cost": -2.25,
  "new_cash": 148.0,
  "message": "卖出成功，获得 2.25（手续费 0.00，均价≈0.450000）"
}
```

### 3.6 下单预估
**POST** `/api/v1/market/quote`

预估交易成本，不实际成交。

**请求体**:
```json
{
  "outcome_id": 1,
  "shares": 10.0,
  "side": "buy"
}
```

**响应**:
```json
{
  "outcome_id": 1,
  "side": "buy",
  "shares": 10.0,
  "avg_price": 0.45,
  "gross": 4.5,
  "fee": 0.0,
  "net": 4.5,
  "after_prices": [
    {
      "id": 1,
      "label": "能",
      "shares": 160.5,
      "current_price": 0.452
    },
    {
      "id": 2,
      "label": "不能",
      "shares": 89.5,
      "current_price": 0.548
    }
  ]
}
```

### 3.7 关闭市场交易（仅管理员）
**POST** `/api/v1/market/{market_id}/close`

将市场状态改为"halt"（熔断），停止交易。

**响应**:
```json
{
  "message": "市场 博丽灵梦能否在下次异变中获胜 已停止交易（熔断）"
}
```

### 3.8 恢复市场交易（仅管理员）
**POST** `/api/v1/market/{market_id}/resume`

将市场从"halt"状态恢复为"trading"状态。

**响应**:
```json
{
  "message": "市场 博丽灵梦能否在下次异变中获胜 已恢复交易"
}
```

### 3.9 结算市场（仅管理员）
**POST** `/api/v1/market/{market_id}/settle`

结算市场，指定赢家选项。

**请求体**:
```json
{
  "winning_outcome_id": 1
}
```

**响应**:
```json
{
  "message": "市场已结算：赢家 outcome_id=1（by admin_user）"
}
```

### 3.10 结算市场（指定兑付，仅管理员）
**POST** `/api/v1/market/{market_id}/resolve`

结算市场，指定赢家选项和兑付金额。

**请求体**:
```json
{
  "winning_outcome_id": 1,
  "payout": 1.0
}
```

**响应**:
```json
{
  "market_id": 1,
  "status": "settled",
  "winning_outcome_id": 1,
  "settled_at": "2024-01-01T12:00:00Z",
  "total_payout": 50.5,
  "settled_positions": 10
}
```

### 3.11 获取市场成交记录
**GET** `/api/v1/market/{market_id}/trades`

获取市场的逐笔成交记录。

**查询参数**:
- `limit`: int (默认50) - 返回数量限制

**响应**:
```json
[
  {
    "id": 123,
    "outcome_id": 1,
    "side": "buy",
    "shares": 10.0,
    "price": 0.45,
    "gross": 4.5,
    "fee": 0.0,
    "timestamp": "2024-01-01T12:00:00Z"
  }
]
```

### 3.12 财富排行榜
**GET** `/api/v1/market/leaderboard`

获取用户财富排行榜。

**查询参数**:
- `limit`: int (默认20) - 返回数量限制

**响应**:
```json
[
  {
    "user_id": 1,
    "username": "rich_trader",
    "net_worth": 50000.5,
    "rank": "大天狗的座上宾"
  }
]
```

## 4. 图表数据模块 (Chart)

### 4.1 价格曲线
**GET** `/api/v1/chart/price`

获取指定选项的价格曲线数据。

**查询参数**:
- `outcome_id`: int - 选项ID
- `from_ts`: datetime - 起始时间（ISO格式）
- `to_ts`: datetime - 结束时间（ISO格式）
- `limit`: int (默认5000) - 最多返回点数
- `bucket`: string (可选) - 降采样桶大小（10s/30s/1m/5m/15m/1h/1d）

**响应**:
```json
{
  "outcome_id": 1,
  "from_ts": "2024-01-01T00:00:00Z",
  "to_ts": "2024-01-01T12:00:00Z",
  "points": [
    {
      "ts": "2024-01-01T10:00:00Z",
      "price": 0.45
    }
  ]
}
```

### 4.2 K线数据
**GET** `/api/v1/chart/candles`

获取指定选项的K线（OHLCV）数据。

**查询参数**:
- `outcome_id`: int - 选项ID
- `interval`: string - K线周期（10s/30s/1m/5m/15m/1h/1d）
- `from_ts`: datetime - 起始时间（ISO格式）
- `to_ts`: datetime - 结束时间（ISO格式）
- `fill`: boolean (默认true) - 是否补齐空桶
- `limit`: int (默认5000) - 最多返回K线根数
- `max_trades`: int (默认200000) - 最多扫描交易条数

**响应**:
```json
{
  "outcome_id": 1,
  "interval": "10s",
  "from_ts": "2024-01-01T00:00:00Z",
  "to_ts": "2024-01-01T12:00:00Z",
  "candles": [
    {
      "t": "2024-01-01T10:00:00Z",
      "o": 0.45,
      "h": 0.46,
      "l": 0.44,
      "c": 0.455,
      "v": 100.5,
      "n": 5
    }
  ]
}
```

## 5. 实时流模块 (Stream)

### 5.1 市场实时数据流（SSE）
**GET** `/api/v1/stream/market/{market_id}`

通过Server-Sent Events (SSE)推送市场实时数据。

**事件类型**:
1. `snapshot` - 初始快照（市场当前状态）
2. `trade` - 交易事件
3. `market_status` - 市场状态变更
4. `ping` - 心跳包（每15秒）

**snapshot事件示例**:
```json
{
  "type": "snapshot",
  "market_id": 1,
  "ts": "2024-01-01T12:00:00Z",
  "data": {
    "id": 1,
    "title": "博丽灵梦能否在下次异变中获胜",
    "description": "预测博丽灵梦在下次异变中的表现",
    "status": "trading",
    "liquidity_b": 100.0,
    "created_at": "2024-01-01T00:00:00Z",
    "winning_outcome_id": null,
    "settled_at": null,
    "settled_by_user_id": null,
    "outcomes": [
      {
        "id": 1,
        "label": "能",
        "total_shares": 150.5,
        "current_price": 0.45,
        "payout": null,
        "is_winner": null
      }
    ]
  }
}
```

**trade事件示例**:
```json
{
  "type": "trade",
  "market_id": 1,
  "ts": "2024-01-01T12:00:01Z",
  "data": {
    "trade": {
      "type": "buy",
      "outcome_id": 1,
      "shares": 10.0,
      "price": 0.45,
      "timestamp": "2024-01-01T12:00:01Z"
    }
  }
}
```

## 6. 数据模型说明

### 6.1 用户相关模型
```typescript
interface UserRead {
  id: number;
  email: string;
  username: string;
  cash: number;
  debt: number;
  is_active: boolean;
  is_superuser: boolean;
  is_verified: boolean;
}

interface UserCreate {
  email: string;
  password: string;
  username: string;
  cash?: number;  // 默认100.0
  is_superuser?: boolean;  // 默认false
}

interface UserUpdate {
  username?: string;
  cash?: number;
  is_superuser?: boolean;
}

interface UserSummary {
  cash: number;
  debt: number;
  holdings_value: number;
  net_worth: number;
  rank: string;
}

interface HoldingRead {
  market_id: number;
  market_title: string;
  outcome_id: number;
  outcome_label: string;
  amount: number;
}

interface TransactionRead {
  id: number;
  type: string;  // "buy" | "sell" | "settle"
  shares: number;
  price: number;
  cost: number;
  timestamp: string;
}
```

### 6.2 市场相关模型
```typescript
interface MarketCreate {
  title: string;
  description?: string;
  liquidity_b: number;  // > 0
  outcomes: string[];   // 至少2个选项
}

interface MarketListItem {
  id: number;
  title: string;
  description?: string;
  liquidity_b: number;
  status: string;  // "trading" | "halt" | "settled"
  outcomes: OutcomePriceRead[];
}

interface MarketDetailRead {
  id: number;
  title: string;
  description: string;
  status: string;
  liquidity_b: number;
  created_at: string;
  winning_outcome_id?: number;
  settled_at?: string;
  settled_by_user_id?: number;
  outcomes: OutcomeQuoteRead[];
  last_trade_at?: string;
}

interface OutcomePriceRead {
  id: number;
  label: string;
  shares: number;
  current_price: number;
}

interface OutcomeQuoteRead {
  id: number;
  label: string;
  total_shares: number;
  current_price: number;
  payout?: number;
  is_winner?: boolean;
}
```

### 6.3 交易相关模型
```typescript
interface TradeRequest {
  outcome_id: number;
  shares: number;  // > 0
}

interface TradeResponse {
  shares: number;
  cost: number;
  new_cash: number;
  message: string;
}

interface QuoteRequest {
  outcome_id: number;
  shares: number;  // > 0
  side: "buy" | "sell";
}

interface QuoteResponse {
  outcome_id: number;
  side: string;
  shares: number;
  avg_price: number;
  gross: number;
  fee: number;
  net: number;
  after_prices: OutcomePriceRead[];
}

interface MarketTradeRead {
  id: number;
  outcome_id: number;
  side: string;  // "buy" | "sell"
  shares: number;
  price: number;
  gross: number;
  fee: number;
  timestamp: string;
}

interface LeaderboardItem {
  user_id: number;
  username: string;
  net_worth: number;
  rank: string;
}
```

### 6.4 结算相关模型
```typescript
interface SettleRequest {
  winning_outcome_id: number;
}

interface ResolveRequest {
  winning_outcome_id: number;
  payout: number;  // >= 0
}

interface SettleResult {
  market_id: number;
  status: string;
  winning_outcome_id: number;
  settled_at: string;
  total_payout: number;
  settled_positions: number;
}
```

### 6.5 激活码相关模型
```typescript
interface ActivateRequest {
  code: string;  // 6-64字符
}

interface CreateActivationCodesRequest {
  count: number;   // 1-500
  length?: number; // 8-64，默认16
}

interface ActivationCodeRead {
  id: number;
  code: string;
  is_used: boolean;
  used_by_user_id?: number;
  used_at?: string;
  created_at: string;
}
```

### 6.6 图表相关模型
```typescript
interface PricePoint {
  ts: string;
  price: number;
}

interface Candle {
  t: string;  // bucket start
  o: number;  // open
  h: number;  // high
  l: number;  // low
  c: number;  // close
  v: number;  // volume (shares)
  n: number;  // number of trades
}

interface PriceSeriesResponse {
  outcome_id: number;
  from_ts: string;
  to_ts: string;
  points: PricePoint[];
}

interface CandleSeriesResponse {
  outcome_id: number;
  interval: "10s" | "30s" | "1m" | "5m" | "15m" | "1h" | "1d";
  from_ts: string;
  to_ts: string;
  candles: Candle[];
}
```

### 6.7 实时流事件模型
```typescript
interface MarketEvent {
  type: "snapshot" | "trade" | "market_status" | "ping";
  market_id: number;
  ts: string;
  data: any;
}

interface TradeEventData {
  trade: {
    type: "buy" | "sell";
    outcome_id: number;
    shares: number;
    price: number;
    timestamp: string;
  };
}

interface MarketStatusEventData {
  status: "trading" | "halt" | "settled";
  winning_outcome_id?: number;
  settled_at?: string;
}
```

## 7. 错误处理

### 7.1 常见HTTP状态码
- `200 OK` - 请求成功
- `201 Created` - 资源创建成功
- `204 No Content` - 请求成功，无返回内容
- `400 Bad Request` - 请求参数错误
- `401 Unauthorized` - 未认证或认证失败
- `403 Forbidden` - 权限不足
- `404 Not Found` - 资源不存在
- `422 Unprocessable Entity` - 请求参数验证失败
- `500 Internal Server Error` - 服务器内部错误

### 7.2 错误响应格式
```json
{
  "detail": "错误描述信息"
}
```

### 7.3 常见错误信息
- `"市场不存在"` - 指定的market_id不存在
- `"选项不存在"` - 指定的outcome_id不存在
- `"现金不足"` - 用户现金不足以完成交易
- `"持仓不足"` - 用户持仓不足以完成卖出
- `"激活码不存在"` - 激活码无效
- `"激活码已被使用"` - 激活码已被使用
- `"市场当前不可交易"` - 市场状态不是"trading"
- `"市场当前不在熔断期，无法结算"` - 市场状态不是"halt"

## 8. 认证与授权

### 8.1 JWT认证
1. 通过 `/api/v1/auth/jwt/login` 获取访问令牌
2. 在请求头中添加：`Authorization: Bearer <access_token>`
3. 令牌有效期：根据后端配置（通常24小时）

### 8.2 权限级别
1. **普通用户**：可以注册、登录、交易、查看资产
2. **激活用户**：普通用户 + 可以使用激活码激活账号
3. **管理员**：所有权限 + 市场管理 + 激活码管理

### 8.3 需要认证的接口
- 所有 `/api/v1/user/*` 接口
- 所有 `/api/v1/market/buy`, `/api/v1/market/sell`, `/api/v1/market/quote`
- `/api/v1/auth/me`
- `/api/v1/auth/activate`

### 8.4 需要管理员权限的接口
- `/api/v1/market/create`
- `/api/v1/market/{market_id}/close`
- `/api/v1/market/{market_id}/resume`
- `/api/v1/market/{market_id}/settle`
- `/api/v1/market/{market_id}/resolve`
- `/api/v1/auth/admin/activation-codes/*`

## 9. 使用示例

### 9.1 完整交易流程
```javascript
// 1. 用户登录
const loginRes = await fetch('/api/v1/auth/jwt/login', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    username: 'touhou_trader',
    password: 'securepassword123'
  })
});
const { access_token } = await loginRes.json();

// 2. 获取市场列表
const marketsRes = await fetch('/api/v1/market/list', {
  headers: {'Authorization': `Bearer ${access_token}`}
});
const markets = await marketsRes.json();

// 3. 获取市场详情
const marketId = markets[0].id;
const marketDetailRes = await fetch(`/api/v1/market/${marketId}`, {
  headers: {'Authorization': `Bearer ${access_token}`}
});
const marketDetail = await marketDetailRes.json();

// 4. 下单预估
const quoteRes = await fetch('/api/v1/market/quote', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${access_token}`
  },
  body: JSON.stringify({
    outcome_id: marketDetail.outcomes[0].id,
    shares: 10.0,
    side: 'buy'
  })
});
const quote = await quoteRes.json();

// 5. 执行买入
const buyRes = await fetch('/api/v1/market/buy', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${access_token}`
  },
  body: JSON.stringify({
    outcome_id: marketDetail.outcomes[0].id,
    shares: 10.0
  })
});
const buyResult = await buyRes.json();

// 6. 订阅实时数据
const eventSource = new EventSource(`/api/v1/stream/market/${marketId}`);
eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('实时数据:', data);
};
```

### 9.2 前端集成建议
1. **Token管理**：登录后存储token到localStorage或cookie
2. **自动刷新**：实现token过期自动刷新机制
3. **错误处理**：统一处理401错误，跳转到登录页
4. **加载状态**：交易接口显示加载状态，防止重复提交
5. **实时更新**：使用SSE或WebSocket保持数据实时性
6. **数据缓存**：适当缓存市场列表等不常变的数据

## 10. 注意事项

### 10.1 交易规则
1. **LMSR定价**：价格基于对数市场评分规则动态计算
2. **手续费**：买入无手续费，卖出有0%手续费（可配置）
3. **最小交易单位**：shares必须为正数，支持小数
4. **市场状态**：
   - `trading`：可交易
   - `halt`：熔断，不可交易
   - `settled`：已结算，不可交易

### 10.2 数据一致性
1. **并发控制**：所有交易接口使用数据库行锁保证一致性
2. **事务处理**：买卖操作在事务中完成，保证原子性
3. **实时同步**：交易后通过SSE推送更新到所有客户端

### 10.3 性能考虑
1. **分页查询**：交易历史、激活码列表等接口支持limit参数
2. **时间范围**：图表数据接口需要指定时间范围，避免查询过多数据
3. **缓存策略**：市场列表等数据可适当缓存

### 10.4 安全建议
1. **HTTPS**：生产环境必须使用HTTPS
2. **输入验证**：所有用户输入都经过Pydantic模型验证
3. **SQL注入防护**：使用SQLAlchemy ORM，避免SQL注入
4. **XSS防护**：输出数据时进行适当的转义

## 11. 更新日志

### v1.0.0 (2024-01-01)
- 初始版本发布
- 完整的用户认证系统
- 基于LMSR的市场交易功能
- 实时数据推送（SSE）
- 图表数据接口
- 管理员管理功能

---

**文档维护**：后端开发团队  
**最后更新**：2024年1月1日  
**版本**：v1.0.0

> 注意：本文档基于当前代码版本编写，如有API变更，请参考最新的代码实现。
