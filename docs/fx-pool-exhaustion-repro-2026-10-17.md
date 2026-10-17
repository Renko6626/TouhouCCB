# FX 百人交易连接池耗尽：隔离复现与调用链（2026-10-17）

对 [fx-loadtest-incident-2026-10-17.md](fx-loadtest-incident-2026-10-17.md) 交接项的第一项验证：在隔离 PostgreSQL、真实 FastAPI 路由、真实连接池（默认 `DB_POOL_SIZE=10`、`DB_MAX_OVERFLOW=20`）下复现应用池耗尽，并定位持有连接的调用链。

**结论：已复现，且与生产现象同签名。** 根因不是数据库或 SSE 数量，而是"认证用的请求 session 在请求结束前一直持有一条连接，处理函数又用 `session.bind` 新建 session 再借第二条连接"；当第一层占用达到 30 条时，30 个请求全部等第二条连接，谁也不释放第一条，形成硬死锁，直到 30 秒池超时逐个 500。市场锁（`PricingRegistry.refresh_groups` 先持锁再借连接）是次要放大项，在百人同款复现中同时出现。

本轮未修改任何产品代码，未连接生产库，未改线上连接池；同日在主工作区出现的短认证 session 修复已用同一 harness 独立验证（E7），验证用的临时复制已还原。业务代码为隔离 worktree `/tmp/sunyunbo/thccb-fx-loadtest-20261017`（分支 `ralph/2026-10-17-fx-loadtest`，`bde43b0`），其 `backend/` 与线上镜像 `ce09c41` 完全一致（`git diff ce09c41 bde43b0 -- backend` 为空）。

## 复现结果一览

| 实验 | 负载 | 结果 |
| --- | --- | --- |
| E1 控制组 | 100 并发 `GET /api/v1/fx/pairs`（无认证、无嵌套 session） | 100/100 成功，0.36s；池一度占满 30 但立即排空 |
| E2 结构复现 | 40 并发 `GET /api/v1/user/trading-versions` | **40/40 HTTP 500**，墙钟 30.47s；服务端 `QueuePool limit of size 10 overflow 20 reached` |
| E3 反事实 | 同 E2，仅把认证改为独立短 session | **40/40 成功**，0.47s，池错误 0 |
| E4 百人同款（CPU 受限） | 真实驱动 100 人 / 200 SSE，正常阶段 2.5 交易尝试/s | 74 个循环、20 笔成交、349 个请求后按"错误率>5%"中止；**30/30 连接、30 条 `idle in transaction`、锁等待者 27** |
| E5 百人无 CPU 限制 | 同驱动、本机空闲 | 正常阶段 306/308 成交、19.4 请求/s、池峰值 24，未死锁；活跃阶段成交 P99>2s 触发保护 |
| E6 成交并发 | 30 个并发现金买入循环（quote→trade→context） | 30/30 成功，成交 P50 668ms、最大 1165ms，无池错误（只有行锁排队） |
| E7 修复验证 | 主工作区同日修复（三个聚合读取改短认证 session）复制到隔离 worktree 后重跑 E2/E4 | 40 并发三路由全部 200、0 池错误；同 CPU 限制的百人驱动 0 超时/0 5xx、池峰值 22（复现时 30） |

E4 与生产 2026-10-17 百人轮的对照（同一驱动、同一人数、同一速率）：

| 正常阶段 | E4 本地（CPU 受限） | 生产百人 |
| --- | ---: | ---: |
| 实际循环数 | 74 | 75 |
| 客户端确认成交 | 20 | 10 |
| 完成 HTTP | 349 | 274 |
| `versions` 成功/超时 | 53 / 95 | 32 / 94 |
| `trade` 成功/超时 | 20 / 9 | 10 / 11 |
| 报价被阻断的循环 | 45 | 54 |
| 成交 P95（成功样本） | 4915ms | 4422ms |
| 私人 SSE 读超时 | 79 | 75 |
| 中止原因 | 滚动 30s 错误率 > 5% | 滚动 30s 错误率 > 5% |
| 服务端池错误 | `size 10 overflow 20` | `size 10 overflow 20` |

本地用 2 个忙循环与后端同绑一个核（后端约 1/3 核），生产当时后端 Docker CPU 峰值 86.5%（约 1 核的 0.87）。两者的绝对算力不能等同，但"同一到达速率下延迟升高 → 在途请求超过 30 → 池死锁"的因果链相同。

## 调用链（已由任务栈证实）

