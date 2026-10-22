# FX writer 数据库审阅与分层优化（2026-10-18）

基线：`ffa5178750c2bb085148711c8397afe78fab4655`；分支：`ralph/2026-10-18-fx-writer-db`。
仓库从 `https://github.com/renko6626/touhouccb.git` 克隆。本次修改未推送、未部署。

三轮无 SQL 插桩实验的吞吐中位数：仅减少查询，融资净买入 **+1.9%**、旧融资 **+10.0%**、
空头开仓/回补 **+8.9%**；再加固定 SQL 结构复用，累计分别为 **+8.2%、+18.5%、+15.8%**。
四 pair 融资累计 **+17.2%**。纯现金路径没有确定收益。完整范围和延迟见
[measurements.md](measurements.md)。这些是本机隔离 writer 的结果，不能当作整站提升或生产容量。

## 结论与实际改动

FX 已经有每 pair 单写者队列、提交后行情快照和 NetTrade 锁后账户/全部空仓复用。
这些是基线能力，不计入本轮收益。原架构文档仍称“FX 没有经济 writer 队列”，本轮已修正。

本轮保留可独立比较的两层改动：

- **A：只减少查询。** 完整 LMSR 正持仓和 FX 正钱包用 `UNION ALL` 一次读取；旧现货/融资和空头开仓、回补、强平回补的依赖发现复用刚锁读的 User；旧融资使用已有 `increase_debt_locked`，不重新锁读同一账户。
- **B：A 加语句结构复用。** 账户锁、账户权威刷新、资产联合查询、外币义务、当前规则及空头 Pair/Wallet 锁读使用固定参数化语句。每次仍执行 SQL、读取当前行；只省 Python 表达式构造和 cache-key 构造工作。沿用项目已有静态语句模式，不引入新框架或跨请求资金缓存。

对应源码：`backend/app/services/credit/risk.py`、`fx/trading.py`、`fx/shorts.py`、`market_locks.py`。
独立补丁见 [queries.patch](queries.patch) 和 [combined.patch](combined.patch)，均相对同一基线。

## 架构与约束核对

阅读了 `AGENTS.md`、`CLAUDE.md`、`CONTRIBUTING.md`、`docs/architecture.md`、
`docs/development.md`、`docs/schema-conventions.md`、PG 测试说明，以及已有 writer profile、SQL 审计和锁后复用报告。

真实主链路是：

```text
不可变命令 → pair 队列 → consumer 新 session
  → Pair 锁 → User 锁 → 幂等/版本检查
  → 钱包/全部空仓/完整组合/实时规则 → Treasury → 同一时点 T 模拟
  → receipt、资金、逐腿成交、audit、pool history 同事务写入
  → COMMIT → 同步安装已提交行情 → 通知账户/历史/推送 → 下一条命令
```

`fx/writer.py` 队列上限 256，submit 超时 10 秒；`writer_ops.py` 每命令独立 commit。
多个 pair 的 consumer 共享进程和事件循环，也可能操作同一 User。增加同 pair 排队请求或连接池容量并不会让这一条 consumer 并行成交。

本轮保持以下实际约束：

1. Pair → User → 资金行 → Treasury 的锁序、`populate_existing=True` 和正常 autoflush；锁后才接受真实余额和风险输入。
2. 跨产品完整正资产集合、其他 pair 空仓及仅利息义务、当前冻结/利率 SQL 读取、一次已提交行情捕获；没有改成 TTL 账户/风控缓存。
3. 同一 T 结息、新本金起息、逐腿 economic/pool version、跨零原子回滚、两类幂等命名空间与精度。
4. receipt/ledger/trade 的 ID 依赖所需 flush，最终 commit，以及 commit → install → notify 顺序。
5. 金融状态不从已取消的排队请求、旧 quote、入队前 ORM 对象或上一命令 session 中复用。

资产联合查询使用 product、group id、asset id 三层区分，避免 LMSR 与 FX 的数值 ID 碰撞。
保留原始 Decimal 金额与正数过滤，不用 JOIN 把两套持仓相乘。新增行为覆盖检查不同用户交替查询、六位小数、零余额过滤，以及本事务尚未 flush 的资产变化。实际 SQLAlchemy 2.0.45 下 UNION 保留所需 autoflush，后一用例已实测通过。

## SQL 数量：确定的收益与边界

以下是独立诊断轮的真实 cursor 计数。每条命令都提交成功；SQL 含 flush 发出的 DML，不把 flush 次数再算作 SQL。COMMIT 和驱动连接健康检查不在 cursor 语句计数中。

