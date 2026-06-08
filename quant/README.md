# thccb-quant

TouhouCCB 的配套量化交易 bot（实盘小额）。需要一个运行中的 TouhouCCB 后端实例。

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

浏览器登录你的实例 → F12 → Network → 找任意 `/api/v1/*` 请求 → 复制
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

## 实盘启动（后台跑，无 sudo）

**一行启动**（实盘真账号下单，清残留 + tmux + watchdog）：

```bash
cd /path/to/TouhouCCB/quant && \
rm -f state/KILL state/quant.db state/quant.db-* logs/*.jsonl && \
tmux new -d -s quant 'bash run.sh' && \
sleep 3 && pgrep -af 'python -m thccb_quant' || echo "trader 未起，看 tmux attach -t quant"
```

做了什么：
- 清掉 dry-run 或前次跑的残留（state 表 + KILL 标志 + 旧 log）
- tmux session 名 `quant`，跑 `run.sh` 看门狗（crash 30s 后重拉，clean exit 不重拉）
- `run.sh` 调 `python -m thccb_quant`（**无 `--dry-run` = 真账号下单**）

**只演练不下单**：手动跑 `python -m thccb_quant --dry-run` 即可。

**开机自启**（用户 crontab，无需 sudo）：

```cron
@reboot tmux new -d -s quant 'bash /path/to/TouhouCCB/quant/run.sh' >> /path/to/TouhouCCB/quant/logs/cron.log 2>&1
```

## 监控运行中的 trader

```bash
# 进 tmux 看 stdout（Ctrl-b d 退出不杀进程）
tmux attach -t quant

# 实时跟策略事件（jq 过滤）
tail -f /path/to/TouhouCCB/quant/logs/system.jsonl \
  | jq 'select(.event | startswith("volharvest_") or startswith("order_"))'

# 查最近下单
sqlite3 /path/to/TouhouCCB/quant/state/quant.db \
  "SELECT ts, strategy, side, shares, price, status FROM orders ORDER BY id DESC LIMIT 10"

# 查最近决策（含 skip + reason）
sqlite3 /path/to/TouhouCCB/quant/state/quant.db \
  "SELECT ts, strategy, action, substr(reason,1,40) FROM decisions ORDER BY id DESC LIMIT 20"

# 真实账号当前持仓 + 现金（脱钩策略内部状态）
cd /path/to/TouhouCCB/quant && source .venv/bin/activate && python -c "
import asyncio, httpx; from dotenv import dotenv_values
env=dotenv_values('.env')
async def go():
    async with httpx.AsyncClient(base_url=env['THCCB_BASE_URL'], timeout=10) as c:
        h={'Authorization':f'Bearer {env[\"THCCB_ACCESS_TOKEN\"]}'}
        s=(await c.get('/api/v1/user/summary', headers=h)).json()
        hd=(await c.get('/api/v1/user/holdings', headers=h)).json()
        print(f'cash={s[\"cash\"]} net_worth={s[\"net_worth\"]} pnl={s[\"unrealized_pnl\"]}')
        for x in hd: print(f'  outcome={x[\"outcome_id\"]} amt={x[\"amount\"]} avg={x[\"avg_price\"]} now={x[\"current_price\"]}')
asyncio.run(go())
"
```

## 停机（Kill Switch）

```bash
# 优雅停（推荐）：trader 跑完当前 SSE event 退出，watchdog 也退
touch /path/to/TouhouCCB/quant/state/KILL

# 硬停（兜底，可能吃当前正在下单的请求）
tmux kill-session -t quant
```

启动时若 `state/KILL` 存在则拒绝启动，要手工 `rm state/KILL` 才能重跑。

## 启动前/启动后必读

- **策略跑期间禁止手动 UI 交易该 outcome** —— 会让策略内部 `_holding` 与真实漂移；reconcile 每 5 分钟才校正一次，期间策略可能按错误持仓下单
- **VolatilityHarvest 启动会进 bootstrap mode**（持仓 < base_shares 时），每 30s 一笔 ≤10 shares 真实买入逐步补到底仓；如果价格偏高，`bootstrap_skip_if_overpriced` 会暂停补仓等回落
- **首次启动到看见主信号触发要数小时** —— window 预热需要 N 笔 SSE trade event（默认 100），thccb 低流动性下要 2-4 小时
- **滑点上限**：`risk.max_slippage_bps` 实测 ±10% 波动市场默认 300 (3%) 会大量拒单，实盘建议 800-1500

## 内置策略

- **`dca`** —— 定投，定时按 CNY 金额买入直到总预算耗尽。详见 `docs/strategies.md`
- **`grid`** —— 网格，区间内涨卖跌买。详见 `docs/strategies.md`
- **`volharvest`** —— 波动率收割（SSE 驱动 + logit 空间 mean reversion）。
  详见 `docs/strategies.md`

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
