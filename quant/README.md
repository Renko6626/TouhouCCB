# thccb-quant

TouhouCCB 量化交易脚本（实盘小额）。spec：
`docs/superpowers/specs/2026-05-17-quant-trader-design.md`。

## 起步

```bash
cd quant/
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

cp config.example.yaml config.yaml && $EDITOR config.yaml
cp .env.example .env && $EDITOR .env   # 填浏览器抓的 token

pytest -x                              # 全过才能跑
python -m thccb_quant --dry-run        # 演练 24h
python -m thccb_quant                  # 实盘（或下面的 run.sh 包装）
```

## Token 获取

浏览器登 thccb.com → F12 → Network → 找任意 `/api/v1/*` 请求 → 复制
`Authorization: Bearer <access>` 的 access token；refresh token 在
登录回调响应里。填到 `.env`：

```
THCCB_BASE_URL=http://127.0.0.1:8004
THCCB_ACCESS_TOKEN=eyJ...
THCCB_REFRESH_TOKEN=eyJ...
```

Refresh token 寿命 7 天，**到期需手动重新从浏览器拿**（后端
`/auth/refresh` 不轮换 refresh token）。日志会在到期前 1 天打
`refresh_token_expiring_soon` ERROR。

## 长跑（公用机无 sudo）

```bash
tmux new -d -s quant 'bash $(pwd)/run.sh'
tmux attach -t quant    # 看，Ctrl-b d 退出不影响进程
```

开机自启（用户 crontab）：

```cron
@reboot tmux new -d -s quant 'bash /data/sunyunbo/www/TouhouCCB/quant/run.sh' >> /data/sunyunbo/www/TouhouCCB/quant/logs/cron.log 2>&1
```

## Kill Switch

```bash
touch quant/state/KILL     # 优雅停（看门狗也退）
# 硬停：
tmux kill-session -t quant
```

启动时若 `state/KILL` 存在则拒绝启动，要手工 `rm` 才能重跑。

## 内置策略

- **`dca`** —— 定投，定时按 CNY 金额买入直到总预算耗尽。详见 `docs/strategies.md`
- **`grid`** —— 网格，区间内涨卖跌买。详见 `docs/strategies.md`
- **`volharvest`** —— 波动率收割（SSE 驱动 + logit 空间 mean reversion）。
  详见 `docs/strategies.md` 和 `docs/superpowers/specs/2026-05-17-volatility-harvest-design.md`

## 加新策略

1. 临时停 watchdog：`touch state/KILL`
2. 前台 dry-run：`python -m thccb_quant --dry-run` 跑 24h
3. 看 `logs/decisions.jsonl` 和 `logs/system.jsonl` 没异常
4. 改 `config.yaml` 把新策略 `enabled: true`
5. `rm state/KILL` 重启 watchdog

写新策略类：继承 `thccb_quant.strategy.base.Strategy`，用
`@register("your_type")` 注册，在 `trader.py` 顶部加 import 触发注册副
作用。

## 风控参数

见 `config.yaml` 的 `risk:` 段。默认值（spec §5.1）：

| 参数 | 默认 |
|---|---|
| single_order_cap_cny | 50 |
| daily_loss_cap_cny | 100 |
| daily_turnover_cap_cny | 2000 |
| max_slippage_bps | 300 |
| min_seconds_between_orders | 3 |

**注意**: `daily_loss_cap_cny` 当前是基于"当日净现金流"的保守代理
（卖出收入 - 买入支出），不计未实现浮盈。所以早期 buy 多了会显示为
"亏损"逼近 cap，即使持仓有浮盈。这是有意为之 — 学习阶段宁可保守。

## 日志位置

所有事件统一写入 `logs/system.jsonl`（structlog JSON Lines 格式）。用
`event` 字段区分类型：

- `event` 形如 `order_*` / `strategy_tick_failed` —— 下单相关
- `event` 形如 `kill_switch_detected` / `refresh_token_*` —— 系统事件
- 策略决策记录写在 SQLite `decisions` 表（不是日志文件），用 `sqlite3
  state/quant.db "SELECT * FROM decisions ORDER BY id DESC LIMIT 50"` 看

复盘示例：

```bash
jq 'select(.strategy=="dca_x" and .event=="order_success") | .cost' \
   logs/system.jsonl
sqlite3 state/quant.db "SELECT ts, action, reason FROM decisions WHERE strategy='dca_x' ORDER BY id DESC LIMIT 20"
```
