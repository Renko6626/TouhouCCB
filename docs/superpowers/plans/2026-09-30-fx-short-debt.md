# FX 做空与统一债务风控 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本计划按七个有业务边界的工作包组织；每包完成后检查实际账务，再进入依赖它的下一包。

**Goal:** 让玩家从 FX treasury 借外币卖入真实 AMM，并在统一保证金下回补、计息和强平，同时保持金圆券贷款、现货及既有消费入口的资金安全。

**Architecture:** `FxShortPosition` 保存按 pair 的真实外币义务和锁定售空所得，`User.cash/debt` 保持原语义。FX 事务内执行器负责钱币守恒；统一风险层按整组卖出净回收 A、整组买足成本 K、含息金债 D 计算 E/B。定时强平延续现有门闩、经济版本和每账户每轮一个资产组；未知 K 有独立阻塞状态。

**Tech Stack:** Python / FastAPI / SQLModel + SQLAlchemy async / Alembic / PostgreSQL 与 SQLite / Decimal / pytest；Vue 3 + TypeScript / Vitest。

**Spec:** [2026-09-30-fx-short-debt-design.md](../specs/2026-09-30-fx-short-debt-design.md)。先读 spec，尤其 §3–8、§11–14；旧 [统一信贷计划](2026-09-30-unified-credit-risk.md) 中“只有一笔金债”和“现金优先还债”仅适用于尚无空头的现状，以新 spec 为准。

## Global Constraints

- `User.cash` 包含空头锁金；`User.debt` 仅指金债。`FxWallet` 非负且只记现货；`FxShortPosition` 的本金、利息、锁金与收益基准都是非负六位 Decimal。任何提交态满足 `0 <= Σrestricted_gold <= User.cash`。
- 真实外币借自同 pair `FxTreasury.foreign_balance`，本金受 `short_lending_limit_foreign` 约束；外币利息沿用 `loan_daily_rate`，不得重复写入金债。金、外币实物守恒；拒单/重试不留单边账。
- 风控用 `E=C+A-D-K`、`B=max(D,(L-1)/L*A)+(L-1)/L*K`；比较用 spec §6 的整数乘法等价式，不用展示舍入值决策。无空头账户的旧准入、触发和恢复判定须等价。
- 同 pair 不同时持有现货与欠币；有空头时 spot buy 不建立多头，须走 cover。售空所得只准回补本仓；不得还金债、消费或被另一仓支用。强平不自动借新金债。
- 仅定时扫描；每用户每轮最多一个资产组动作。最大空头无付款能力时跳过它、卖其他可执行资产；有待回补空头时保留可用现金。K 未知先建立/续接 active run，不把未知写成零、判恢复或判坏账。
- 沿用 OWNERSHIP 单写守卫、GATES 全序、pair→User→钱包/空头→treasury 行锁序；版本及依赖集合在锁内重验。用户事务内执行器不 commit、不发 SSE，提交后发布。不得加全站经济锁或事件触发清算。
- 新开空总闸默认 `false`、pair 本金额度默认零；完整读写和强平能力到位之前不得打开。`fx_enabled=false` 的用户交易全停语义、`reduce_only` 允许回补语义见 spec §9。
- 永久测试只证明真实故障：资金/债务守恒、授信和用途边界、幂等、强平推进、迁移数据保留与回滚、并发账务。简单展示改动仅构建和人工最多截图验证；不加源码文字、迁移头位置、组件内部结构等脆弱断言。不启动浏览器自动交互。

## Review Focus

以下五项是容易在正常改动中回归、且直接影响资金或债务的条件；对应测试放在所属工作包，不另建重复的“覆盖率任务”。

| 条件 | 预期 | 归属 |
| --- | --- | --- |
| 全回补报价后利息又增加 | 锁内重新取含息欠币；一次回补后不会意外留尾仓或借币两次 | WP3 |
| 同 key 的现货、开空、回补请求交错重试 | 参数冲突报错，同一合法请求只落账一次 | WP3 |
| 最大空头现金为零，另有可卖资产 | 本轮跳过空头、卖一组资产，下轮可用所得回补 | WP4 |
| `Q>=F` 且此前没有 run | 创建唯一 active run、拒增险、保留欠币；部分回补不伪称恢复 | WP4 |
| 管理员注撤资/关闭 pair 与欠币并发 | 行锁下更新储备与版本；有欠币时拒绝关闭/归档/删除，注撤资造成未知报价则走阻塞流程 | WP5 |

