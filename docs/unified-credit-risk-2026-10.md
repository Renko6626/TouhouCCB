# 统一信贷与定时强平（2026-10）

此文是发布操作手册，不代表已经完成发布门槛。当前版本仅运行统一信贷，模式收敛见 [统一信贷方案](superpowers/specs/2026-10-10-unified-credit-only-design.md)。
权威规则见 [设计 spec](superpowers/specs/2026-09-29-unified-credit-risk-design.md)。

## 玩家规则

一个金圆券贷款余额、一种日利率；预测市场和多个外汇钱包共用风险待遇。
账面净值 = 现金 + 各产品瞬时市值 − 含待结利息的债务。
清算净值 = 现金 + 各产品真实可变现金额 − 含待结利息的债务。
LMSR 用逐笔更新 q 的组清算算法；FX 用该交易对 AMM 卖出算法；均扣通常卖出手续费，不另罚金。
两种净值即使无债也不相等。暂停且非只减仓的资产可有账面市值，但清算价值为零。
账户页面的清算净值和风险状态是最近刷新快照，实际准入和扫描重新检查。

名义杠杆 L 对应初始 / 恢复率 `1 / (L - 1)`，维持率必须严格小于初始率。
新增借款和增险后须达到初始率；有债且清算净值 / 债务低于维持率时启动强平。
默认每 600 秒扫描一次；管理员 run-now 走同一入口且不重叠，不绕过 liquidation_enabled、风险阈值或重入保护。
交易、报价和行情变化不会额外触发强平。

强平先使用现金还债，再按当前整组净清算价值降序选择可卖的一组；同值依产品名、组 ID 升序。
一组指一个 LMSR 市场或一个 FX 交易对。每人每次扫描最多卖一组的配置比例（`liquidation_partial_pct`，默认 10%），数量按产品精度向上取整；清算净值不大于 0 时卖该组全部，仍不一次处理多组。
恢复到初始率或债务为零后停止。没有可变现资产且仍有债务时冻结新增信用，债务保留。
既有 paused FX 默认全停；只有管理员显式设 reduce-only 才能卖出 / 强平，且禁止买入。
首期最多 3 个 trading FX 交易对，各自保有池子和钱包。

## 启用与发布门槛

全新空配置默认开启贷款，名义杠杆上限为 10 倍，维持率为 4%；对应初始率 `1/9 ≈ 11.1111%`。无其他风险占用、忽略费用时，500 金圆券净值的理论新增贷款额度为 4,500 金圆券。
启动初始化仅补充缺失配置，保留管理员已保存的借款开关和风险参数；更新代码不会自动覆盖现有运营配置。

迁移保持旧授信：`credit_leverage = loan_leverage_k + 1`，不会自动提高杠杆。
统一信贷代码允许的最高名义杠杆为 50 倍，预测市场与 FX 共用同一配置。
设定 50 倍时，先将 `credit_maintenance_ratio` 调至严格低于 `1/49 ≈ 0.020408`（例如 `0.01` 为合法配置），再设置 `credit_leverage=50`；门槛需重启后端生效。
手续费、滑点和清算估值会使实际可开仓额度低于理论名义上限。
20 倍必须显式设置 `credit_leverage=20` 和 `credit_maintenance_ratio=0.04`；初始率为 `1/19 ≈ 5.2632%`。
应先设置维持率再提升杠杆，确保每次配置校验都成立。
切勿将展示用小数当作风险计算的精确边界。

本期 `loan_daily_rate` 和全局 LMSR `sell_fee_rate` 在运行期间固定，
只允许重复设置数值相同的值；应在恢复交易前配置和公示。
已有离线维护工具可先按旧利率结息再改息，操作要求见下节；在线仍禁止直接改息。全局手续费也不在交易中途切换。
FX 每交易对费率仍可通过持交易对门闩的管理接口修改。
只读实例拒绝非安全 HTTP 方法，包括登录 / 注册；登录使用写实例，已有认证的读取请求不受影响。

