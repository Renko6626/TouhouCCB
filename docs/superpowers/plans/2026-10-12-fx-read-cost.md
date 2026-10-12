# FX 报价与交易读取精简 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. 用户已批准第一阶段实施；实现用 gpt-6.1-sol low，任务及最终审阅用 gpt-6.1-sol medium。提交、推送、部署仍待后续指令。

状态：Tasks 0–4 完成，最终 medium 审阅 spec/quality PASS、整体 Ready；交付隔离工作树，未 commit/push/deploy。

**Goal:** 合并 FX 报价基础读取与请求查重，停用用户已确认取消的合计借币上限；保留真实库存、资金及个人风险检查。

**Architecture:** 沿用数据库权威的 FX net 编排、现有回执模型和统一风控。第一阶段改变只读查询组织，并取消已确认的总量上限检查；旧列和管理员 API 字段保留兼容，不新增迁移。交易依赖复用、前端报价流程与公开参数属于后续候选，尚未批准实施。

**Tech Stack:** FastAPI / SQLAlchemy / SQLModel / PostgreSQL / SQLite / Vue 3 / TypeScript；不新增依赖。

**Spec:** [2026-10-12-fx-read-cost-design.md](../specs/2026-10-12-fx-read-cost-design.md)。基线 `29c1250`，分支 `feat/fx-net-trading`。

## 本阶段执行范围

顺序执行 Task 0–4，先完成报价读取、再合并查重、再停用合计上限，最后做一次最终相关验证与记录。执行时每步检查自己影响的行为；未再修改的检查不因进入下一任务机械重跑。若由 subagent 实现，保留用户的 sol medium 偏好，明确下面文件所有权，不并行修改共同的 `net_trading.py`。

| 文件/模块 | 所有权与允许改动 |
| --- | --- |
| `backend/app/services/fx/net_trading.py` | 三项局部改动：报价基础加载器、请求身份加载器、删除合计上限检查 |
| `backend/app/services/fx/shorts.py` | 只删除旧报价/旧执行的合计上限检查及不再使用的汇总 helper |
| `backend/app/services/fx/trading.py`、`fx/amm.py`、`credit/risk.py`、`loan_service.py` | 本阶段只读；保留已有定价、现货记账、风险和借还款核心 |
| `thccb-frontend/src/pages/admin/FxManage.vue` | 移除上限表单值、读取赋值、验证、发送和显示；不改玩家交易 UI |
| 现有 net/short/API/PG 测试 | 复用夹具和有效断言，仅修正明确改变的上限行为或补具体缺口 |
| `docs/fx.md`、`docs/api.md`、本计划、配套设计与最终验证记录 | 同步停用行为、边界、实测成本和实施状态 |

继续调用原 `_write_spot_ledger`、`_write_open_ledger` 和 `_write_cover_ledger`。底层只 flush、不自行提交；不把跨零拆成两笔提交，不复制 AMM、手续费、还债或审计代码。详见设计“服务分层与复用边界”。

## Global Constraints

- 保留原 AMM、六位金额、Decimal 字符串、手续费舍入、利息和保证金公式。
- 保留金库范围/库存、全部本人短债/锁金、异常双方向、状态与资金检查。
- 保留反向先平仓、跨零原子性、增仓许可、经济版本、滑点底线及请求重放顺序。
- 保留品种先于 User 的锁序、完整门闩集合、等待释放连接、有限重试和权威风险读取。
- 第一阶段不改 schema、HTTP 契约、350ms 报价时机、交易依赖发现、账务、审计和发布边界。
- 不读取生产库、不施加生产负载、不启动浏览器交互；不自动提交、推送或部署。
- 复用已有检查；永久新测试只覆盖明确行为缺口，不固化查询文字/当前文件位置。
- 所有验证命令显式设置新的临时 `DATABASE_URL`；不得继承生产连接。PG 并发另设隔离 `TEST_PG_DATABASE_URL`。

## Review Focus

1. 外连接不得把缺钱包/缺金库的合法品种丢掉，也不得泄漏其他用户钱包（Task 1）。
2. 合并读取仍能发现手续费令金库越界、其他品种欠币或锁金导致资金不足（Task 1）。
3. User 与品种缺失、已认证 identity-map 和直接 service 调用保留既有语义（Task 1）。
4. 旧入口编号冲突、同编号参数不一致及已成交重放优先于版本检查（Task 2）。
5. 组合查询减少往返，但不能以减少数目为由删除权威风控、锁内发现或回执（Task 1–2）。
6. 取消合计上限后，旧上限为 0 不再阻止开空；库存不足、并发库存约束和个人风控仍拒绝（Task 3）。

## 现有覆盖与需要填补的缺口

