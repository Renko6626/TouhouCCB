# Anti-bot 系统设计 spec

> **Status**: Approved
> **Owner**: Renko6626
> **Created**: 2026-05-20
> **Related**:
> - `docs/development.md`（栈约束）
> - `docs/style.md`（设计系统）
> - `docs/schema-conventions.md`（schema 工程约定）
> - `quant/` 自有量化脚本

## Goal

防止外部脚本破坏散户体感，但保留自己 quant 脚本的运行能力。三层防护：

1. **L1 ToS 公示**：法律护栏 + 心理威慑
2. **L2 HMAC client_token**：技术门槛（活动期间临时启用）
3. **L4 行为监控**：事后人工 review + 封号

> 注：L3 是"白名单"，跟 L2/L4 组合使用，不是独立层。

**关键设计选择**：**默认 L2 关、L4 开**，部署上线 0 行为变化；活动当天 admin 一键开 L2 + 配白名单。

## 非目标 (Non-Goals)

- 不追求"绝对防 bot"（不可能；bearer token 在 bundle 可见）
- 不阻挡所有脚本（自己的 quant 必须能跑）
- 不做 CAPTCHA / WebAuthn / 设备指纹（hobby 站 over-engineer）
- 不做自动封号（误伤代价高，admin 人工决定）

## Architecture overview

```
┌─────────────────────────────────────────────────────────┐
│ L1: ToS + 公示 (docs/tos.md "禁脚本，保留封号权")        │
└─────────────────────────────────────────────────────────┘
        ↓
┌─────────────────────────────────────────────────────────┐
│ L2: HMAC client_token (活动期间临时启用)                │
│   前端 vite VITE_CLIENT_TOKEN_SECRET → SubtleCrypto    │
│   后端 .env CLIENT_TOKEN_SECRET → anti_bot.py 验证      │
│   FastAPI Depends on /market/{buy,sell,quote}           │
│   site_config: activity_mode_enabled (默认 false)       │
└─────────────────────────────────────────────────────────┘
        ↓ (L2 通过/绕过 同时)
┌─────────────────────────────────────────────────────────┐
│ L3: User ID 白名单 (跟 L2 / L4 配合)                    │
│   site_config: quant_whitelist_user_ids = "1,5"  csv    │
│   - L2 检查时：whitelist user 直接通过                  │
│   - L4 扫描时：跳过 whitelist user，避免噪音            │
└─────────────────────────────────────────────────────────┘
        ↓
┌─────────────────────────────────────────────────────────┐
│ L4: 行为监控 (后台异步，与 L2 独立)                     │
│   APScheduler 每 30 min 扫近 2 hour Transaction         │
│   信号: high_freq / late_night / regular_interval       │
│   触发 → 写 bot_suspicion 表（含 metrics 快照）          │
│   SQLAdmin auto-CRUD: 列表 + 改 review_status           │
└─────────────────────────────────────────────────────────┘
```

**核心交互点**：

- L2 + L3 在 `/market/{buy,sell,quote}` 路径上，**同步阻断**
- L4 在 scheduler 异步运行，**完全独立** —— 即使 L2 关闭，L4 仍写记录给 admin 看

## 数据模型

### 新表 `bot_suspicion`

```python
class BotSuspicion(SQLModel, table=True):
    __tablename__ = "bot_suspicion"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    triggered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
        index=True,
    )
    # 触发信号类型，csv 拼接（多信号一起触发）
    # e.g. "high_freq,late_night"
    signals: str = Field(max_length=200)

    # 信号具体数值快照（JSON 字符串），admin review 时看
    # e.g. {"total_trades": 145, "late_night_trades": 23,
    #       "interval_stddev_ms": 56, "round_amount_rate": 0.95}
    metrics: str = Field(max_length=500, default="{}")

    # 触发窗口（admin 知道是看了哪段时间）
    window_start: datetime = Field(sa_type=DateTime(timezone=True))
    window_end: datetime = Field(sa_type=DateTime(timezone=True))

    # admin 处理状态
    review_status: str = Field(default="pending", max_length=20)
    # "pending" / "confirmed_bot" / "false_positive" / "whitelisted"

    reviewed_by: int | None = Field(default=None, foreign_key="user.id")
    reviewed_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    review_note: str | None = Field(default=None, max_length=500)
```