上线前须完成：后端完整回归、隔离 PostgreSQL 并发和崩溃恢复、迁移与赛季重置检查、前端检查、影子估值对账，以及同机同负载性能对照。
无债 P95/P99 增幅与吞吐降幅各不得超过 5%，错误率不得显著上升，完整扫描 P95 必须小于周期。
未达标不能用较低目标替代。现有证据与尚未完成的发布验收见 [验证记录](unified-credit-risk-validation-2026-09-30.md)。

发布顺序（仅有单独发布授权后执行）：

1. 停止发布窗口内的变更，备份完整数据库，记录代码版本、迁移版本和备份校验和；在隔离库验证可恢复。
2. 设置 `credit_new_risk_frozen=true`，再设 `liquidation_enabled=false`；确认冻结即时生效。
3. 部署审核过的版本并执行 `alembic upgrade head`。确认唯一写实例所有权，只读副本不挂载写调度器和直接 SQLAdmin 写入口。始终启动 LMSR writer 消费器，不受旧 single_writer_enabled 开关影响；不允许使用内联强平或 legacy 后备路径。
4. 只读取证现行 `sell_fee_rate`，确认 FX 各 pair 的卖出费率。若 LMSR 原强平未收普通卖出费而新路径开始收取，按运营决定的文案与时长提前公示；本手册不假定生产费率为零。
5. 显式设定并审计杠杆和维持率。门槛是启动缓存，重启唯一写实例后生效；冻结开关热生效。旧 `unified_credit_enabled` 配置已移除。
6. 隔离环境先完成演练。正式切换选择受控低流量窗口，继续保持 `credit_new_risk_frozen=true`，设置 `liquidation_enabled=true`，再通过同一安全入口 run-now 核对报价、还债、公开摘要、内部审计以及重试幂等。此时定时扫描也可能执行；run-now 不是绕过开关的测试入口。
7. 确认扫描、发布器、所有权和错误指标正常后，再解除 `credit_new_risk_frozen`，确认公示。

## 离线日利率维护

工具为 [`backend/scripts/change_loan_daily_rate.py`](../backend/scripts/change_loan_daily_rate.py)，实现见 [`rate_maintenance.py`](../backend/app/services/credit/rate_maintenance.py)。它要求已迁移的 PostgreSQL，不初始化数据库、不执行迁移。以下仅说明命令，实际生产执行仍需单独授权：

```bash
cd backend
python scripts/change_loan_daily_rate.py --rate 0.02 --operator-user-id 1
```

两个参数都必填。`--rate` 是严格大于 0、小于 1 的有限日利率；`--operator-user-id` 必须对应现有用户，用于记录操作者。示例数值不代表生产配置建议。

执行前备份并停止所有经济写入，包括写实例、调度器、后台消费者和维护脚本；仅设置 `credit_new_risk_frozen=true` 不够，还款、减仓、回补和强平仍可能写入。保持停写直到维护结果核对完成并重启写实例。工具在实际提交结息的同一 PostgreSQL 连接上取得事务级 advisory 所有权锁，与应用写实例的会话级所有权锁互斥；取得锁失败即拒绝维护，连接丢失也会失去所有权并回滚未提交事务。所有权锁不能替代停写要求。

工具使用同一 UTC 时点 T，按数据库中的旧日利率结算全部正金圆券债务和正外币欠币，将各计息时钟推进至 T，再修改 `loan_daily_rate`，一并提交计息审计、配置审计和受影响账户的经济版本。时钟无效、金额越界或配置/schema 不符合要求时拒绝并回滚。输出含旧/新利率、T 和受影响用户数，须与审计核对。离线进程清除自身配置缓存并不能更新其他进程；恢复服务时重启唯一写实例，确认启动缓存读取新值后再恢复写入。

## 回退

先冻结新增信用，保留统一估值、还款、安全减仓及必要强平能力，排查后向前修复。
当前版本没有旧信贷模式切换开关；存在 FX 抵押债务时，LMSR-only 旧强平无法处理现有组合风险。
生产不删除 run/action 记录。降级迁移只在可丢弃的隔离库演练，不能作为资金系统线上回退方案。
若必须回旧代码，停服并恢复与旧代码匹配的完整数据库备份，明确备份之后交易的恢复或补偿安排；只切分支、切开关或执行 downgrade 均不充分。
演练必须记录备份恢复、审计一致性和余额检查结果；此文不声称已完成该演练。