| 实际行为 | 优先复用的已有用例 |
| --- | --- |
| 跨零第二腿失败整笔撤销、底层记账失败回滚 | `test_second_leg_failures_leave_no_money_or_receipt`、`test_later_ledger_error_rolls_back_already_written_first_leg` |
| 借新金币只用于新多头、含息回补和同一计息时点 | `test_cross_zero_buy_repay_interest_and_finance_only_long`、`test_net_uses_one_interest_timestamp_and_keeps_audit_replay` |
| 其他品种锁金不能用于本仓回补 | `test_net_cover_cannot_spend_sibling_locks_or_new_loan` |
| 请求重放、跨旧入口编号冲突 | `test_cross_zero_sell_receipt_replay_and_money`、`test_key_namespace_conflicts_between_net_and_legacy` |
| 并发净交易同编号仅一份回执、net/旧入口争同编号 | PG `test_net_same_key_cross_zero_commits_one_receipt`、`test_net_and_legacy_contend_for_shared_key` |
| 单次库存不足拒绝且状态不变 | `test_insufficient_treasury_changes_nothing`（由原 `test_insufficient_treasury_or_limit_changes_nothing` 用例调整）保留库存拒绝，原上限参数改按新规则验证 |

报价合并的缺关联行、其他用户钱包及手续费令金库越界若现有覆盖不足，集中在 net 服务数据库行为用例补齐，不在 API 层完整复制。库存并发尚无精确覆盖，Task 3 在 PG 层补一个不同用户、混合入口的真实竞争场景。

## Task 0：冻结范围和成本基线

**Files:** 本计划、配套设计；临时 SQL 计数脚本和 JSON 在 `/tmp`，不新增永久验证工程。

- [x] 用户已审阅并批准第一阶段，指定 subagent-driven；后续候选保留待确认状态。
- [x] 确认隔离工作树、Git 状态与基线，记录既有文档改动。
- [x] 复用 `/tmp/thccb-api-sql-probe-20261012.py` 对同一小账户/储备/持仓运行真实 HTTP 冷热计数，输出到新的临时文件；不覆盖原基线。
- [x] 扩充一次性场景记录缺钱包/缺金库、纯回补、跨零、有金债、有其他品种短债及查重重放；计数记录 SELECT/DML 和响应，不引入时间阈值。
- [x] 原临时脚本目前输出文件名固定，先复制到本任务 `/tmp` 并改为显式选择输出文件；每个前后场景恢复初始状态、固定同一 UTC 模拟时点。只保留本任务 JSON/脚本作为诊断证据，不搭建永久性能框架。

**验收：** 可复现的基线与样本条件；无债 6、有债样本 18 为原采样参考，不强制所有账户等于该数值。

## Task 1：合并报价基础读取

**Files:** 修改 `backend/app/services/fx/net_trading.py`；按覆盖缺口修改 `backend/tests/test_fx_net_trading.py`，复用 `backend/tests/test_fx_api.py`。

**Interfaces:** 保留 `quote_net_trade(db, user_id: int, pair_id: int, req: FxNetQuoteRequest) -> FxNetQuote`；新增内部 `_read_quote_base(db, user_id: int, pair_id: int) -> tuple[FxPair, User, FxWallet | None, FxTreasury | None]`。

