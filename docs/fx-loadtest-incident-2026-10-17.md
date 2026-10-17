# FX 百人交易连接池耗尽：诊断、修复与后续待办

记录日期：2026-10-17。下文按调查阶段保留当时结论；最新状态以本段和文末待办为准。认证连接占用问题及 context 重复读取已修复并完成本机验证，尚未部署。高压时同市场交易串行排队仍是主要瓶颈。

## 当前结论

100 人在线、200 条 SSE 的只读场景通过，开始正常频率交易后，应用数据库连接池耗尽，报价、成交、账户刷新和版本查询大量超时。50 人、100 条 SSE 的短时正常交易没有错误，但尾部延迟触发停测阈值。

已确认故障现象是应用池耗尽；具体调用链尚未通过隔离复现或故障现场任务栈证实。不能把原因写成“SSE 超过某个数量必然堵死”，也不能把增大连接池当成已经验证的修复。

## 两轮实测

目标：`https://thccb.secret-sealing.club`，开发机经公开 HTTPS 入口发请求。业务后端版本均为 `ce09c41`；两轮间没有修改业务代码、连接池或实际 Nginx 配置。在线角色比例为 60% 现金、20% 融资、20% 空头。

| 指标 | 100 人 / 200 SSE | 50 人 / 100 SSE |
| --- | ---: | ---: |
| 两分钟只读 HTTP | 789/789 成功 | 395/395 成功 |
| 正常阶段名义交易尝试/s | 2.5 | 1.25 |
| 正常阶段实际时长，含排空 | 52.611s | 44.060s |
| 实际交易循环数 | 75 | 56 |
| 客户端确认成功成交数 | 10 | 56 |
| 成功成交/s，含排空时长 | 0.190 | 1.271 |
| 正常阶段 HTTP 成功/总数 | 90/274 | 439/439 |
| 正常阶段 HTTP 超时数 | 184 | 0 |
| 成功成交 P95 | 4.422s | 1.763s |
| 成功成交 P99 | 4.422s | 2.259s |
| 清理前池耗尽错误 | 明确存在 | 摘要未发现 |
| 自动停止原因 | 滚动30秒超时等错误率 >5% | 滚动30秒成功成交P99 >2s |

50 人正常阶段 P50 为 283.963ms，全轮 1894 个采样结果均成功。50 人没有完成原定五分钟稳态，不能宣称长期稳定。两轮活跃、加压、集中下单和条件单阶段都因提前停测而未运行。

只读结束时各轮 SSE 均在线。正常阶段结束记录的 SSE 0/0 混有客户端主动停测退出，不能直接理解为所有连接自然断开。百人另有75条私人 SSE 读取超时采样。百人11个成交请求自身超时；客户端超时不能证明交易没有提交，不应据此自动重试写请求。

这两轮按相同人均频率缩放，人数、版本读取量、交易尝试量和 SSE 数量一起变化，没有单独分离 SSE 因素。各轮池子克隆的是准备当时原市场储备，并非完全相同的初始经济状态。

## CPU 与数据库证据

百人故障期实际日志：

```text
QueuePool limit of size 10 overflow 20 reached, connection timed out, timeout 30.00
```

摘要中该文字出现740次，可能重复出现在异常堆栈中，不能解释成740个独立失败请求。

百人第二段服务器采样：

- 后端 Docker CPU 峰值86.52%，PostgreSQL峰值38.42%，同在09:19:38 UTC。
- 数据库连接09:19:38至09:21:11多次维持32；09:19:49至09:21:01多次只有1个active，包含监控查询自身。
- 该窗口最多5个active、1个锁等待连接。32是该数据库总连接数，不等同于应用池本身的30条上限。
- Docker CPU的100%表示一个核的用量；离散采样不能排除短峰。没有观测到CPU持续打满，证据更支持连接被持有/等待，而非数据库持续忙于执行SQL。

50 人资源窗口09:36:49–09:41:44 UTC，仅覆盖入场、持仓准备及大部分只读，**没有覆盖正常阶段的高延迟窗口**。该窗后端CPU峰值69.76%、PostgreSQL19.97%、连接峰值10、active峰值2、锁等待0；不要将这些数字说成50人正常交易的资源峰值。清理前近15分钟错误摘要覆盖正常交易时段，未发现QueuePool/TimeoutError/deadlock文字；这不证明完全没有等待。

## 最优先验证的代码线索

以下结构已通过阅读代码确认存在；它是否就是本轮池耗尽的主因，仍需复现。

