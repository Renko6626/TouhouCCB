# 审计事件流（audit_event）设计

日期：2026-08-22　目的：活动期间所有改钱 / 改仓 / 改市场 / 改配置的操作落成
**一条全序、带后状态快照的事件流**，支持「给定时间窗 → 回放交易、持仓、资产、价格演化」
的离线研究与回测，并可用独立重放器与线上表交叉校验。

## 路线选择

- 纯推演（只记输入）否决：回测器与历史共用 LMSR 代码，无独立事实可校验；代码漂移让旧事件算不出当年数。
- PG 触发器行历史否决为主路线：无业务语义、q 向量散在 n 行、SQLite 开发环境跑不了。
- **采用：应用层事件 + 每事件后状态快照**。T 时刻状态 = 各实体 `id ≤ cutoff` 的最后一条事件的 `*_after`。

## 表 `audit_event`

| 列 | 类型 | 说明 |
|---|---|---|
| id | bigint pk autoincrement | 全局序号。同一实体（user 行 / market）上的写入被行锁或 writer 串行化，因此**按实体的 id 序 = 提交序**；跨实体只保证近似 |
| ts | timestamptz idx | 写入时刻 |
| event_type | varchar(32) idx | 见下表 |
| user_id / market_id / outcome_id | int null idx | 关联实体 |
| operator_user_id | int null | 管理员操作者 |
| ref_table / ref_id | varchar(32) / int null | 指向 transaction / ledger_entry / liquidation_events 等细表 |
| payload | JSON | 输入与中间量（Decimal 以字符串存） |
| user_after | JSON null | `{cash, debt, debt_last_accrued_at}` |
| position_after | JSON null | `{amount, cost_basis}`（该 user × outcome） |
| market_after | JSON null | `{outcome_ids, q, b, prices, status}`——**全向量** |

索引：`(user_id, id)`、`(market_id, id)`、`(event_type, id)`。

## 事件类型与埋点位置

| event_type | 位置 | payload 要点 | 快照 |
|---|---|---|---|
| user_register | auth.py ×2 | initial_balance | user |
| trade_buy / trade_sell | writer_ops + market.py legacy | shares, gross, fee, fee_rate, price, pre/post_mp | user, position, market |
| trade_liquidate | writer_ops.op_liquidate_market + liquidation_service legacy | 同上 + mode | user, position, market |
| settle_win / settle_lose | op_resolve + legacy resolve | shares, payout_unit, pay | user, position(amount=0) |
| market_create / close / resume / settle | market.py + writer_ops | b, outcome labels / winning_outcome_id, payout_unit, total_payout | market |
| loan_borrow / loan_repay / admin_adjust_cash / admin_force_loan / admin_forgive_debt / admin_amnesty | ledger_service.record_entry 内统一发 | cash_delta, debt_delta, rate, interest_accrued, reason | user |
| liquidation_repay | liquidation_service（两条路径）| repay_amount | user |
| liquidation | liquidation_service（两条路径）| LiquidationEvent 全字段 | user |
| interest_accrual | loan_sweep | debt_before, debt_after, rate, elapsed_sec | user |
| admin_set_role / admin_ban / admin_unban | admin_user_service | 前后值 | — |
| config_set | site_config.set_value | key, old, new | — |
| redeem_purchase / danmuku_exchange | redemption / danmuku services | amount | user |

借/还/管理员操作路径里隐式结的利息，随 `interest_accrued` 进对应事件 payload，不单独发 interest_accrual。

## 工具

- `backend/scripts/audit_export.py --from --to [--out DIR]`：导出 events.jsonl + user_state.csv + market_state.csv（按事件逐行展开的后状态）；`--at T` 打印 T 时刻全量快照。
- `backend/scripts/audit_verify.py`：从 seq=1 折叠事件流，与线上 user.cash/debt、position、outcome.total_shares 比对，差异即 bug 或漏记。

## 不做

- 不改现有表；不引入 PG 触发器；不做前端页面（研究走导出文件）。
- 事件失败不静默：与业务同事务，写不进事件 = 业务回滚。