|场景|基线 SQL / SELECT|A SQL / SELECT|B SQL / SELECT|减少 SQL|
|---|---:|---:|---:|---:|
|无债现金净买入|15 / 6|15 / 6|15 / 6|0|
|融资净买入|21 / 9|20 / 8|20 / 8|1（4.8%）|
|旧融资买入|22 / 12|19 / 9|19 / 9|3（13.6%）|
|独立空头开仓/全回补，均等混合|18.5 / 11.5|16.5 / 9.5|16.5 / 9.5|2（10.8%）|

现金路径原本就跳过完整依赖发现，A 对它没有应有收益；其测量差异用于观察噪声。
NetTrade 基线已复用 User 和全部 short rows，因此本轮只有合并正资产读取这一条可省。
旧融资多省 User 刷新和借款入口的重锁两条；空头路径多省 User 刷新一条。
B 与 A 的查询数严格相同，其收益应归于每次执行的 Python/ORM 开销变化。

## 性能测量

结果与统计见 [measurements.md](measurements.md)，原始数据见 [raw/summary.json](raw/summary.json)。

测试环境：用户指定的 conda `touhouccb`，Python 3.13.16、SQLAlchemy 2.0.45、asyncpg 0.30.0；
独立 PostgreSQL 16.15，Unix socket `/tmp/touhouccb-pg-socket`，内部 socket 端口号 55486，没有 TCP 监听。
主机为 WSL2 / Intel i7-14700。保留 `fsync=on`、`synchronous_commit=on`、`full_page_writes=on`。
没有修改系统 PostgreSQL、Docker 服务、生产库、`.env`、部署文件或数据库 schema 模型。

实验方法：

- 三个独立源码目录/进程，每个场景每个变体新建空白 `*_test` 库；每次记录全部后端源码 SHA256。
- 16 个独立账户，单 pair；另做四 pair 的融资场景。每账户先预热 2 笔，再执行 32 笔，单轮 512 笔。
- 3 轮交替顺序：A→B→基线、B→基线→A、基线→A→B；按每种场景汇总中位数和范围。
- 另有每账户 4 笔的诊断轮，记录 SQL、cursor elapsed、显式 flush 和 commit。它不混入容量统计；这些 span 有包含关系，不能相加。
- 使用真实 `FX_WRITER.submit`、真实业务 handler、真实 PostgreSQL 和 durable commit。没有启动 HTTP、SSE、行情聚合、结息/强平调度器；保留实际提交后安装/通知调用。
- 每轮验证金币资产减金币债务守恒、外币实物守恒、成交/audit 对应、pool history/版本增量，以及空仓回补清零。例行业务边界与真实跨账户锁竞争由回归测试覆盖。

**解释范围：这是 writer 微压测，不是全站压测或线上容量保证。** 本机 Unix socket、轻量单币种组合、零利率、短时饱和闭环负载，不代表远程数据库、长历史、大组合、真实息费混合和 200 条 SSE。
闭环压测的 P99 不是固定开放到达率下的 SLO；增加并发用户还可能放大排队。没有把旧报告另一台机器的数字和本轮数字相除。

首次预检的空头回补预算设为 0，原始代码按契约拒绝；已改为明确的 100 金币预算，并整组重启实验。
`pilot/` 是这轮未完成预检，**不参与结果统计**。首个小样本也补齐了与正式 lifespan 一致的 REGISTRY 配置。

## 更系统性改动：收益来源、优先级和代价

“少查询”与“更大改造”不是同一个收益项。设单 pair 平均服务时间为 $S$，稳定吞吐上限近似为 $1/S$；
减少 $k$ 次往返的收益受每次延迟 $r$ 限制，理想服务时间改善约 $kr$。真实客户端往返还含调度和 ORM，因此不能把数据库执行时间、cursor elapsed 和 flush elapsed直接相加。
排队接近饱和时，小幅缩短 $S$ 也可能明显降低等待；超出容量时则不能保证 P99。