1. `backend/app/core/users.py:get_current_user`（约63行）使用请求依赖 `get_async_session` 查询用户。查询后直接返回用户，没有在此结束请求session；`current_active_user`就是该函数别名。
2. `backend/app/api/v1/user_trading.py` 的 context、versions、prices 接口同时依赖认证和请求session，再将session传给credit读取函数。
3. `backend/app/services/credit/trading_context.py`：`read_trading_context`（约68行）、`read_trading_versions`（约84行）、`read_trading_prices`（约93行）基于传入session的bind再创建session，执行账户/规则/依赖读取；定价刷新还有独立session。
4. `backend/app/services/trading_prices.py:PricingRegistry.refresh_groups`（约59行）先持有每个市场的asyncio.Lock，再用 `session_factory()` 借连接读取快照。所有玩家看同一市场，会经过同一把锁。
5. 私人SSE在 `backend/app/api/v1/user_trading.py:trading_stream`，用短session鉴权，每15秒复核身份/规则/依赖，账户事件也读依赖；并非每条SSE都持续独占一个数据库连接。

可疑机制：请求仍持有认证连接，又等待内部读取/共享定价刷新借另一条连接；部分请求持有连接等待市场锁，持锁任务又等待空闲连接。并发跨过池容量后可能形成资源相互等待。连接等待使请求滞留，进一步放大并发，能解释50到100的断崖及“连接很多、active很少”。目前没有将这种资源等待确认成数据库deadlock。

## 下一会话建议从这里开始

- 先在隔离环境用现有真实路由、真实连接池复现嵌套借连接及市场锁等待。SQLite或只mock掉数据库的测试无法证明PostgreSQL池行为。
- 观察认证结束后请求session是否仍checkout连接、等待定价锁时是否仍占连接、锁持有者是否等另一个连接；优先记录关键任务栈/连接checkout状态，避免增加一整套监控框架。
- 若复现成立，优先修正已确认路径的session/连接生命周期，保留认证、账户版本一致性、定价覆盖等既有业务约束。不要未经确认新增队列、缓存层、数据库表、恢复状态机或跨模块架构。
- 连接池参数在 `backend/app/core/config.py`：`DB_POOL_SIZE=10`、`DB_MAX_OVERFLOW=20`；`backend/app/core/database.py`传给engine。用户询问过能否调大，尚未批准或执行具体参数更改。讨论过20+20作为对照候选；实施前先核对PostgreSQL `max_connections`、其他进程和预留连接，不默认100人就要100条连接。
- 修复验证先补最小真实回归、运行受影响既有检查，再按相同50人/100人场景复测。资源采样要覆盖实际交易阶段，别只采到入场/只读。不要因本记录自动再压线上或扩大验证矩阵。

## Nginx 与运维状态

新按token分桶方案已写入仓库模板，但**实际Nginx站点尚未应用**。线上 `/etc/nginx/sites-available/thccb.conf` 是root:644，部署用户有校验/reload权限，没有安装站点文件的权限。补丁在临时副本上成功，安装步骤权限失败，未替换实际站点；未绕过权限。用户目前不能登录服务器。

两轮没有观测到429，不应将百人故障归因于已证实的限流拒绝。后续限流发布与业务连接池修复要分别记录。

已真实验证Action建号、加密artifact交付、本机解密、HTTP驱动和清理。维护脚本已修正：固定使用当前运行镜像，避免latest触发服务器构建；Compose参数兼容；标准输入隔离，避免消费确认输入/远程脚本；临时维护容器使用操作者UID/GID，保留凭证目录0700。

两轮均已完成备份和定向清理，原市场ID8（CARD）恢复trading；专用市场ID9/ID10已删除。每轮脚本预备100个账户，50人轮次只让其中50个连接/操作；清理均删除全部100个预备账户。开发机两轮token及接收私钥已删除。服务器保留数据库备份与manifest，没有全库回滚。

最后50人轮次恢复检查：09:44:49 UTC，FX pairs与market/list均JSON200，分别257.1ms/98.4ms。这是当时复核，后续会话需要操作时重新检查当前状态。

## 证据与工作区位置

主工作区：`/data/sunyunbo/www/TouhouCCB`。它仍停留在较旧的本地main（当时c0bebd8），有大量用户未提交修改；**不要直接reset、覆盖或在未检查的情况下pull整合**。

已发布的远端main及压测隔离分支截至本次测试为 `bde43b0`；业务后端镜像实际为 `ce09c41`。隔离目录 `/tmp/sunyunbo/thccb-fx-loadtest-20261017`，分支 `ralph/2026-10-17-fx-loadtest`。下一会话先核对分支/远端状态，勿把旧工作区缺少压测文件误认为代码未完成。