**索引**：
- `user_id`（按用户聚合）
- `triggered_at` desc（admin 列表默认排序）

**No FK CASCADE on `user_id` / `reviewed_by`** —— 历史记录在用户删除后仍要保留（跟 `LiquidationEvent` 同款约定）。

### `User` 表无改动

白名单走 site_config csv，不用 `is_quant_whitelisted: bool` 列（YAGNI；用户量小，csv 够）。

### Site_config 新 keys（共 11 个）

| Key | Type | 默认 | 含义 |
|---|---|---|---|
| `activity_mode_enabled` | bool | `false` | L2 HMAC 总开关 |
| `quant_whitelist_user_ids` | str | `""` | L3 csv "1,5,12" |
| `bot_detection_enabled` | bool | `true` | L4 scheduler 开关 |
| `bot_detection_interval_sec` | int | `1800` | 30 min 扫一次 |
| `bot_detection_window_sec` | int | `7200` | 每次扫近 2 小时 |
| `bot_freq_threshold` | int | `120` | 2h 窗口内 ≥ 120 笔 |
| `bot_late_night_threshold` | int | `20` | 03-06 时段 ≥ 20 笔 |
| `bot_interval_stddev_ms_threshold` | int | `100` | 间隔 stddev < 100ms |
| `bot_fast_follow_trigger_cost` | decimal | `500.0` | "大额交易"门槛（金/笔），≥ 此值算作 trigger event |
| `bot_fast_follow_latency_ms` | int | `1000` | 跟进延迟阈值，user 在 trigger 后 < 此值下单 → 算一次 fast_follow |
| `bot_fast_follow_count_threshold` | int | `3` | 窗口内 ≥ 此次数 fast_follow → 触发信号 |

由 `app/services/loan_migrate.py::DEFAULT_CONFIGS` 在启动时种入，`app/api/v1/site_config.py::_WHITELIST` 添加可写白名单。

### .env 新变量

```bash
# 后端
CLIENT_TOKEN_SECRET=<32-byte random hex>

# 前端 build（GitHub Actions secrets + vite build env）
VITE_CLIENT_TOKEN_SECRET=<同上>
```

通过 GitHub Actions secrets 注入 CI build，服务器 .env 单独维护。详见"部署"章节。

## 关键流程

### L2 HMAC 验证

**前端** (`thccb-frontend/src/utils/clientToken.ts`)：

```typescript
async function generateClientToken(uid: number): Promise<{token: string, ts: number}> {
  const ts = Math.floor(Date.now() / 1000)
  const secret = import.meta.env.VITE_CLIENT_TOKEN_SECRET
  const msg = `${ts}|${uid}`
  const key = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(secret),
    {name: 'HMAC', hash: 'SHA-256'}, false, ['sign']
  )
  const sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(msg))
  const hex = Array.from(new Uint8Array(sig))
    .map(b => b.toString(16).padStart(2, '0')).join('')
  return {token: hex, ts}
}
```

**前端 axios interceptor** (`src/api/index.ts`)：

```typescript
api.interceptors.request.use(async (cfg) => {
  if (cfg.url?.match(/^\/api\/v1\/market\/(buy|sell|quote)/)) {
    const uid = authStore.user?.id
    if (uid) {
      const {token, ts} = await generateClientToken(uid)
      cfg.headers['X-Client-Token'] = token
      cfg.headers['X-Client-TS'] = String(ts)
    }
  }
  return cfg
})
```

**后端** (`backend/app/services/anti_bot.py`)：