1. `backend/app/core/users.py:63` `get_current_user` 的 `db: AsyncSession = Depends(get_async_session)` 是**请求级 session**；`await db.get(User, user_id)` 开启事务并占用一条连接，直到响应结束才随依赖清理归还（`backend/app/core/database.py:38`）。
2. `backend/app/api/v1/user_trading.py:22/26/30` 的 `context` / `versions` / `prices` 把这个 session 传给读取函数。
3. `backend/app/services/credit/trading_context.py:84-91`（`read_trading_versions`，同样模式见 `:68-79` context、`:93-99` prices）用 `async_sessionmaker(session.bind)` **新建 session**，因此同一请求在仍持有第 1 条连接时再借第 2 条。
4. 读取路径进入 `backend/app/services/trading_prices.py:59-66` `refresh_groups`：先 `async with lock`，再 `async with self.session_factory() as db` —— **持锁后再借连接**。

死锁现场（E4 第 2 个快照起，连续 61 秒不变）：

- 池 30/30 占满，持有者 30 个任务各持 1 条连接（`multi=1`）。
- 26–27 个持有者在 `refresh_groups:63` 等 `fx:2` 的市场锁（`asyncio.Lock` 等待者 27–28）。
- 3–4 个持有者（含锁持有者）在 `refresh_groups:65 → _snapshot → AsyncSession.execute → Queue.get` 等第 31 条连接。
- 数据库侧：30 条 `idle in transaction`、活跃连接≈0（生产当时是 32 条连接、最多 5 个 active、1 个锁等待）。

即：**锁持有者已经持有一条连接，却需要再借一条才能完成读取；其余 27 个持锁等待者也各占一条连接；30 条连接全部无法释放，零进展。**

E2 是最小复现：不涉及市场锁（快照里 `locks` 为空），仅 30 个请求的"认证连接 + 嵌套连接"就足以把池锁死；池只能靠 30 秒超时逐个释放。E2 服务端日志出现 80 次 `QueuePool limit...`（含同一异常堆栈的重复输出，不能当作 80 个独立失败请求；与生产报告"740 次"的统计口径一致）。

E3 反事实把认证改成独立短 session（`serve2.py` 内 `REPRO_MODE=short_auth`，仅 harness 覆盖，不改产品代码）后，同样的 40 并发全部成功、池错误 0，直接证明"请求 session 在嵌套读取期间占着连接"是原因，而不是并发数本身。

## 为什么 50 人能过、100 人崩

死锁阈值是"同时持有第一层连接的请求数达到池上限 30"。50 人时在途请求低于 30；100 人时只要单条请求耗时升到 2–4 秒（生产 `versions` 正常样本 P95 已 3.6s），在途就超过 30，随即进入上述零进展状态。E5 显示本机空闲时 100 人正常阶段池峰值仅 24，所以没有复现；E4 施加 CPU 压力后立刻崩溃，两者共同说明这是延迟/在途驱动的阈值，而不是"人数=100"本身。

另外，生产报告里客户端 184 个 15 秒超时意味着故障期间服务端同时有数十个未完成请求，超过 30 条连接上限，与上述机制一致。

## 与"调大连接池"的关系

本复现给的是 2 倍关系：每个请求在嵌套读取期间最多占用 2 条连接，池上限 N 时，只要同时有 N 个请求进入嵌套读取就会死锁。把 10+20 改成 20+20 只是把阈值从 30 推到 40，在同样的延迟放大下仍会到达。**调大连接池不是修复**，也不该作为第一手段；先消除嵌套借连接，再谈容量。

## 修复方向（未实施，待确认）

1. **认证不占请求 session**（最小、已验证方向）：`get_current_user` 用独立短 session 查询用户后即关闭；或认证查询结束后显式结束请求 session 的事务。这样单请求任意时刻最多 1 条连接，E3 已证明 40 并发不再死锁。需要复核 `User` 对象在 session 关闭后的属性访问是否符合现有依赖语义（`stream_identity` 已是这种写法）。
2. **读取复用请求 session/连接**：让 `read_trading_*` 不再 `session.bind` 新建 session。需要复核原注释所说的"clean session"快照语义（可能刻意要与请求事务隔离）。
3. **锁与连接的顺序**：`refresh_groups` 先借连接再取锁，或去掉锁内的 session 借出，避免"锁持有者等连接、等待者持连接等锁"的环形等待。
4. 不建议用"给读取路径单独小池/限并发"掩盖问题；那会增加新的排队层而不解决 2 倍占用。

## 与同日修复的衔接（E7）

本复现进行时，主工作区已出现同日修复（未提交）：`backend/app/api/v1/user_trading.py` 新增 `trading_user_id` 依赖，用独立短 session 完成认证后返回用户 ID，三个聚合读取接口改用它；原理与本文方向 1 相同。该修复与 [fx-loadtest-incident-2026-10-17.md](fx-loadtest-incident-2026-10-17.md) 新增小节描述一致。

为独立验证，把该文件复制进隔离 worktree（`backend/` 与线上一致的版本）后用同一套 harness 重跑，跑完 `git checkout --` 还原，worktree 恢复干净：