|技术/改动|潜在收益与证据|需要解决的问题|本轮状态|
|---|---|---|---|
|固定语句结构复用|SQL 数不变，减少 Python 构造/缓存键成本；B 对 A 的实测单独列出|绑定值必须每次更新，不能缓存账户/配置结果|已实现并验证|
|统一锁后执行上下文|触发订单 `orders._attempt_order_owned → execute_fx_instruction_in_session → execute_net_trade_in_session` 三层均锁 Pair/User；后两层还重读净交易身份，存在进一步合并机会|上下文只在同 session/事务/账户内有效；保持订单取消竞争、回放、失败状态与 T；不能仅靠布尔“已锁”跳过检查|源码审阅发现；没有把未压测收益计入本轮|
|分离 SSE/报价与经济 writer 的进程资源|历史 profile 显示公共/私人 SSE 调度占用显著；可能释放 writer 的事件循环时间，比继续省一条 SQL 更重要|REGISTRY、账户通知、订阅当前均进程内；需要提交序号、跨进程可靠投影/失效/恢复；不能只开更多 uvicorn worker|架构后续项，未实施，不能承诺倍率|
|缩短写入阶段、批量 Core DML/预分配 ID|减少 ORM unit-of-work 与 ID 获取导致的多阶段往返；本次 cash 的 15 条里有 9 条 DML，单靠减少 SELECT 无法消掉|receipt→trade→audit/history 外键和 ID、时间戳/精度、每腿快照、版本推进、失败回滚须保持；需独立原型及故障注入|未实施|
|多命令 group commit / 驱动 pipeline|只有 commit/往返占比高时才显著；本次诊断提供 commit 占比和理想上界|跨命令依赖、失败隔离、取消/超时未知结果、幂等重试、发布顺序；现用 asyncpg，libpq pipeline 不是可直接开启的 SQLAlchemy 选项|未实施；不建议先做|
|进一步分片/多写进程|四 pair 场景能检验进程内多个消费者的总量；仍受共享 CPU 与跨 pair User 锁限制|全局经济写所有权、跨产品账户风险和一致性协议；不能简单去掉 User 锁|未实施|
|缩短队列/按延迟拒绝入队|过载时减少无望排队、结果未知和尾延迟|改变接入/拒绝行为；不会增加实际成交处理能力，需定义服务目标|未实施|

建议后续先用同一生产等价 HTTP/SSE 混合负载验证本轮改动，再按占比决定做锁后执行上下文或资源隔离。
不能把单写者理解为“所有经济写都没有并发”：别的 pair、LMSR、信贷、后台管理仍可能争同一 User。
也不采用关闭 durability、异步资金/audit 落库或行情缓存替代资金账本来换数字。

技术依据：[SQLAlchemy 官方缓存说明](https://docs.sqlalchemy.org/en/20/core/connections.html#using-lambdas-to-add-significant-speed-gains-to-statement-production)
区分 SQL 编译缓存和 Python 语句构造成本；这里沿用静态语句而非新增 lambda 层。
[PostgreSQL WAL 配置](https://www.postgresql.org/docs/16/wal-configuration.html)
说明 group commit 需要可并发到达的提交且受提交吞吐限制，`commit_delay` 也会增加延迟。
[PostgreSQL pipeline](https://www.postgresql.org/docs/16/libpq-pipeline-mode.html)
要求管理错误后的同步/中止状态。以上用于技术方案判断，不充当本项目实测结果。

## 验证与限制

- 后端全套：1532 passed、1 skipped、59 deselected；默认排除 PG。日志 [full-tests](validation/touhouccb-fx-full-tests.log)。
- 定向相关用例初轮：119 passed。此后扩充已有完整组合用例，覆盖用户隔离/零行/精度；全套通过。
- 新增同事务未 flush 持仓用例：1 passed，单独执行于全套收集后；没有把它冒充全套同次执行结果。
- PostgreSQL：22 passed，覆盖跨市场信贷准入、FX 空头/融资争用与回放、强平；日志 [pg-tests](validation/touhouccb-fx-pg-final-tests.log)。
- 另外尝试的 4 个 PG 触发订单测试被仓库硬编码的旧机器数据库白名单拒绝 setup，没有执行测试体；未修改它的护栏来宣称通过。SQLite 订单测试在全套中通过。
- 后端全部 app 模块 py_compile、`import app.main`，两个新压测脚本 py_compile 通过。开发导入提示未配置 SSO；本轮不做 UI/SSO 验证。
- 使用 Node 22.23.3 执行前端 type-check 通过；lint 45 个既有错误，源码与基线完全相同，未修改前端。日志 [lint](validation/touhouccb-lint.log)。
- `git diff --check` 通过。未新增依赖条目、迁移、后台进程架构或部署配置。

## 复现

创建三个独立源码目录，基线均来自上述 commit；A 应用 `queries.patch`，B 应用 `combined.patch`。
不要在有工作改动的目录切换/重置版本。准备本地、耐久参数开启的隔离 PostgreSQL，指定 Unix socket。

```bash
conda activate touhouccb
python loadtest/fx_writer_db_ab.py \
  --baseline /tmp/touhouccb-fx-baseline \
  --query-only /tmp/touhouccb-fx-query-only \
  --combined backend \
  --socket /tmp/touhouccb-pg-socket --port 55486 --user tonrac \
  --output output/fx-writer-db-review-20261018/raw \
  --repeats 3 --operations 32
```

脚本为每次调用新建数据库，不清空已有库。单次脚本 `loadtest/fx_writer_db.py` 拒绝非本地、
非 `*_test`、非空数据库，原始 JSON 含源码哈希、持久性参数、测量结果与守恒检查前后值。
保留 `touhouccb` 环境、PostgreSQL 二进制和测试数据以便复核；本次结束时停止实验数据库。