---

### Task 1: WP1 持久化、外币计息与 exact-output 报价

**Files:** `backend/app/models/fx.py`、`backend/app/models/credit.py`、`backend/app/services/fx/amm.py`、`backend/app/services/fx/shorts.py`（先放纯计息）、新增 Alembic revision（接当前 head `fx_pair_archive_20260930`）、`backend/tests/test_fx_migration.py`、`backend/tests/test_fx_amm.py`、`backend/tests/test_credit_pending_debt.py`。

**Interfaces:** `FxShortPosition(user_id,pair_id,principal_foreign,interest_foreign,interest_last_accrued_at,restricted_gold,proceeds_basis_gold)`；`quote_buy_exact_out(foreign_out: Decimal, gold_reserve: Decimal, foreign_reserve: Decimal, fee_rate: Decimal) -> FxQuoteMath`；`pending_short_debt(position: FxShortPosition, daily_rate: Decimal, now: datetime) -> Decimal`、`accrue_short_interest(position: FxShortPosition, daily_rate: Decimal, now: datetime) -> Decimal`（返回新增利息，调用方持锁、负责审计与版本）。迁移为后续 WP 预留 `FxTrade.purpose`/请求身份列、pair 额度、强平 cover 字段和可空风险快照；旧成交默认 `spot`。

- [ ] **写失败测试：** 用真实 Alembic upgrade/downgrade 验证旧现金/钱包/成交保留、新表约束和“有欠币/锁金拒绝 downgrade”；以 `q` 接近 `F`、最小单位、非零买费验证 exact-output 买足且 `G'F'≥GF`；验证旧本金结息后追加新本金不追收旧时段利息。测试验证 schema/数据行为，不断言“某迁移永远是 head”。
- [ ] **运行红灯：** `cd backend && venv/bin/python -m pytest -q tests/test_fx_migration.py tests/test_fx_amm.py tests/test_credit_pending_debt.py`；确认新增用例因缺功能失败，既有用例不被误改。
- [ ] **实现：** 新表含唯一 `(user_id,pair_id)`、非负与时间状态约束、按用户/pair 索引；预检负 `FxWallet` 中止迁移。用 spec §5 的两次向上量化计算净金及总金，验证费用、储备、精度和 Numeric 存储范围；计息复用金债的时间/量化语义。`LiquidationAction.kind` 增 `cover_group` 并迁移 CHECK，原有历史值仍可读。
- [ ] **绿灯与提交：** 重跑上述测试和 `git diff --check`，检查升级后写一笔债的降级拒绝及清债后的降级；只提交本包文件。此包不启用开空。

### Task 2: WP2 统一估值、授信与现金用途边界

**Files:** `backend/app/services/credit/{risk,valuation,thresholds,fx_quote,flags}.py`、新增 `backend/app/services/credit/cash.py`、`backend/app/services/{loan_service,loan_sweep,redemption,danmuku,writer_ops,admin_user_service}.py`、`backend/app/api/v1/loan.py`、`backend/app/services/fx/trading.py`；测试沿用 `test_credit_{risk,valuation,thresholds}.py`、`test_credit_wp6b_consumption.py`、`test_credit_wp6b_admin.py`、`test_loan_sweep.py`，必要时增加一个跨产品场景文件。

**Interfaces:** `restricted_cash(session: AsyncSession, user_id: int) -> Decimal` 返回已持久化锁金之和；`available_cash(session: AsyncSession, user: User) -> Decimal` 返回 `User.cash−Σrestricted_gold`，若负值则抛账务错误。`AccountValuation` 增 `short_cover_cost: Decimal | None`、`risk_basis: Decimal | None`、`available_cash`、`restricted_cash`、`risk_status`、`blocked_reason`，其中 `liquidation_equity` 在未知 K 时为可空；`DependencySet`/`PostTradeState` 纳入按 pair 的含息空头与交易后储备。现有 `check_new_risk` 对外调用形状尽量保留。

