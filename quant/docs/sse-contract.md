# TouhouCCB SSE 契约（bot ↔ 主站）

quant bot 通过主站的 SSE 实时流感知成交并驱动策略。本文档固化 bot 依赖的隐式契约，
让 bot 开发者无需读主站源码。**主站改这些字段 = 破坏 bot**，改动需同步本文件。

来源：`backend/app/api/v1/stream.py`、`backend/app/services/realtime.py`、
`backend/app/api/v1/market.py`（事件发布处）。

## 端点

```
GET /api/v1/stream/market/{market_id}      (text/event-stream)
```

- 无需认证（公开行情流）。
- 连接最长 **3600 秒**（`MAX_SSE_DURATION`），到点服务端主动关流，客户端需重连。
- 每 **25 秒**无事件时发一条 `ping` 心跳（防反代超时）。
- 限制：单市场订阅者上限满 → **503**；同一 IP 对单市场并发过多 → **429**；
  市场不存在 → **404**。bot 应对 503/429 退避重试。

## 报文格式

每条 SSE 消息：

```
event: <type>
data: <JSON>

```

`data` JSON 结构（`realtime.sse_pack`）：

```json
{
  "type": "snapshot | trade | market_status | ping",
  "market_id": 123,
  "ts": "2026-06-08T12:00:00+00:00",
  "data": { ... },
  "seq": 42
}
```

- `seq`：**单市场单调递增**序号。bot 用它检测断线期间漏事件（gap）。
  `snapshot` 的 seq 是订阅锚点，其后所有事件 seq 严格 > 锚点。
- `ping` 的 `data` 为 `{}`，`seq` 可忽略。

## 事件类型

### `snapshot`（连接首包）

市场当前完整状态。`data`：

```json
{
  "id": 123, "title": "...", "description": "...",
  "status": "TRADING | HALT | SETTLED",
  "liquidity_b": 100.0,
  "created_at": "ISO", "winning_outcome_id": null,
  "settled_at": null, "settled_by_user_id": null,
  "outcomes": [
    {"id": 1, "label": "A", "total_shares": 0.0,
     "current_price": 0.5, "payout": null, "is_winner": null}
  ]
}
```

bot 用此初始化每个 outcome 的当前价与份额。

### `trade`（每笔买卖成交后广播）

`data.trade`：

```json
{
  "id": 9001, "type": "BUY | SELL",
  "outcome_id": 1, "username": "alice",
  "shares": 50.0, "price": 0.52, "gross": 26.0, "fee": 0.0,
  "post_market_price": 0.55,
  "market_prices_post": [0.55, 0.45],
  "timestamp": "ISO"
}
```

- **`market_prices_post`**：成交后**全市场所有 outcome 的现价快照**，**按 `outcome.id`
  升序**排列。LMSR 下任一 outcome 成交会改变同市场所有 outcome 价格，bot 必须用这个
  数组 O(1) 刷新所有 outcome 当前价，而不是只更新 `outcome_id` 那一支。
- `fee`：买入恒为 0；卖出为实际手续费（受 admin 配置 `sell_fee_rate` 影响）。
- 重复语义：snapshot 与紧随其后的 trade 可能反映同一笔成交（微秒级 race），bot 侧应按
  `trade.id` 去重（INSERT OR IGNORE / 跳过已处理 id）。**宁可重复，不可漏失。**

### `market_status`（市场状态变更）

`data` 含新的市场状态（如转入 `HALT` / `SETTLED`）。bot 应在非 `TRADING` 时停止对该
市场下单。

### `ping`（心跳）

无业务含义，忽略即可（用于保活）。

## bot 消费要点

1. 连接 → 收 `snapshot` 建立初始价格/份额视图。
2. 收 `trade` → 用 `market_prices_post` 全量刷新价格、按 `trade.id` 去重、喂策略。
3. 监控 `seq` 连续性；发现跳号（gap）→ 重连并以新 snapshot 重建视图。
4. `market_status` 非 TRADING → 暂停该市场策略。
5. 连接 1h 到期 / 网络断 → 退避重连（注意 429/503）。

参见 `quant/thccb_quant/client/sse.py` / `sse_subscriber.py` 的实现。