```python
import hmac, hashlib, time
from app.core.config import settings

def verify_client_token(token: str, ts: str, uid: int) -> bool:
    if not settings.CLIENT_TOKEN_SECRET:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    # 时间窗 30s（容差客户端时钟漂移）
    if abs(time.time() - ts_int) > 30:
        return False
    expected = hmac.new(
        settings.CLIENT_TOKEN_SECRET.encode(),
        f"{ts}|{uid}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, token)


def parse_whitelist(csv_str: str) -> set[int]:
    """解析 'a, 1, 5,12,abc' → {1, 5, 12}"""
    return {
        int(x.strip()) for x in csv_str.split(",")
        if x.strip().isdigit()
    }
```

**FastAPI Dependency** (`backend/app/api/v1/market.py`)：

```python
async def verify_anti_bot(
    request: Request,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
) -> User:
    if not await site_config.get_bool(db, "activity_mode_enabled"):
        return user

    whitelist_csv = await site_config.get_str(db, "quant_whitelist_user_ids", "")
    if user.id in parse_whitelist(whitelist_csv):
        return user

    token = request.headers.get("X-Client-Token")
    ts = request.headers.get("X-Client-TS")
    if not token or not ts:
        raise HTTPException(403, "缺少 anti-bot 验证头（活动期间禁止脚本访问）")

    if not verify_client_token(token, ts, user.id):
        raise HTTPException(403, "anti-bot 验证失败")
    return user
```

把 `Depends(verify_anti_bot)` 加到 `/market/buy` `/market/sell` `/market/quote` 三个端点。

### L4 行为监控扫描

`backend/app/services/bot_detection.py`：

```python
async def run_bot_detection_once() -> dict:
    """每 30 min 一次:
    1. site_config 读 enabled / window / 阈值
    2. enabled=false → 直接返回 {"skipped": "disabled"} 完整字段
    3. 拉近 window_sec 的 Transaction (joined user, 跳过 whitelist)
    4. 按 user_id 分组算信号
    5. 任一信号触发 → 检查 6h 内同信号 pending 记录 → 无则写新
    """
```

**信号算法**：

- **high_freq**: 窗口内总笔数 ≥ `bot_freq_threshold`
- **late_night**: 03-06 时段（Asia/Shanghai）笔数 ≥ `bot_late_night_threshold`
- **regular_interval**: ≥ 3 笔交易 + 相邻间隔的 stddev_ms < `bot_interval_stddev_ms_threshold`（少于 3 笔跳过）
- **fast_follow**: 检测"大额交易后毫秒级跟进"（bot 特征）
  1. 找出窗口内所有"trigger 交易"：`abs(cost) ≥ bot_fast_follow_trigger_cost` 且 **trigger 不是当前 user 自己的交易**（自己跟自己不算）
  2. 对每个 trigger 交易，找当前 user 在该 trigger 之后 `bot_fast_follow_latency_ms` 内同 market_id 的交易（最近一笔）
  3. 计算这种"跟进事件"次数
  4. ≥ `bot_fast_follow_count_threshold` 次 → 触发
  5. 实现：trigger 数组按 timestamp 排序，user 交易也排序，双指针扫

**不重复写**：
- 检查 `BotSuspicion WHERE user_id=? AND signals like '%<signal>%' AND review_status='pending' AND triggered_at > now - 6h`
- 有则跳过，避免数据库爆炸

**APScheduler 集成**（仿 `loan_sweep` / `liquidation_sweep`）：

```python
# bot_detection.py
async def start_scheduler() -> None: ...
async def stop_scheduler() -> None: ...
async def reschedule(interval_sec: int) -> None: ...
```

`app/main.py` lifespan startup/shutdown 各加一行 await。

`backend/tests/conftest.py` 的 `_disable_scheduler` fixture 加 patch
`app.main.start_bot_detection_scheduler` + `stop_bot_detection_scheduler`。

### SQLAdmin 集成

`backend/app/core/admin.py`：

