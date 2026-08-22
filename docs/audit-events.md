# 审计事件流（audit_event）

> 设计背景：`docs/superpowers/specs/2026-08-22-audit-events-design.md`。
> 目标：给定任意时间窗，完整回放交易、持仓、资产、价格的演化；可独立校验；可导出做回测。

## 1. 一句话模型

每个改钱 / 改仓 / 改市场 / 改配置的操作，在**同一事务**里向 `audit_event` 追加一行：
`payload`（输入与中间量）+ 操作后快照（`user_after` / `position_after` / `market_after`）。

**T 时刻状态 = 各实体 `id ≤ cutoff` 的最后一条事件的 `*_after`。** 不用重放、不依赖 LMSR 代码。

`id` 是全局序号。同一 user 行的写入被 `SELECT … FOR UPDATE` 串行化、同一市场的写入被 writer 串行化，
因此**按实体的 id 序 = 提交序**；跨实体（A 用户和 B 市场之间）只保证近似同序，研究时以实体内顺序为准。

## 2. 事件类型

| event_type | 触发点 | payload 要点 | 快照 |
|---|---|---|---|
| `user_register` | Casdoor / dev-login 首次登录 | username, is_superuser, source | user |
| `trade_buy` / `trade_sell` | `/market/buy` `/market/sell`（writer 与 legacy 两条路径都记，`payload.path` 区分） | shares, cost, gross, fee, **fee_rate**, price, pre/post_market_price | user, position, **market（全向量 q / b / prices / status）** |
| `trade_liquidate` | 强平逐仓位卖出 | 同上 + mode, partial_pct | user, position, market |
| `settle_win` / `settle_lose` | 结算逐仓位 | shares, payout_unit, cost(=−pay) | user, position(amount=0) |
| `market_create` / `market_close` / `market_resume` / `market_settle` | 市场生命周期 | create: title, liquidity_b, outcomes；settle: winning_outcome_id, payout_unit, total_payout | market |
| `loan_borrow` / `loan_repay` | 用户借还 | cash_delta, debt_delta, daily_rate, **interest_accrued**（本次操作前隐式结进 debt 的利息） | user |
| `admin_adjust_cash` / `admin_force_loan` / `admin_forgive_debt` / `admin_amnesty` | 管理员资金操作（经 `ledger_service.record_entry` 自动发） | 同上 + reason；operator_user_id | user |
| `interest_accrual` | **定时结息 sweep**（`loan_sweep.run_sweep_once`，每个被结息用户一条） | debt_before, debt_after, interest, daily_rate, elapsed_sec | user |
| `liquidation_repay` | 强平后自动还债（不走 ledger，这里单独记） | repaid, interest_accrued, trigger_source | user |
| `liquidation` | 强平汇总（= `LiquidationEvent` 全字段，`ref_id` 指向它） | pre_*/post_* | user |
| `admin_set_role` / `admin_ban` / `admin_unban` | 账号管理 | before/after, reason | — |
| `config_set` | `PUT /admin/site-config/{key}` | key, old, new | — |
| `redeem_purchase` / `danmuku_exchange` | 兑换 / 弹幕扣款 | amount, … | user |

`cost` 口径与 `Transaction.cost` 一致：buy 为 +支出，sell / liquidate / settle 为 −收入，
所以任何交易事件都满足 `cash_after = cash_before − cost`。

**利息**：显式结息只有 sweep 的 `interest_accrual`；借/还/强平/大赦路径里顺带结的利息随各自事件的
`interest_accrued` 字段进账，`debt_after = debt_before + interest_accrued + debt_delta`。

Decimal 一律以字符串存 JSON（不丢 6/8 位精度）；时间 ISO 字符串。

## 3. 工具

```bash
cd backend   # DATABASE_URL 指向目标库。生产数据请在 pg_dump 恢复出的本地库上跑，不要连生产主库

# 自检：从 seq=1 折叠 + 独立增量校验 + 与线上 user/position/outcome 表比对。非 0 退出 = 有不一致
python scripts/audit_verify.py

# 导出时间窗（带时区）→ events.jsonl / user_state.csv / position_state.csv / market_state.csv / snapshot_start.json
python scripts/audit_export.py --from 2026-08-25T00:00:00+08:00 --to 2026-08-26T00:00:00+08:00 --out ./export

# 打印 T 时刻全量快照
python scripts/audit_export.py --at 2026-08-25T12:00:00+08:00
```

`audit_verify.py` 做的是**独立重放**：用事件的输入（shares / cost / delta / interest）从上一状态推出本事件应得的状态，
与事件自带的快照比对——它不调用 LMSR，所以能发现写路径 bug 或漏记事件，而不是把同一份代码再算一遍。
增量校验只对「锚定」实体生效（从 `user_register` / `market_create` 起有完整记录）；审计上线前已存在的用户/市场
只做快照对齐与线上比对，统计里会标出 anchored 数。

## 4. 回测怎么用导出

- `market_state.csv` 每笔交易后每个 outcome 一行（q、price、b、status）：在任意 `event_id` 处拿到 `(q, b, fee_rate)` 就能离线算反事实价格与滑点（`app/services/lmsr.py` 可直接 import）。
- `position_state.csv` + `user_state.csv`：任意玩家在任意事件点的仓位 / 现金 / 债务；估值用同一事件点的 q 向量算 LCV。
- `snapshot_start.json`：窗口起点的全量状态，回放窗口从它开始。
- 同一 `event_id` 在三个 csv 里是同一事务。

## 5. 给开发者的约束

新增任何会改 `user.cash/debt`、`position.amount`、`outcome.total_shares`、`market.status`、`site_config` 的写路径：
在同一事务里调 `audit_service.record(...)`（交易用 `record_trade`），快照在业务值写完**之后**取。
漏了会被 `scripts/audit_verify.py` 和 `tests/test_audit_events.py` 的线上表比对抓出来。
`AUDIT_EVENT_TYPES` 是白名单，新类型先加到 `models/audit.py` 并在本文档 §2 补一行。
