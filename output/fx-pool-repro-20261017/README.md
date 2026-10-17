# FX 池耗尽复现证据（2026-10-17）

配套记录：[docs/fx-pool-exhaustion-repro-2026-10-17.md](../../docs/fx-pool-exhaustion-repro-2026-10-17.md)。

本目录是本机隔离复现的原始产物，**不含 token、生产数据或服务器凭证**。服务端为真实 FastAPI lifespan + 隔离 PostgreSQL 14，连接池保持默认 `10 + 20`。

## 目录

| 路径 | 内容 |
| --- | --- |
| `harness/serve2.py` | 插桩服务：外部挂 SQLAlchemy pool checkout/checkin 事件、标记 `AsyncAdaptedQueue.get`、按 await 链（`cr_await`）抓任务栈、用独立 NullPool 连接采样 `pg_stat_activity`；`REPRO_MODE=short_auth` 为反事实模式 |
| `harness/probe2.py` | 并发探针：`versions` / `trade` / `control` 三种模式 |
| `harness/srv.sh`、`harness/start_throttled.sh` | 起停脚本；后者把后端与 2 个忙循环绑同一 CPU 核 |
| `harness/seed_source.py`、`harness/fix_configs.py` | 隔离库的源市场与开关 |
| `runs/run5/` | E2 结构复现：40 并发 `trading-versions` 全部 500；含 14 个热点快照中的 2 个样本、池事件、pg 采样、错误摘要 |
| `runs/run6/` | E3 反事实：认证改用短 session，同负载 40/40 成功、0 池错误 |
| `runs/run7/` | E4 百人同款（CPU 受限）：29 个快照中 4 个样本 + `snapshot-timeline.jsonl`（每个快照的占用/锁等待人数）、池事件、pg 采样、错误摘要 |
| `runs/run4-control/`、`runs/run4-trade/` | E1 100 并发公开读、E6 30 并发成交循环 |
| `runs/run8-fix/` | E7 修复验证：三个聚合读取路由各 40 并发全部 200、池错误 0 |
| `runs/run9-fix-throttled/` | E7 修复验证：与 run7 相同的 CPU 限制与百人驱动，0 池错误、池峰值 22 |
| `driver/drive-full/` | E5 本机空闲时 100 人驱动结果 |
| `driver/drive-throttled/` | E4 CPU 受限时 100 人驱动结果（含 manifest） |
| `driver/drive-fix-throttled/` | E7 修复后同限制的百人驱动结果 |
| `probes.jsonl` | E1/E2/E3/E6 汇总指标 |

## 怎么读证据

- `runs/run5/snapshots/hot-001.json`：`holders` 里 30 个任务各持 1 条连接，`chain` 末尾是
  `read_trading_versions → authoritative_version → AsyncSession.execute → greenlet_spawn → Queue.get`。
- `runs/run7/snapshot-timeline.jsonl`：第 2 个采样起，`checkout=30`、`holder_kinds` 只有
  `waiting_market_lock` 与 `waiting_for_pool`、`lock_waiters["fx:2"]=27..28`，持续 61 秒。
- `runs/run7/snapshots/hot-002.json`：`waiting_for_pool` 持有者的链末尾是
  `refresh_groups:65 → _snapshot → AsyncSession.execute → Queue.get`，即锁持有者在借连接。
- `runs/runX/server-pool-errors.txt`：只保留行数与首条 `QueuePool limit ...`，原始日志在本机 `/tmp/sunyunbo/fx-pool-repro-20261017/`。

## 重跑要点

1. 隔离 PG（端口 55570）→ `alembic upgrade head`（临时 psycopg2 目录，未改 `backend/venv`）。
2. `python -m scripts.fx_loadtest prepare --run-id repro100 --source-pair 1 --users 100 --confirm-stopped`，再 `mint`。
3. `srv.sh start runX` 起插桩服务；`probe2.py` 或 `loadtest/fx_http.py` 打流量。
4. 复现前确保没有残留 `serve2.py` 进程，且池参数仍为 10/20。

## 边界

- 本机空闲时 100 人、2.5 交易尝试/s 不会死锁（池峰值 24），需要在 CPU 受限或用更高并发才触发；这是阈值现象，不是"100 人必然崩"。
- CPU 受限用 2 个忙循环模拟，不代表生产容量。
- 反事实 `short_auth` 仅用于验证因果，不是修复实现。
- E7 使用主工作区未提交的 `user_trading.py` 修复文件，复制进隔离 worktree 跑完后已还原；不代表该修复已提交或部署。