- [ ] **写失败测试：** 同样的 C、A、D 在无空头时判定与旧门槛一致；有空头时售空所得已在 C 内只算一次、真实买足 K 含费和滑点；纯空头金债为零时跨市场买入/借现金仍受准入；还金债、兑换、弹幕和管理员扣款不能使 `C<S`；K 不可报价时增险拒绝且净值为 `None`。只保留覆盖不同故障的场景，不为每个调用入口复制同一断言。
- [ ] **运行红灯：** `cd backend && venv/bin/python -m pytest -q tests/test_credit_risk.py tests/test_credit_valuation.py tests/test_credit_wp6b_consumption.py tests/test_credit_wp6b_admin.py`，记录新增用例失败原因。
- [ ] **实现：** 批量加载空头、两侧费率和版本，按 pair 报整组 K；用精确乘法比较 §6 门槛并向下量化可借额度。统一现金服务卡住所有实际扣款和还金债路径；原有无债快路径增加“无任何欠币”的锁内条件。扩大定时结息候选集合，纯结息只改变债务和经济版本。卖出/回补的减险准入与普通买入的增险准入分开；未知报价拒绝增险，不误伤允许的充值和减仓。
- [ ] **绿灯与提交：** 运行本包测试及受影响的现有 `test_loan_service.py`、`test_credit_wp6a1_admission.py`、`test_credit_wp6b_fx_ops.py`；检查无空头旧行为和 SQL 批量读取，提交本包文件。开空闸仍关闭。

### Task 3: WP3 真实开空/回补事务、API 与幂等审计

**Files:** `backend/app/services/fx/shorts.py`、`backend/app/services/fx/trading.py`、`backend/app/api/v1/fx.py`、`backend/app/schemas/fx.py`（或独立 `fx_short.py`）、`backend/app/services/audit_service.py`、`backend/app/services/site_config.py`、`backend/app/api/v1/site_config.py`、`backend/app/models/fx.py`；测试集中到 `backend/tests/test_fx_short_trading.py` 与现有 `test_fx_api.py`。WP1–2 的模型、报价和准入接口是本包前提。

**Interfaces:** `execute_short_open_in_session(db, *, user_id, pair_id, foreign_amount, min_gold_out, idempotency_key)` 与 `execute_short_cover_in_session(db, *, user_id, pair_id, foreign_amount: Decimal | None, cover_all: bool, max_gold_in, idempotency_key)` 返回成交与提交后发布所需数据；调用方拥有事务，执行器不 commit/SSE。玩家路径见 spec §10：`short/quote`、`short/open`、`short/cover`、`GET short`，资金字段使用十进制字符串；现有 spot 请求/响应语义不变。

- [ ] **写失败测试：** 一个端到端资金账例覆盖开空、追加、计息、部分/完整回补：逐币种核对 pool+treasury+用户实物总量、欠币、S 与收益基准；覆盖 `cover_all` 锁内新利息、其他仓 S 不可挪用、亏损时本仓额外锁金可用。另一场景覆盖同 key 崩溃重试和跨 purpose/参数冲突，验证只产生一次借币、成交、退款/归还和审计。再覆盖同 pair 现货/空头互斥与 `reduce_only` 下可回补。
- [ ] **运行红灯：** `cd backend && venv/bin/python -m pytest -q tests/test_fx_short_trading.py tests/test_fx_api.py`；新增用例应因缺端点/执行器失败。
- [ ] **实现：** 新增默认关闭的全站开空配置、pair 零借出额度管理，遵守贷款闸；pair→User→空头/钱包→treasury 锁序内统一结息、重新报价、检查本金上限/库存/交易后风险；原子更新钱币债锁金、`FxTrade.purpose`/请求身份、审计和版本。spot 与 short 共用 `(user_id,idempotency_key)` 冲突检测；`cover_all` 重试返回原成交，不操作后来新开的仓；提交后异步公开真实成交。用户回补资金不足明确拒绝，强平预算回补留给 WP4。
- [ ] **绿灯与提交：** 运行本包测试与 `test_fx_trading_in_session.py`、`test_fx_publication_e2e.py`；验证失败/回滚不改库存、债、现金或锁金，再提交。资金费、借出/归还外币必须已经写入可重放审计包，WP5 做全历史重放对账。

### Task 4: WP4 定时强平、未知估值与坏账续接

**Files:** `backend/app/services/credit/{sweep,execution,runs,valuation}.py`、`backend/app/services/fx/shorts.py`、`backend/app/models/credit.py`、`backend/app/services/credit/flags.py`；测试集中 `backend/tests/test_fx_short_liquidation.py`、现有 `test_credit_{sweep,execution,runs}.py` 与 `backend/tests/pg/test_pg_credit_liquidation.py`。依赖 WP1–3。