```python
class BotSuspicionAdmin(ModelView, model=BotSuspicion):
    name = "Bot 嫌疑记录"
    column_list = [
        BotSuspicion.id, BotSuspicion.user_id, BotSuspicion.triggered_at,
        BotSuspicion.signals, BotSuspicion.review_status,
    ]
    column_searchable_list = [BotSuspicion.user_id, BotSuspicion.signals]
    column_sortable_list = [BotSuspicion.triggered_at, BotSuspicion.user_id]
    column_filters = [BotSuspicion.review_status, BotSuspicion.signals]
    # 默认按 triggered_at desc 排
```

复用现有 SQLAdmin auth (JWT cookie + admin role)，无新增。

admin 工作流：进 SQLAdmin → BotSuspicion 列表 → 改 `review_status` + 填 `review_note` → save → 手动决定是否封号 / 加 quant_whitelist。

## 错误处理 + edge cases

### SECRET 缺失

| 场景 | 后端行为 | 前端行为 |
|---|---|---|
| `.env` 没设 + `activity_mode=false` | 启动 log WARNING，运行正常 | bundle SECRET 空 string，反正后端不验 |
| `.env` 没设 + `activity_mode=true` | 启动 log ERROR；所有 buy/sell 403（含白名单要在 verify_anti_bot 之前判，所以白名单照常通过）| 前端发 token 但永远验不过 |
| 前后端 SECRET 不一致 | 验证全部失败 → 非白名单全员 403 | 用户点不了 buy/sell |

**应对**：
- 后端启动时检查：`if activity_mode_enabled and not CLIENT_TOKEN_SECRET: logger.error(...)`
- deploy.sh 加 SECRET hash sanity check

### 客户端时钟漂移

时间窗 ±30s，少数错时钟用户 403。

**应对**：前端 catch 403 显示"系统时间可能不准"提示，先不做服务端时间同步端点（YAGNI）。

### 白名单边界

- `""` → empty set ✓
- `"1,5,abc"` → `{1, 5}` ✓
- `" 1 , 5 "` → `{1, 5}` ✓（strip + isdigit）
- 白名单 user **跳过 L2** + **跳过 L4 扫描**（避免自己 quant 频繁刷被记录）

### L4 重复写防御

- 同 user 同信号 6h cooldown
- 比扫描 interval（30 min）长得多
- 同一爆刷 → 1 条记录

### L4 数据稀疏

- 凌晨 03-06 窗口内可能只 1-2 笔交易：
  - high_freq 阈值 ≥ 120：不触发 ✓
  - late_night 阈值 ≥ 20：不触发 ✓
  - regular_interval：< 3 笔跳过 ✓

### Scheduler 重叠

APScheduler `max_instances=1` 防同 job 重叠（跟 loan_sweep / liquidation_sweep 同款）。

### Hot path 性能

`verify_anti_bot` Dependency：
- `site_config.get_bool` / `get_str` 进程内 cache，命中 < 1μs
- HMAC-SHA256 单次计算 < 100μs
- 总 overhead < 200μs，可忽略

**不写专门 benchmark**（YAGNI；如果 prod 卡顿再加）。

### 数据保留

`bot_suspicion` 表只增不删。**先不做自动清理**，未来加 cron 删 30 天前 `review_status != 'pending'` 行。

## 测试矩阵

### `backend/tests/test_anti_bot.py`（unit）

- `verify_client_token`：有效 / 过期 / 篡改 / 错 user / SECRET 空
- `parse_whitelist`：空 / 含非数字 / 含空格

### `backend/tests/test_market_anti_bot_integration.py`（端到端）

- `activity_mode=false` → /market/buy 不带 header 通过
- `activity_mode=true` + 非白名单 + 无 header → 403
- `activity_mode=true` + 非白名单 + 有效 token → 通过
- `activity_mode=true` + 白名单 user + 无 header → 通过
- `activity_mode=true` + 篡改 token → 403

### `backend/tests/test_bot_detection.py`