- [x] 复核现有行为用例；只为未覆盖的缺关联行/其他用户钱包/金库手续费越界新增简短数据库行为用例，断言响应和资金不变。
- [x] 使用 FxPair 主表与限定用户、品种的外连接一次读取基础四实体，验证至多一行；保留原品种 404、User 缺失和 ORM 默认刷新语义。
- [x] `quote_net_trade` 调用新加载器后仍单独读取全部本人短仓，继续调用原 `_simulate`；不改配置、风控或模拟算法。
- [x] 在 `backend` 运行隔离库已有检查：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-fx-read-cost-quote-20261012.db venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_api.py tests/test_fx_amm.py tests/test_credit_risk.py`，通过后停止扩展无关验证。
- [x] 重跑对应临时 SQL 样本并核对同初始状态下报价字段/拒绝原因，允许剔除实时时间字段后比较；目标普通 warm 6→3、有债同样本 18→15。

**验收：** 查询往返减少，数据内容、缺行行为、报价可执行性及风险检查保留；报价路径没有 DML。固定样本目标已实测，见最终验证记录；不承诺毫秒或吞吐收益。

## Task 2：合并交易请求查重

**Files:** 修改 `backend/app/services/fx/net_trading.py`；复用 `backend/tests/test_fx_net_trading.py`、`backend/tests/pg/test_pg_fx_short_concurrency.py`。

**Interfaces:** 保留 `execute_net_trade(db, user_id, pair_id, req)`；新增内部 `_read_net_request_identity(db, user_id: int, key: str) -> tuple[FxExecution | None, bool]`，bool 表示旧成交编号存在。

- [x] 复用原重放、参数冲突、跨旧入口及并发用例；若缺“无新回执但旧编号存在”的行为覆盖，只补该缺口。
- [x] 在原 User 锁内位置，用常量单行源、回执外连接和旧编号 EXISTS 合成一次 SELECT；不把事务锁或查重挪出原位置。
- [x] 保留原回执优先、参数比较、rollback 重放、旧编号拒绝及先重放后查版本的顺序；原有完整发现/风控、利息和分腿执行不变。
- [x] 运行相关 net/API 及已有隔离 PG 并发组，报告实际命令、结果和未运行项；仅使用可丢弃数据库。
- [x] 临时记录无债、有债、跨零、原编号重放、旧编号冲突的 SELECT/DML 与结果。无债每成功轮目标少 1 SELECT；有债通常首试和重试各少 1，冲突路径分别报告。

**验收：** 正常与重放结果不变、仅一次财务提交、并发查重约束保留；不得用删除有效行为断言换取通过。

## Task 3：停用合计借币上限

**Files:** 修改 `backend/app/services/fx/shorts.py`、`net_trading.py`、`thccb-frontend/src/pages/admin/FxManage.vue`、`docs/fx.md`、`docs/api.md`；复用相关 short/net 行为及 PG 并发测试。模型、迁移、管理员请求/响应字段不变。

**Interfaces:** 新旧开空及报价签名不变；`_pair_principal_sum` 若没有其他调用则删除。`short_lending_limit_foreign` 仅为兼容保留，不参与准入。

- [x] 在旧开空执行、旧开空报价及 net 模拟中移除合计本金查询和上限判断，保留相邻库存、数值、开关与风险检查。
- [x] 管理页面移除上限输入、校验与 patch 发送；保留管理员 API 旧字段兼容，并在文档注明不再生效。
- [x] 在最合适的现有 short 行为层验证：旧上限为 0 或小于借币量，库存与风险允许时仍开空成功；实际库存不足和关闭开空仍拒绝。原 cap 拒绝用例按新业务规则调整，不机械新增各层重复场景。
- [x] 复用 `test_fx_short_api.py` 中开关矩阵，将旧“上限 0 拒绝”改为不被上限阻止；继续保留开空关闭、只减仓、冻结、回补和管理员旧字段有效格式/范围兼容的断言。
- [x] 在 `tests/pg/test_pg_fx_short_concurrency.py` 增加 `test_competing_short_opens_cannot_overborrow_treasury`：两名不同用户分别经 net 和旧开空服务竞争同一库存，设零费率、初始库存 100、每笔借 80、风险资金充足；断言一笔成功、一笔库存不足、最终库存 20、合计新本金 80、只有一次成交与相应审计、失败用户无财务变化。调用真实生产路径，不替换锁或风险为 mock。
- [x] 运行相关 net/short API、short trading 与原 PG 并发组；前端运行类型检查、改动文件只读 lint、相关已有单测和构建。无浏览器交互。
- [x] 临时记录含新开空的报价和执行查询数，证明不再做全品种本金汇总；原无新开空路径的收益保持 Task 1–2 口径。

**验收：** 上限停用在新旧入口一致生效，真实库存和个人风险仍约束借币，现有本金/利息/锁金/回执和生产数据保留。

## Task 4：记录收益与交付

**Files:** 更新本计划及配套设计实施状态；新增 `docs/fx-read-cost-validation-2026-10-12.md`，只在实际实施后生成。

- [x] `git diff --check`，核对仅授权范围的改动。
- [x] 写明前后输入、配置冷热、SELECT/DML、数据规模、各检查命令和真实结果；不把 SQLite 查询计数当作 PG 压测。
- [x] 明确仍保留的有债依赖重发现、利率/风险权威查询，以及尚未实施的前端方案。
- [x] 异常 JOIN 停止扩展条件未触发；PG EXPLAIN/生产耗时未验证，限制已记录。未来若出现行量或耗时异常，保留证据并停止扩大范围，不凭 SELECT 减少宣称更快。
- [x] 交付可审阅隔离工作树；提交、推送和部署依后续指令处理。

### 最终相关命令

后端在 `backend` 执行，下述路径均为本次新建可丢弃文件；PG URL 由执行者实际创建的独立库提供，不使用生产配置：

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-fx-read-cost-final-20261012.db venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_api.py tests/test_fx_short_trading.py tests/test_fx_short_api.py tests/test_fx_amm.py tests/test_credit_risk.py tests/test_fx_audit_replay.py tests/test_fx_short_audit_replay.py
```

PG 组：显式设置隔离 `TEST_PG_DATABASE_URL` 和临时 `DATABASE_URL` 后运行 `venv/bin/python -m pytest -q -m pg tests/pg/test_pg_fx_short_concurrency.py`。没有 PG 库时报告未验证，不能把跳过当通过；关闭仅本任务拥有的临时 PG 服务。