## 接口

`/user/summary` 的 `fx_wallets` 按 pair 返回币种、余额和账面金圆券价值。同时返回两种净值、含息债务、两条门槛、风险状态和冻结状态，保证金率使用 `equity_to_risk_basis`。
`/loan/liquidation-policy` 返回运行中的统一门槛、强平开关、卖出费率和扫描周期；旧策略字段与 `legacy` 对象已移除。
`/loan/recent-liquidations` 仅新增可空 `product`，不公开 run/action 内部恢复信息。

### 贷款操作与账户刷新

借款、还款操作响应保留 `cash`、`debt`、`effective`，`max_borrow` 可以为 null：借款复用准入计算结果，还款不为填充额度再读取完整组合。`effective` 表示实际生效金额，还款会按真实债务和现金封顶。响应在资金事务提交前构造并校验，提交后不再读取持仓或额度。GET `/loan/quota` 的 `max_borrow` 仍为非空数值，操作响应不能代替账户快照。

前端收到操作成功结果后展示实际结果，再 GET quota 一次。若该刷新失败，提示“操作已完成，账户信息刷新失败”，清空旧 quota，避免用旧现金或旧额度继续提交。超时、断网或提交结果未知时不自动重发借还款或 LMSR 买卖；先刷新账户并核对，但刷新本身无法证明某个原请求是否执行。用户手动再次提交按新操作处理。贷款和 LMSR 暂无请求编号、去重或原结果重放，见[交易请求编号与重试去重待办](superpowers/specs/2026-10-10-trade-request-idempotency-deferred.md)。

### 冻结与风险等级

quota 和 UserSummary 返回 `credit_frozen`（账户信用冻结）、`new_risk_frozen`（运营冻结新增风险）和 `borrow_blocked_reason`。借款原因按以下优先级返回：`loan_disabled`、`frozen_by_operator`、`credit_frozen`、`valuation_unavailable`、`insufficient_initial_margin`、`no_borrow_headroom`；没有阻塞时为 null。`blocked_reason` 仍表示估值原因。冻结与保证金等级分别展示，healthy 账户也可能冻结；冻结不阻断还款、安全减仓和回补。

账户 `risk_status` 为后端精确计算的 `healthy`、`warning`、`danger` 或 `blocked`，组件直接采用该等级，不用展示百分比或量化后的风险基数重新判级。读取失败展示 unknown，不能沿用旧快照判定当前状态。FX 空头报价的 `margin_status` 表示交易后保证金等级；原 `risk_status=ok/blocked` 表示估值是否可用，`executable` 表示能否执行，两者继续保留，不由保证金等级反向禁止安全回补。账户快照和公开明细投影用于展示，借款冻结原因与实际交易准入分别检查。

### 扫描观测

`soft_warning_count` 统计本轮已扫描、完整估值且判为 warning 的用户；`valuation_blocked_count` 统计初筛估值阻塞用户，`execution_blocked_count` 统计执行器阻塞用户，`blocked_count` 为两类用户集合并集人数。同用户在两阶段都阻塞只计一次，不能直接相加。

`retry_exhausted_count` 单独统计执行重试耗尽，不再计入普通 `skipped_count`；执行器 blocked 仍计入 `skipped_count` 以兼容旧统计，因此各字段并非互斥。`triggered_count`、`recovered_count`、`errors`、`deadlocks` 保留。重试耗尽仍等待下一轮扫描，不无限重试，不通过行情触发强平。`valuation_duration_ms` 为批量估值耗时；`execution_duration_ms` 与 `max_user_execution_ms` 为含等待的端到端执行耗时，不是行锁持有时间。

本轮修复范围见[可靠性设计](superpowers/specs/2026-10-10-unified-credit-reliability-design.md)。[2026-10-10 验证记录](unified-credit-reliability-validation-2026-10-10.md)记录本轮回归、隔离查询与扫描测量及未验证范围，不能据此宣称已通过发布门槛。
