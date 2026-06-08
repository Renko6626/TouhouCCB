# 可配置经济参数 spec — 2026-06-08

分支 `feat/2026-06-08-configurable-economy`（栈在 docs 分支上）。

**目标**：把「新用户初始余额」和「卖出手续费率」从硬编码改为 admin 可热配（走现成 site_config）。

## 决策（已批准）
- 只做**卖出**手续费（买入仍无费）
- 校验：`sell_fee_rate ∈ [0, 0.2)`、`initial_balance ∈ [0, 1_000_000]`
- `User.cash` 模型死默认 `500` → `0`（仅 `User()` 不传 cash 时的 fallback；注册总显式传值）

## 两个新 site_config key
| key | 类型 | 默认 | 校验 |
|-----|------|------|------|
| `sell_fee_rate` | decimal | `0`（行为不变） | `0 ≤ v < 0.2` |
| `initial_balance` | decimal | `settings.INITIAL_BALANCE`(100) | `0 ≤ v ≤ 1_000_000` |

## 安全读取
新增 `site_config.get_decimal_or(session, key, default)`：key 缺失返回 default（不抛）。
所有读 sell_fee_rate / initial_balance 的地方用它，key 未 seed 时回落 0 / 100，
保证现有测试与冷启动行为不变。

## 接线
- `loan_migrate.DEFAULT_CONFIGS`：seed 两个 key（initial_balance 取 settings.INITIAL_BALANCE）
- `site_config.py`(api) `_WHITELIST` + `_validate`：加两 key + 区间校验
- `auth.py:182` 注册 cash：`settings.INITIAL_BALANCE` → `get_decimal_or(db,"initial_balance",默认)`
- `market.py` 卖出两处(735/1055)：读 `get_decimal_or(db,"sell_fee_rate",0)`，删常量 `SELL_FEE_RATE`
- `wealth.py`：`compute_users_holdings_value` sentinel 分支改读 config（删 `_get_sell_fee_rate`/`_CACHED_*`）
- `user.py:175`、`admin_stats.py:47`：改读 config
- `models/base.py` cash 默认 500→0 + `rank.py:14` 过期注释
- 前端 `/admin/site-config` 页：确认两新 key 可见可改（实现时查）

## TDD 顺序
get_decimal_or → 配置 key（whitelist/默认/校验）→ LCV 扣费 + 缺失回落 → 接线 → 全套回归。
默认值下行为零变更（fee=0/balance=100），现有 353 测试应全过。