- E2 三个路由各 40 并发：`trading-versions` 40/40 200（0.46s）、`trading-context` 40/40 200（0.60s）、`trading-prices` 40/40 200（0.42s）；池一度到 30 但立即排空，服务端 `QueuePool limit` 0 次。
- 与 E4 完全相同的 CPU 限制与百人驱动：正常阶段 72 循环、69 笔成交、642 个请求，**0 超时、0 5xx、0 次池错误、0 个热点快照**，池峰值 22；`versions` 158/158 成功。剩余中止原因是"成交 P99 > 2s"（该限制下成交 P50 已达 2.37s），属于延迟指标而非池故障。
- 对照 E4（未修复、同一限制）：74 循环、20 笔成交、349 请求、95 个 `versions` 超时、29 个热点快照、池 30/30 锁死 61 秒。

结论：短认证 session 消除了池死锁；在本机 1/3 核的限制下仍会撞到成交 P99 门槛，这是算力/延迟问题，不能据此说修复"性能达标"，但池耗尽这一故障不再出现。

E7 只验证了未提交的主工作区改动，没有 commit、部署或线上复测；隔离 worktree 的临时复制已还原。

## 证据与重跑

证据目录：[output/fx-pool-repro-20261017/](../output/fx-pool-repro-20261017/README.md)

- `harness/`：插桩服务、探针、启动脚本（均在本机 `/tmp` 编写，未进产品代码）
- `runs/run5`：E2 结构复现（快照、池事件、pg 采样、服务端错误摘要）
- `runs/run6`：E3 反事实
- `runs/run7`：E4 百人同款（29 个热点快照 + `snapshot-timeline.jsonl`）
- `runs/run4-control`、`runs/run4-trade`：E1、E6
- `runs/run8-fix`、`runs/run9-fix-throttled`：E7 修复验证（三路由 40 并发 + 同限制百人驱动）
- `driver/`：E4/E5/E7 三种驱动的 `summary.json`
- `probes.jsonl`：E1/E2/E3/E6 的汇总（E7 探针结果在 `runs/run8-fix/` 的池事件与驱动摘要中）

环境复原步骤（隔离 PG 端口 55570，库名 `thccb_lt_pool_test`）：

```bash
# 1. 隔离 PG + schema（alembic 需要同步驱动；psycopg2 只装到临时目录，未改 venv）
initdb -D /tmp/.../pgdata -U sunyunbo --auth=trust
pg_ctl -D /tmp/.../pgdata -o "-p 55570 -k /tmp/.../sock -h 127.0.0.1" -l pg.log start
createdb -h 127.0.0.1 -p 55570 -U sunyunbo thccb_lt_pool_test
PYTHONPATH=<临时 psycopg2> DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55570/thccb_lt_pool_test \
  backend/venv/bin/python -m alembic upgrade head

# 2. 源市场 + 开关；100 个普通账户；签发 token（真实 CLI）
backend/venv/bin/python -m scripts.fx_loadtest prepare --run-id repro100 --source-pair 1 \
  --users 100 --initial-cash 100000 --output-dir /tmp/.../lt --confirm-stopped
backend/venv/bin/python -m scripts.fx_loadtest mint --run-id repro100 --output-dir /tmp/.../lt

# 3. 插桩服务（真实 lifespan、真实池 10+20）
REPRO_DIR=/tmp/.../run5 backend/venv/bin/python /tmp/.../serve2.py

# 4. 复现 / 反事实
backend/venv/bin/python probe2.py --mode versions --concurrency 40
REPRO_MODE=short_auth ... serve2.py   # E3
```

## 未覆盖与边界

- CPU 受限实验用 2 个忙循环模拟慢机，不是生产硬件或容量结论；未做 1 核等同硬件的五分钟稳态。
- 未覆盖 Casdoor 登录、PvE/强平/系统干预混合、多副本、真实公网/Wi-Fi。
- 隔离库仍保留本轮 100 个测试账户与专用市场（token 60 分钟后过期）；未清理生产数据，未发布任何改动。
- E3 是诊断性反事实，不是已验收的修复；修复需另开分支、补最小回归并按同一驱动复测。

## 建议的下一步

1. 主工作区的短认证 session 修复已由 E7 独立验证可消除池死锁；下一步是补齐既有检查、提交到单独分支，并决定是否部署复测。
2. 复测时保留本 harness 的池占用/热点快照，先确认百人正常阶段池错误为 0，再判断成交 P99 是否需要单独优化（本机 1/3 核的 P99 不能代表生产）。
3. 仍建议评估方向 3（`refresh_groups` 锁内借连接）；修复后它不再是死锁必要条件，但仍是"持锁等连接"的结构性风险，尤其在未来加入更多嵌套读取时。
4. 若线上复测，Nginx 按 token 分桶仍未安装，需要单独处理，不能与本次池修复混为一谈。