- high_freq：合成 120 笔/2h → 触发
- late_night：合成 20 笔 03-06 → 触发
- regular_interval：stddev < 100ms → 触发；> 100ms → 不触发
- < 3 笔 → regular_interval skip
- fast_follow：合成 5 个 trigger 大单 + user 在每个后 500ms 内跟进 → 触发
- fast_follow 自跟进不算：user 自己的大单后 100ms 自己再交易 → 不触发
- fast_follow 跨 market：trigger 在 market A，user 在 market B 跟进 → 不算（同 market_id 才算）
- 白名单 user → 不参与扫描
- 6h 内同信号重复 → 不重写
- enabled=false → tick 直接 return 完整 schema（review I-1 教训）

### `backend/tests/test_admin_bot_suspicion.py`

- BotSuspicion ModelView 列表渲染
- review_status 改 'confirmed_bot' 能 save

**总数**：约 25 个新 test。

## 部署步骤

### 1. 生成 SECRET（一次性）

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 2. GitHub Actions secrets

在 https://github.com/Renko6626/TouhouCCB/settings/secrets/actions 加：
- `CLIENT_TOKEN_SECRET` = 上面生成的值
- `VITE_CLIENT_TOKEN_SECRET` = 同上

### 3. 服务器 .env

SSH 到 prod 编辑 `/home/deploy/TouhouCCB/.env`：
```bash
CLIENT_TOKEN_SECRET=<生成的值>
```

### 4. 修改 `.github/workflows/ci.yml`

前端 build job 加 env 注入：
```yaml
- name: Build
  run: npm run build
  env:
    VITE_CLIENT_TOKEN_SECRET: ${{ secrets.VITE_CLIENT_TOKEN_SECRET }}
```

### 5. Alembic migration

代码包含一个 migration 文件（建 bot_suspicion 表）。CI deploy.sh 自动跑 `alembic upgrade head`。

### 6. PR merge 到 main → CI 自动部署

merge 后 5-10 分钟部署完成。

### 7. 上线后默认状态

- `activity_mode_enabled` = false → L2 静默
- `bot_detection_enabled` = true → L4 自动跑
- `quant_whitelist_user_ids` = "" → 空白名单

**用户体验完全不变**。L4 在背后默默写 BotSuspicion 表。

### 8. 活动当天

1. SQLAdmin → site_config → 加自己 quant 的 user_id 到 `quant_whitelist_user_ids`
2. 改 `activity_mode_enabled` = true
3. 活动结束改回 false

## 回滚方案

- **最快**：admin UI `activity_mode_enabled` 改 false → buy/sell 立刻恢复（不需要重启）
- **保底**：CI revert 上一个 deploy

## 风险评估

| 风险 | 概率 | 影响 | 应对 |
|---|---|---|---|
| L2 SECRET 配置错（前后端不匹配）| 中 | 全员 buy/sell 挂 | 部署前 sanity check + 一键 disable activity_mode |
| L4 误报（正常用户被记 BotSuspicion）| 高 | admin 看到要 review，但不影响用户 | admin 改 `false_positive` 标记，调阈值 |
| L4 漏报（真 bot 没触发任何信号）| 中 | 散户受影响 | 上线后调阈值；信号可后续迭代加新规则 |
| bundle SECRET 被逆向 | 高（必然）| L2 失效，但 L4 兜底 | 接受现实，依赖 L4 + 人工封号 |
| `bot_suspicion` 表膨胀 | 低（短期）| 长期可能积累几万行 | 后续加自动清理 cron |

## 后续迭代（不在本 spec 范围）

- **新信号**：用户行为分析模型（同一 IP 多账号、注册时间集中、price impact 反向跟进）
- **自动清理**：cron 删 30+ 天 `review_status != 'pending'`
- **CAPTCHA**：仅在 BotSuspicion 多次触发同 user 时按钮启用
- **风控分数**：不只 binary signal，给 user 算 bot 嫌疑 score 然后阈值分级
- **数据保留**：考虑 GDPR 等合规要求（hobby 站暂无）

## 参考

- 现有架构：`docs/holdings-value-semantics.md`, `docs/schema-conventions.md`
- 类似 scheduler 实现：`backend/app/services/loan_sweep.py`, `backend/app/services/liquidation_sweep.py`
- 业界对照：Steam trading hold / Robinhood ToS-only / Polymarket no-op