前端在 `thccb-frontend` 执行 `npm run test:unit`、`npm run type-check`、`npx eslint src/pages/admin/FxManage.vue`、`npm run build`。不运行会自动修复全站文件的 `npm run lint`。若这些命令已在最后一次相关代码变化之后通过，Task 4 只引用证据，不重复运行。

## 待确认的后续候选

### 交易锁内依赖输入复用

见设计“后续交易候选”。须先批准一致性方案，确认合作写者的 User 锁/经济版本与品种门闩覆盖；之后另列实现步骤和针对真实失效风险的并发验证。第一阶段不删除锁内完整发现，不创建新 locked 借款 helper。

### FX 本地预览（待办，2026-10-12）

- [ ] 输入时改用本地预览，确认交易前获取一次完整后端报价，提交时保留锁内权威检查。

用户要求将提案记入待办，之后再实施。本项尚未实施；下述方向用于后续设计，不代表接口范围和确认流程已经批准。

**目标与流程：** 减少输入期间反复请求及有债增仓的全仓风控计算。加载市场／账户快照后，输入变化只做本地预估；确认前取得完整后端报价，再按确认结果提交。完整报价和成交各自保留必要检查。正式提交仍携带账户版本、滑点限制、增仓许可和幂等编号。

**所需输入：** 市场快照包含金币／外币储备、买卖费率、状态、池版本及快照时点；账户快照包含现金／可用现金／总锁金、本币种多头数量／含息欠币／本仓锁金、含息金债、账户版本与快照时点。现有账户接口已提供大部分信息；普通行情尚无完整储备／费率／统一时点，统一报价腿内的储备不能替代明确的预览快照。确认参数的可见范围和更新方式后再调整接口。

**预览范围：** 预计成交量、手续费、成交后价格、先平仓再反向开仓的数量、最终方向及现金／还款变化。跨零第二腿使用第一腿执行后的模拟储备。仅用边际价格乘数量不能代表数量滑点；本地余额提示也不能保证成交获准。不将其他玩家持仓、金库库存或整套 LMSR 抵押资产搬到前端做准入。

**误差与失效边界：**

- 使用与后端一致的六位金额和逐腿舍入规则：手续费向上、产出向下；优先复用现有 BigInt 金额工具，不直接用普通浮点数生成正式金额。
- 第一版沿用服务器提供的含息欠款快照，不在前端新增复利引擎。实际提交期间利息增长仍可能改变回补／转多分界，最终报价与成交重新计算。
- 市场或账户快照变化、断线／重连、账号／币种切换和过期时，更新或废弃旧预览；不拼接不同池版本的数据，时效规则留待实施前确定。
- 缺失／未知数据不当作0，不显示为确定可成交；行情、账户及利息变化的差异用最终确认和滑点保护处理。
- 全仓风险、实际资金／借币库存、原子成交、只平仓／允许增仓、版本检查及未知成交恢复继续由后端负责。减掉输入期风险检查不改变定时强平机制。

**实施前待确定：** 预览快照通过何种接口提供、储备／费率可见范围、更新信道与时效，以及最终报价变化后的确认交互。届时另列实现任务；验证重点为普通买卖、跨零、完全／部分回补的金额一致性、旧快照失效和输入期请求减少，不重复搭建整套风险算法测试。

### FX 内存 writer（待办，2026-10-12）

- [ ] 测量成交中的储备读取和锁等待成本，再单独设计 per-pair 内存经济 writer，统一交易、强平、流动性与管理员池配置写入口。
- [ ] 确认跨品种风控版本、SQL 提交后内存失败恢复和单写所有权；用户资金、仓位、回执与审计继续每笔提交 SQL。

本项只登记；用户已将 LMSR 行情阶段 1/2 也延期待办，当前不启动这些实现。具体待办和实施前决策见 [LMSR 行情计划的阶段 3](2026-10-12-lmsr-market-data.md#阶段-3-待办fx-内存-writer本轮不实施)，批准独立架构方案后再写 FX 实施计划。

## 计划自检

已覆盖组合读取缺行/用户隔离、金库范围与其他品种义务、请求重放、上限停用及风险边界。各 helper 只服务当前模块，HTTP/schema 不变。现有测试与临时采样各负责行为和成本；不新增完整重复算法或永久性能门槛。Tasks 0–4 已完成，阶段及最终 medium 审阅 PASS、整体 Ready；隔离工作树交付，root 已关闭本任务临时 PG，未 commit/push/deploy。实测结果和阶段限制见 [验证记录](../../fx-read-cost-validation-2026-10-12.md)。PG EXPLAIN 与生产耗时未验证，异常计划停止扩展的条件仍保留。