**Interfaces:** `execute_liquidation_cover_in_session(db, *, user_id, pair_id, run_id, round_no, planned_amount, max_gold_budget)` 使用 WP3 的原子账务内核；不 commit/SSE，按真实成交量更新动作后态。`AccountValuation` 的可空 E/B 先经完整性分支，数值门槛只接受完整估值。

- [ ] **写失败测试：** spec §8.1 的 `A1=A2=600、K=1500、C=S=400、L=10、m=0.05` 场景：现金用尽后下一轮能跳过空头并卖资产，所得留待回补；D>0 时不先被自动还债耗光。覆盖 `Q>=F` 从无 run 创建唯一 active run、连续扫描不重复建 run/扣款、恢复报价后按初始门槛续接；完整报价溢出时只做固定比例的资金受限有效回补，余额不足不清零债务。验证每轮仅一个成交组及相同 `(run_id,round_no)` 不重复执行。
- [ ] **运行红灯：** `cd backend && venv/bin/python -m pytest -q tests/test_fx_short_liquidation.py tests/test_credit_sweep.py tests/test_credit_execution.py`。
- [ ] **实现：** 扫描候选加入外币义务；已知 E/B 用共享门槛，未知 K 用 spec §8.2 的状态表；按绝对金额排序但跳过本轮无法成交的组。出售多头后在有待回补空头时留存未锁现金；回补用本仓 S+未锁现金并记录 `limited_by_cash`。只有完整可信估值及实际资产耗尽才能判资不抵债；清仓/报价恢复按 §8 的门槛关闭 run。扩展 action/event 的可空快照、cover 金支出和外币归还，历史字段语义不变。
- [ ] **绿灯与提交：** 运行本包测试和现有强平回归；有独立测试 PG 时运行 `cd backend && TEST_PG_DATABASE_URL=... venv/bin/python -m pytest -q -m pg tests/pg/test_pg_credit_liquidation.py`，没有则记录未验证，绝不把 skip 计作通过。检查锁内重验及无连接等待 writer，提交。

### Task 5: WP5 全站运营入口、审计重放与生命周期

**Files:** `backend/app/services/{audit_replay,admin_user_service,site_config}.py`、`backend/app/api/v1/{admin_fx,site_config}.py`、`backend/app/services/fx/{scheduler,engine}.py`、`backend/scripts/season_reset.py`、启动能力检查所在 `backend/app/services/credit/flags.py`/`backend/app/main.py`；测试复用 `test_fx_audit_replay.py`、`test_admin_fx.py`、`test_fx_season_reset.py`、`test_credit_startup_gating.py`，仅在真实行为缺口处增加用例。依赖 WP1–4。

**Interfaces:** 管理员按 pair 全额核销余下外币义务（本金、利息、锁金、收益基准和审计一起更新，不向 treasury 增币）；pair 借出上限管理默认零。金债免债保持原语义，核销外币是独立动作；重置/归档/降级检查真实空头行与锁金。

- [ ] **写失败测试：** 保存几笔开空、计息、回补和核销后，审计从历史截止点重放到 live 钱币债锁金一致；篡改本金/锁金/库存可检出。管理员有欠币时关闭/归档/删 pair、现金重置到 S 以下被拒；完整核销不铸外币。注撤资与外币义务并发时重验储备及版本，若完整报价变未知，债务仍保留且进入阻塞。迁移与赛季重置测试验证旧历史保留、空头清理后的库存/审计锚点一致、回退旧实例前的能力门阻止有债写入。避免固定迁移号或源码字符串断言。
- [ ] **运行红灯：** `cd backend && venv/bin/python -m pytest -q tests/test_fx_audit_replay.py tests/test_admin_fx.py tests/test_fx_season_reset.py tests/test_credit_startup_gating.py`。
- [ ] **实现：** audit replay 按 purpose 重放借币、售空、回补、外币利息和显式核销；各资金入口统一校验 `C>=S` 和有债禁消费；pair 状态、费率、注撤资在门闩及 pool_version 下重验。旧率维护只在停写条件下以同一 T 完成全金/外币结息并审计；重启与回滚门检查存量欠币/锁金，不能仅凭新 flag 保护旧实例。赛季重置处理新表和审计锚点。
- [ ] **绿灯与提交：** 运行上述测试及 `test_credit_wp6b_admin.py`、`test_redemption_debt_guard.py`；检查反例实际被拒，提交。

### Task 6: WP6 玩家读模型、财富口径与最小 UI