本机结果（不含token）：

- [百人报告](../output/fx-live-20261017/report.md)
- [百人完整统计](../output/fx-live-20261017/http/summary.json)
- [百人故障资源采样](../output/fx-live-20261017/server-final/server-metrics.jsonl)
- [百人错误摘要](../output/fx-live-20261017/server-final/backend-errors.json)
- [50人报告](../output/fx50-live-20261017/report.md)
- [50人完整统计](../output/fx50-live-20261017/http/summary.json)
- [50人错误摘要](../output/fx50-live-20261017/server-final/backend-errors.json)
- [50人收尾健康复核](../output/fx50-live-20261017/recovery-check.json)

各轮目录内还有 `http/requests.jsonl`、控制台日志和Action清理日志。操作手册为隔离分支的 `loadtest/FX_README.md`；计划为 `docs/superpowers/plans/2026-10-17-fx-http-loadtest.md`。

远端证据：

- [部署成功](https://github.com/Renko6626/TouhouCCB/actions/runs/39019130379)
- [百人建号](https://github.com/Renko6626/TouhouCCB/actions/runs/39020090750)、[百人清理](https://github.com/Renko6626/TouhouCCB/actions/runs/39020834251)
- [50人建号](https://github.com/Renko6626/TouhouCCB/actions/runs/39021695809)、[50人清理](https://github.com/Renko6626/TouhouCCB/actions/runs/39022219831)

本交接文档是主工作区新增的本地文档，本轮未为它提交或推送，不触发部署。

## 同日后续：隔离 PostgreSQL 复现

以下是交接后的调查结果，不改变前文对原始线上证据的限定。没有更改业务代码、线上参数或再次压测线上。

### 版本与范围

主工作区仍为 `c0bebd8`，有用户未提交修改；压测隔离目录为干净的 `bde43b0`。对 `users.py`、`database.py`、`user_trading.py`、`credit/trading_context.py`、`trading_prices.py` 执行与 `ce09c41` 的 diff，主工作区及隔离版本均无差异。复现导入隔离目录的生产代码。

使用新建的一次性 PostgreSQL 14 集群，只监听 `/tmp/sunyunbo/fx-pool-incident-20261017/socket`，端口编号 55489，无 TCP 监听。数据库为 `fx_pool_test`；真实 SQLAlchemy/asyncpg 池容量保持 10+20，仅测试引擎将池等待超时缩短为 1 秒。FastAPI 测试应用挂载真实聚合读取路由，执行真实 JWT 校验、用户查询、账户读取和定价逻辑；不启动生产应用 lifespan 或交易后台任务。

### 已复现的机制

认证的 `get_current_user`（`users.py:83`）通过请求依赖查询用户后未结束 session；`get_async_session`（`database.py:38`）在请求依赖清理时才关闭 session。路由与认证复用这个请求 session。三个聚合读取服务随后创建独立 session，因而出现拿着认证连接再借连接的情况。

最小复现通过认证依赖包装器调用原始认证函数，仅在认证完成后设置同步屏障，安排 30 个请求都完成认证再继续真实路由，构造可达的饱和时序。等待时记录到 30 条连接全部 checkout、30 个请求未完成，任务等待链分别位于 `read_trading_context:73`、`read_trading_versions:87`、`read_trading_prices:95` 的内部查询，随后进入真实池等待。

首次结果：

| 路由 | 单请求 | 30 个保留认证连接的请求 | 30 个短认证 session 对照请求 |
| --- | --- | --- | --- |
| trading-context | 200 | 21 池超时、9 个 200 | 30 个 200 |
| trading-versions | 200 | 29 池超时、1 个 200 | 30 个 200 |
| trading-prices | 200 | 19 池超时、11 个 200 | 30 个 200 |

错误为 `QueuePool limit of size 10 overflow 20 reached, connection timed out, timeout 1.00`。超时请求退出后释放连接，因此部分请求随后成功；具体失败数量取决于调度时序。所有场景结束后 checkout 均回到 0，未见持续泄漏。短认证 session 对照仅在临时脚本内调用原始认证逻辑并关闭独立认证 session，业务服务及池容量相同；不是已提交的修复。

第二个复现安排 29 个真实 versions 请求完成内部读取、在进入价格刷新前等待，再启动第 30 个请求占用最后一条认证连接，随后释放刷新屏障。采样结果为 checkout=30、市场锁已持有、锁等待者=28、未完成请求=30：

- 第一个请求停在 `trading_prices.py:65` 的 `_snapshot` 查询，持有市场锁等待连接。
- 第二个请求停在 `trading_prices.py:63` 的市场锁获取，仍占用认证连接。
- 第 30 个请求停在 `trading_context.py:87` 的内部账户版本查询，等待连接。

最终 2 个池超时、28 个 200，全部结束后 checkout=0。确认市场锁可以参与这条资源等待链；这不是已确认的 PostgreSQL 数据库锁 deadlock。

### 产物、命令与结论边界

临时目录：`/tmp/sunyunbo/fx-pool-incident-20261017`。

- `reproduce.py`：三路由单请求、保留认证连接的并发请求及短认证 session 对照。
- `reproduce.jsonl`：首次完整输出，包含等待任务的协程链。
- `reproduce-rerun.jsonl`：脚本改为可重复使用调查账户后的再次运行输出。
- `lock_reproduce.py`、`lock-reproduce.jsonl`：市场锁参与等待的复现与任务链。
- 执行命令：在 `/tmp/sunyunbo` 下运行 `python /tmp/sunyunbo/fx-pool-incident-20261017/reproduce.py` 和 `python /tmp/sunyunbo/fx-pool-incident-20261017/lock_reproduce.py`，均退出 0；前者首次及再次运行均满足正常/对照全部 200、保留认证连接场景每个接口出现真实池超时的行为断言。

这确认了当前代码存在可触发的连接池资源等待问题，且释放认证连接可以消除本次受控复现中的故障。屏障用于固定并发时序，不代表原线上请求到达过程；本轮没有 SSE、真实交易或原现场任务栈，不能声称它已被证明是线上事故的唯一原因，也没有验证 50/100 人持续交易容量。

建议最小修复优先限定在三个聚合只读接口：复用私人 SSE 已有的短认证 session 思路，在进入内部读取/市场锁之前归还认证连接。不要全局随意 rollback 共享认证 session，以免影响其他写路由的 ORM 对象与事务边界；保留原认证校验、账户版本前后复核、定价串行刷新和覆盖约束。下一步修复时将真实池回归收敛到现有 PostgreSQL 测试层，并运行受影响既有检查；本轮没有新增永久测试或实施修复。

## 同日修复：三个聚合查询接口的短认证 session

用户确认方案后，已在主工作区实施局部修复，尚未提交、部署或线上复测。

- `backend/app/api/v1/user_trading.py` 增加 `trading_user_id` 依赖：使用短 session 调用原有 `get_current_user`，提取整数用户 ID 后关闭 session。三个聚合查询接口改用此依赖，在进入业务读取前已归还认证连接。
- 业务读取仍使用原有账户版本复核、依赖授权和定价刷新逻辑。没有更改全局认证函数、其他写接口的共享 session、池参数或私人 SSE 行为。
- 在现有 `backend/tests/pg/test_pg_trading_context.py` 增加参数化真实 HTTP 回归：三个路由分别运行于只有一条连接、无 overflow 的 PostgreSQL 池，使用真实 JWT 认证和真实业务查询，并验证返回的账户或市场身份。这直接捕获保留认证连接再嵌套 checkout 导致正常查询超时的故障，无需并发屏障或大批请求。

验证命令及结果（在 `backend` 下执行，PG URL 指向上述一次性本机库）：

```sh
TEST_PG_DATABASE_URL='postgresql+asyncpg://sunyunbo@/fx_pool_test?host=/tmp/sunyunbo/fx-pool-incident-20261017/socket&port=55489' python -m pytest -q -m pg tests/pg/test_pg_trading_context.py
python -m pytest -q tests/test_trading_context.py tests/test_trading_stream.py tests/test_update_username.py
git diff --check
```

新增回归在修复前三个路由均因真实池超时失败；修复后 PG 文件 5 项通过，其余相关检查 21 项通过，diff 检查通过。没有运行全套测试或线上 50/100 人稳态场景，因此暂不据此宣称线上容量已验收。

## 同日本机混合角色压测

用户随后授权临时数据库复测。采用干净的 `bde43b0` 后端快照，仅替换本次修复的 `user_trading.py`，用项目 `backend/venv` 启动完整 lifespan、单 Uvicorn worker、本机 PostgreSQL 14 和真实 loopback HTTP。应用池保持 10+20，现金/融资/空头角色为 60/20/20。每轮独立库和相同初始经济参数；资源采样覆盖实际交易阶段。没有向线上发请求。

- 50 人短测：正常 61.004 秒，74 笔全部成功，成交 P99 151.931ms；活跃 114 笔成功；加压 373 个循环、366 笔成功，7 次报价版本冲突未下单。集中下单 50 笔全部成功，实际提交窗口 2 秒。全轮无 HTTP 请求错误或 SSE 错误，连接占用峰值 15/30。
- 100 人：两分钟只读完成，200 SSE 在线。正常交易 301.006 秒，749 个循环、748 笔成功、1 次报价版本冲突未下单；5762 个 HTTP 请求全部成功，成交 P99 213.285ms，实际成交 2.485/s。正常阶段池占用峰值 11/30。
- 100 人活跃短测 61.015 秒，452 个循环、447 笔成功，5 次报价未通过；2708 个 HTTP 请求全部成功，成交 P99 1266.516ms，阶段结束时 200 SSE 在线。
- 100 人名义 25 次交易尝试/s 的加压阶段触发成交 P99 >2 秒保护：34.331 秒内，384 个循环、322 笔客户端确认成功、27 个成交请求超时、22 个成交请求 HTTP 409 版本冲突、13 次报价未通过。池占用峰值 18/30，无 QueuePool 耗尽文字或异常堆栈；CPU 采样峰值约一核满载。成交 P99 14630.755ms。100 人集中下单未执行，挂单两轮均跳过。高负载仍有性能上限，未进一步定位全部耗时来源或实施额外优化。

两轮均未复现原有池耗尽。受控本机结果不能替代线上容量验收：没有网络往返、Nginx/CDN 和原服务器资源约束，池子初始状态也与线上不同。100 人加压结束的 SSE 0/0 包含客户端保护停测取消，不能当成自然断流。客户端写超时不证明未提交，没有自动重试。

完整结果：[本机压测报告](../output/fx-local-pool-fix-20261017/report.md)。逐请求统计、资源采样和后端日志在相邻 50/100 子目录。临时入口 `/tmp/sunyunbo/fx-pool-loadtest-20261017/run.py`，两轮 token 已删除，临时后端与 PG 均已停止，未提交、推送或部署。

## 同日高负载诊断

用户授权继续定位性能开销，未授权或实施新的架构改造。本轮仅在临时启动器中增加 SQL、函数墙钟计时、市场门闩等待和事件循环滞后计时，并使用临时安装的外部 py-spy 采样；没有再改仓库业务逻辑。

较轻采样轮次的关键证据：

- 高压 net/trades 每次市场门闩获取平均等待 6742.650ms、P99 8828.815ms；拿到门闩后平均持有 80.667ms。队列峰值 102 个任务，池 checkout 采样峰值 20/30，数据库锁等待采样峰值 1。长等待主要在应用市场门闩前，不是池耗尽。
- 门闩内执行包含市场/用户/钱包/空头锁定、依赖与风控复核、成交/账本/审计写入和 commit。混合角色可能多次获取门闩，因此门闩次数不等于成交次数。
- 较轻采样 normal 段，context 每 HTTP 请求 12 次数据库执行、snapshot 3 次、my-trades 2 次，成交后刷新合计约 17 次，报价、成交、版本轮询与 SSE 周期复核另算。net/trades 平均约 23 次数据库执行，包含混合角色及重试，不能当成每一种成交的固定次数。
- `read_dependencies` 每次分别查询持仓、钱包、空头三组身份，多个 HTTP 读取和私人 SSE 重复调用。较轻采样高压阶段，私人 SSE 1790 次 SQL、context 4332 次、versions 1386 次，net/trades 6459 次。cursor elapsed 包含 IO、调度及可能的锁等待，不能当成数据库纯执行时间。
- CPU 栈的方向性线索为 SQLAlchemy、异步 IO/调度与 SSE，而不是纯 AMM 数学。首轮高频采样有明显观察扰动；较轻非阻塞采样也有丢样，不能给出精确 CPU 分账。本轮短阶段与此前五分钟基准不同，仅用于定位，不能替代容量验收。

优先建议在既有架构内检查合并依赖查询、减少 basis/依赖重复读取，以及独占事务中的重复加载与数据库往返。认证、风控重验、记账审计与锁顺序仍须保留；改前端刷新契约、调整 SSE 检查频率或新增跨请求缓存等需另行确认。本轮没有实施优化。

完整诊断及采样限制：[高负载诊断报告](../output/fx-local-diagnosis-light-20261017/report.md)。两轮临时后端与 PG 均已停止、token 已删除。

## 共用读取层调查

本节仅阅读代码及已有 SQL 采样，未实施优化或新增测试。

### 实际读取内容与成本

`credit/trading_context.py` 返回供浏览器估值和交易预估使用的原始输入，不在这些聚合接口内运行完整账户估值：

- 账户基准：现金、金债及计息时间、自动还款、冻结/活跃状态、经济版本；LMSR 持仓数量/成本、FX 钱包数量/成本、空头本金/欠息/计息时间/成本。
- 市场依赖：账户持有/欠有的全部 LMSR/FX 市场身份，再加当前页面市场。用于跨产品组合估值、定价覆盖和 prices 补齐权限；不只读取当前页面。
- 规则：利率、费率、功能开关、信用阈值、应急停增险状态。常规 site_config 项有现有的 60 秒进程缓存；停增险查询刻意绕过缓存，每次 read_rules 都执行一条热读。
- 已提交的定价输入：FX 储备/费率/状态/pool_version，或 LMSR 的 b/q/状态/截止时间，并生成进程 epoch、revision、input_hash。不是盘口、成交列表、K 线或完整图表快照。

热缓存、仅一个定价组、没有重试时，采样和代码对应如下：

| 入口 | 数据库执行分解 | 合计 |
| --- | --- | ---: |
| context | 认证1 + 前置版本1 + 账户基准4 + 依赖身份3 + 热规则1 + 定价1 + 后置版本1 | 12 |
| versions | 认证1 + 账户版本1 + 依赖身份3 + 热规则1 + 定价1 | 7 |
| prices | 认证1 + 补齐权限所需依赖身份3 + 定价1 | 5 |
| 私人 SSE 每15秒复核 | 用户状态1 + 热规则1 + 依赖身份3 | 5 |
| 私人 SSE 每条 account_changed | 更新订阅所需依赖身份3 | 3 |

涉及多个市场时定价查询按组增加，配置缓存冷却/过期、context 一致性重试等也会增加次数。100 人都保持当前单组页面时，仅 15 秒 versions 轮询和私人 SSE 周期复核就约 80 次数据库执行/s，不包含交易、账户事件、价格 publisher 等；这是按上述代码次数计算的负载量，不是新的实测吞吐。

### 明确的重复与不能直接删除的复核

1. context 的 read_basis 已读取持仓、钱包、空头全部行及市场 ID，read_dependencies 随后又查相同三类行，仅取 ID。可优先研究在本次请求内从 basis 构建依赖集合，保留当前组、去重排序和缺失 Outcome/市场处理。单组情况下理论上 context 可由12次降至9次，无需跨请求缓存或改变事件契约；尚未实施或测量性能收益。
2. context 前置 authoritative_version 与紧随其后的 basis 用户行都读 economic_version，可再评估前置版本读取能否由 basis 中的版本承担。前后复核不是纯粹浪费：必须继续保证用户、仓位、钱包、空头在读取期间没有跨经济版本混装，不能直接删除结束时的权威复核。此项不与第一项默认捆绑实施。
3. PricingRegistry 的 _entries 保存上次内容用于比较 revision，不会跳过数据库读取。每次 context、versions、prices 都重新读取同一组定价输入，publisher 也独立刷新。市场锁只保证读取/安装顺序，没有合并并发请求的数据库查询。复用跨请求定价快照需要明确新鲜度、全部定价变更、缺失组及 epoch/覆盖语义，属于需进一步确认的设计。
4. account_changed 后，服务端 SSE 先查三类依赖更新订阅，浏览器随后拉 context 又取得同一账户状态/依赖；15秒 versions 和 SSE 复核也各自重新读依赖与规则。当前事件只有版本提示，没有携带完整依赖集合，重复有其来源。改变事件载荷、订阅更新或复核周期会改变同步契约，本轮不直接删逻辑。
5. 前端 Fx.vue 的 refreshAll 会强制请求账户刷新并读取 snapshot、my-trades；account_changed 和 versions 发现新版本也会请求账户刷新。tradingSession 已有200ms合并、单个在途 context、版本已覆盖时跳过刷新和价格已覆盖时跳过 prices，不能把多个触发源直接等同于每笔必然多次 HTTP。无版本的强制刷新与已在途/SSE刷新相遇时是否额外再读，需专项时序证据，当前不宣称已测出比例。
6. snapshot 的储备/价格部分与定价组存在重叠，但还有24h成交额、历史覆盖等独立内容；my-trades 是成交明细，context 不提供。因此“成交后17次数据库执行”里有可减少的重复，也有页面实际需要的独立读取，不能全部算作无用工作。

建议先做第1项这种请求内数据复用，再单独评估版本查询和身份查询合并的可行性。应急停增险热读、认证连接及时归还、prices 的依赖授权、账户版本一致性、丢消息后的恢复检查仍保留。尚无证据需要新增缓存服务或修改交易 writer。

### context 请求内复用的安全性审查

本次审查限定于用已读取的 AccountBasis 构建 context 的定价依赖集合，保留 before/basis/after 三方版本比较及最多三次重读。没有改生产代码，也没有将同样做法推广到不读取 basis 的 versions、prices 或 SSE。

逻辑等价条件已逐项检查：

- LMSR：从 basis.lmsr_positions 的非 None market_id 取集合，与原 Position INNER JOIN Outcome 相同。basis 自身仍保留 Outcome 缺失造成的 market_id=None 记录，供前端维持 missing_market_identity 提示；仅在构建 GroupKey 时跳过 None。
- FX：取所有钱包和空头行的 pair_id，不按金额过滤。零余额钱包、已清零但保留的空头行仍产生原来的市场依赖；同一 FX pair 的钱包与空头合并为一个组。
- product 与 id 一起去重，避免相同数字的 LMSR 与 FX ID 混为一组；保持原来的 product/id 排序。
- 当前页面组即使无持仓也必须加入。市场本身缺失时仍交给原 Registry/missing_keys 生成 missing_groups，不提前静默丢弃依赖。
- basis 仍由认证用户 ID 的数据库查询构建，不读取其他用户的行；不复用跨请求缓存，不延长认证连接持有时间。
- 正常经济写入与 economic_version 在同一事务提交是既有不变量，新方案继续依赖它。版本前后复核保留，因此读取后新增/移除仓位会触发重读；不以旧依赖先读到新仓位身份来冒充一致快照。市场价格、状态的独立变化仍由原 Registry 读取和 stamp 契约处理。

临时脚本 `/tmp/sunyunbo/fx-read-basis-audit-20261017/audit.py` 导入真实生产 read_basis/read_dependencies/Registry，在临时脚本内定义候选集合转换，对静态集合与并发读取进行检查，没有 mock 数据库。结果保存在同目录 `results.jsonl`：

- SQLite 与 PostgreSQL 各8个新旧集合对照一致：有仓/空账户、重复市场、多产品同数字ID、零余额/零份额、其他用户隔离、当前组已有/未持有/不存在。
- SQLite 另外验证 Outcome 和 FX pair 缺失：账户里的缺失 Outcome 记录保留、依赖集合相同、缺失 FX 定价组仍报告 missing_groups。与项目现有异常数据覆盖一致；没有在 PostgreSQL 上关闭外键来伪造故障。
- PostgreSQL 确认为 read committed。用独立真实写事务在 read_basis 完成后新增/删除钱包并推进经济版本，候选路径均读取两次，第一次旧集合未返回，最终账户版本与依赖集合均匹配新状态。
- 每次读取后都提交新版本时，三次重读后仍返回原有 `409 account_context_changed`。

已有检查实际执行：`python -m pytest -q tests/test_trading_context.py` 为2项通过；项目 venv 下、`TEST_PG_DATABASE_URL` 指向新建一次性 `fx_read_basis_audit_test` 库，执行 `python -m pytest -q -m pg tests/pg/test_pg_trading_context.py` 为5项通过。既有检查验证当前代码；候选简化的行为验证来自上述临时脚本，不将其混称为已落地修复。审查结束后临时 PG 已停止。结论：可以实施此限定的请求内复用，零金额过滤、版本检查删减及跨请求缓存不纳入本次方案。


### context 请求内依赖复用实施与性能对照

用户确认后由原生 subagent 实施，主 agent 检查最终差异并复测。`read_trading_context` 用 `_dependencies_from_basis` 从已读取的账户基准构建市场集合，替代同请求中的三次依赖身份查询。保留零金额行、LMSR缺失身份、当前组、去重排序及全部版本检查；versions、prices、SSE、Registry与交易writer均未改变。扩展现有context用例覆盖零行、重复市场及不存在的当前组。

在两个隔离后端快照、各自新建的本机PG库上对照，双方都包含上一轮短认证session修复，唯一业务差异是本次context实现。专项为预热后3轮、每轮200请求、并发10；混合为100用户与200条SSE。结果如下：

| 指标 | 修改前 | 修改后 |
| --- | ---: | ---: |
| 单组热缓存context每次SQL | 12 | 9 |
| context专项吞吐（请求/s） | 100.70 | 112.73 |
| context专项平均延迟（ms） | 98.16 | 87.60 |
| 正常阶段成交P99（ms） | 377.38 | 298.96 |
| 活跃阶段成交P99（ms） | 1241.97 | 767.40 |
| 高压成功成交（笔/s） | 9.44 | 9.72 |
| 高压成交P99（ms） | 14641.81 | 14641.20 |

确定性收益是context每次少3次SQL（减少25%）。专项吞吐提升约11.9%是三轮短测观测值；混合只各运行一轮，运行顺序、调度和机器负载存在波动，不据此承诺整体交易容量提升。正常及活跃阶段两版HTTP采样全部成功、200条SSE在线；高压两版均触发成交P99保护，存在超时与版本冲突，未运行后续burst。高压仍只有约9～10笔/s，串行交易排队瓶颈尚未解决。简化版停测附近有一次连接取消堆栈，两版均无池耗尽文字，详见原始日志。

最终代码验证：context现有用例2项通过，PG相关用例5项通过。临时脚本改为调用最终生产转换函数，再次验证SQLite/PG各8组等价集合、缺失身份保留、并发增删钱包触发重读及持续更新返回409。没有新增跨请求缓存或删减一致性复核。

完整方法、阶段数据、限制与原始结果：[性能对照报告](../output/fx-context-read-ab-20261017/report.md)，[并发与等价审查结果](../output/fx-context-read-ab-20261017/audit-results.jsonl)。本轮没有部署或提交代码。


## 阶段性归档与后续待办

本阶段交付：三个聚合读取接口使用短认证 session，context 从已读取的账户基准派生依赖；相关测试及本机性能对照随代码归档。本阶段不修改连接池容量、交易串行约束、版本检查、事件契约或数据库 schema。

| 顺序 | 待办 | 调查范围与交付证据 |
| --- | --- | --- |
| 1 | 拆解同市场交易独占段 | 在现有交易路径中记录依赖发现、加锁后重读、风控、写入、flush/commit的次数与耗时，区分必要重验和可复用数据。优先解释约80ms门闩占用中可减少的数据库往返；保留锁顺序、经济版本、记账及审计约束。先给出局部优化候选及风险，再实施。 |
| 2 | 评估其余读取接口的依赖查询 | versions、prices、私人SSE仍各有三次身份查询。先核对能否在现有接口内减少往返，明确零行、缺失身份、授权及当前组语义；不把context的basis复用强行推广到没有basis的入口。 |
| 3 | 核对客户端刷新与事件重叠 | 对照一次成交后的account_changed、context、versions、snapshot和my-trades时序，确认现有200ms合并及在途请求复用后还剩多少实际重复。交付真实请求次数和触发链，再决定是否修改。 |
| 4 | 专项评估定价查询复用 | Registry目前不会跳过数据库读取。若前三项仍不足，再提出新鲜度、失效来源、缺失组及stamp/epoch约束明确的方案；跨请求缓存、事件载荷或模块边界变更须先由用户确认。 |
| 5 | 对保留的优化做性能回归 | 每项单独对照，使用相同代码基线、数据、用户角色、200条SSE及目标频率。先确保百人正常阶段稳定，再看活跃/高压成交P99、门闩等待/占用、SQL、池峰值、超时及版本冲突；目标25次/s为所有用户合计尝试率。达到保护阈值即停，不自动重试超时写请求。 |
| 6 | 发布与环境复核 | 后续明确安排发布时，再按项目既有流程部署、健康检查及受控验证。当前本机结果不能替代部署验收，不直接重跑线上高压。 |

建议下一项先做第1项：读取优化已经节省3条SQL，但高压成交仍约9～10笔/s、P99约14.6秒，继续只优化context不足以解决交易排队。暂不引入队列、拆分同一AMM市场并发写、缓存服务或新表。

验证记录：`cd backend && venv/bin/python -m pytest -q tests/test_trading_context.py`为2项通过；临时PG库下`venv/bin/python -m pytest -q -m pg tests/pg/test_pg_trading_context.py`为5项通过；`git diff --check`通过。没有执行全量测试。临时后端/PG均已停止，压测token已删除。

归档范围：本轮代码、测试、本文、隔离复现说明，以及读取A/B、连接池修复复测和轻量诊断的报告/摘要。逐请求日志、后端日志及资源原始采样保留在本机对应output目录，不随本阶段提交；早期线上实验与隔离复现的原始目录链接也可能仅本机可用。