**Files:** `backend/app/api/v1/{loan,user,market,admin_stats,fx}.py`、`backend/app/services/fx/valuation.py`、相关 Pydantic schemas；`thccb-frontend/src/{api/fx.ts,types/fx.ts,pages/Fx.vue,pages/loan/Loan.vue,stores/loan.ts}`，按实际组件位置补资产/账户头部；如确有消费榜和称号财富计算入口一并更新。依赖 WP2–5 的权威状态。

**Interfaces:** `cash` 仍为总现金，`debt`/`debt_with_interest`/`equity_to_debt` 仍为金债；新增 `available_cash`、`restricted_cash`、`short_positions`、`short_cover_cost`、`risk_basis`、`equity_to_risk_basis`、`risk_status/blocked_reason`。旧 JSON 字段类型不批量改动；新交易输入由十进制字符串送出，不经 JS Number 重建。

- [ ] **验证目标先明确：** 唯一值得持久化的读模型用例是“售空所得不刷高榜单/称号净值，未知 K 不被当零，旧 debt 字段仍只报金债”；放在现有财富/用户 API 测试中。UI 文案、布局、组件分支不加结构性永久测试。
- [ ] **运行红灯：** `cd backend && venv/bin/python -m pytest -q tests/test_credit_presentation.py tests/test_wealth_mtm.py tests/test_wealth_stats.py`，先让新增业务用例失败。
- [ ] **实现：** 统一展示净值口径扣外币边际义务，风险读模型展示可执行 K 或 blocked；FX 页明确分“卖现货/开空/回补”，显示本仓欠币、锁金、回补成本与真实收益基准。贷款页显示总/可用现金并限制可还金额；资产/排行榜/称号统计复用同一债务扣减定义，不重复统计售空所得。
- [ ] **绿灯与提交：** 后端目标测试通过；运行 `npm --prefix thccb-frontend run build` 与已有相关单测。UI 验证遵守项目约定，默认最多看截图，不自动点击或填表。提交。

### Task 7: WP7 跨产品验证与可回退发布准备

**Files:** 按发现的真实缺口修补对应文件；记录检查证据到 `docs/fx-short-debt-validation-2026-09-30.md`（或执行日期）。依赖 WP1–6；这是验收包，不为检查本身新增永久测试。

- [ ] **账务与功能回归：** 跑受影响后端测试集、前端构建；用同一测试库检查开空→价格变动→强平/回补→审计重放的真实后态。任何失败先定位对应 WP 修复，再重跑该故障及相关回归。
- [ ] **PG 并发：** 在明确的临时测试库验证开空与 spot 买、金债借还、消费、计息、系统 tick、pair 状态/费率修改交错；检查 `C>=S`、债务与 treasury 守恒、幂等及锁序。缺 PG 环境时记录未完成，不能以 SQLite 结果替代上线门槛。
- [ ] **迁移与性能：** 用旧数据副本证明 upgrade 保留数据、down 有活跃欠币拒绝；检查开空总闸关着时旧交易可用，回补及强平仍能服务存量。相同机器/PG/数据/负载比较旧基线与新版本无新增信用交易 P95/P99 和吞吐，约 5% 相对回归目标，并报告 50/100 用户多 pair 扫描耗时与 SQL 次数；拒单率同时报告，不用更高拒单率换延迟数字。
- [ ] **发布材料：** 写备份、停经济写/调度器、升级、账务对账、保持开空关闭回归、运营显式设额度后逐步开启的步骤；写关闭开空、清仓后回旧镜像的可执行退路。此 WP 只备齐发布证据，不执行部署/推送或切换线上配置。

## 执行与验收说明

WP1→WP2→WP3→WP4 是关键依赖链；WP5、WP6 在权威交易与风险接口稳定后可按文件所有权并行，WP7 最后执行。若采用 subagent，每包一名实现者负责列出的文件，任务提示必须重复 Global Constraints、文件所有权、验收命令和“共享工作区，不回退他人修改”；交接时检查实际 diff、测试输出和锁序。若由同一实现者执行，保持相同工作包边界即可。

每包的测试先写能够证明真实经济故障的失败用例，随后实现、运行目标测试并审查 diff；通过后再进入下一包。不要把每条文案、每个常量、每个 API 属性分别拆成永久测试或子任务。完整 spec 是最终验收依据，计划中列出的测试是最小关键集，不替代上线前的 PG 并发与账务对账。
