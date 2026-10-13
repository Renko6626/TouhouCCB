# Business Reset and New Database Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development task-by-task. Steps use checkbox syntax.

**Goal:** 完成保留账号与称号的业务重置，建立完整新 Alembic 基线，退出旧迁移/转换路径并归档其工具。
**Architecture:** 保持当前 PostgreSQL、SQLAlchemy/SQLModel、Alembic、Compose 与 Actions。先准备可独立验证的当前版本逻辑和冻结 schema；新维护入口在停写条件下导出白名单、建空目标、核对后切换，普通部署仅接受新基线及之后版本。
**Tech Stack:** 现有 Python、SQLModel、SQLAlchemy、Alembic、asyncpg、SQLite 测试、PostgreSQL、Bash、GitHub Actions；不新增依赖。
**Spec:** docs/superpowers/specs/2026-10-13-business-reset-baseline-design.md（用户已确认）

## Global Constraints

- 保留账号身份、SSO 绑定、账号状态、管理员权限，以及完整称号子系统和佩戴关系；称号引用的机器人仅保留停用、零资产身份，不恢复交易功能。
- 真人现金=源库当前 siteconfig.initial_balance；debt=0，经济版本旧值+1；清空全部兑换、交易、资产、市场、行情、旧资金流水/审计和机器人业务数据。
- 保留有效当前配置、仅补缺省项；经济五项开关关闭，credit_new_risk_frozen=true；不修改环境中的 SSO/JWT 密钥、不自动开市。
- 新 baseline 必须冻结完整 DDL，空库实际 upgrade；不得以 runtime create_all+stamp 代替。归档的旧历史不在 Alembic/pytest/运行时加载路径。
- backfill_fx_candles.py、来源转换辅助和自动准备入口均归档；不另造事件版修复 CLI，在线事件聚合和当前交易 API 保留。
- 真实交易、审计、池事件同事务持久化、锁顺序、幂等与金额精度、公开白名单及现有行情有界消费不改变。
- 旧物理库在新库验收完成后删除；保留可恢复 dump、旧不可变镜像/代码/配置/前端；新账产生后不得自动回退丢账。
- 所有 source/target 必须明确且不同，target 完全空；不得读取/输出生产 .env、连接凭据、身份清单或兑换码。
- 一次一个 Sol low 实施者，Sol medium 任务审阅/最终审阅；子 agent 仅在 worktree 内实施，不推送、合并、操作生产或再委派。
- UI 默认最多截图，不自动点击/填表；不加全站性能矩阵；永久测试只覆盖新关键行为或真实缺口，保留有效断言。
- 原用户 main 工作区未提交的文档/output 保持不动。生产动作由主 agent 在验证与审阅通过后执行，不重复请求已经给出的授权；出现新的架构决策点或意外数据差异再报告。

## Review Focus

- 尚未退休的旧配置/游标/脚本引用：迁移后不再执行旧补丁，现有参数、行情和接口行为仍正确（Task1/4）。
- 完整空库 schema 与 User/Title 循环外键：PG/SQLite 均可建立且模型可用，旧 revision 明确拒绝而不自动 stamp（Task2）。
- 非 bot 的历史无 SSO 账号、停用管理员及称号依赖机器人：原身份/称号完整，机器人不恢复交易（Task3）。
- 非法金额/账户版本溢出、未识别保留字段、非空目标、旧 ID 水位：在导入前拒绝，不制造混合新旧状态（Task3）。
- 生产只读验收也可能收到登录回调写入：验收阶段必须真正阻止目标库写入；失败保持可恢复阶段与停写（Task4）。

## Task 1: Current configuration and event-only FX runtime

**Files:** 新 app/services/config_seed.py；app/main.py、app/services/site_config.py、app/models/fx.py、app/services/fx/{candles,market_state,market_reads}.py 的明确 legacy cursor 引用；相关当前配置/启动/FX 测试。归档 app/services/loan_migrate.py、scripts/{migrate_loan_v1,backfill_fx_candles,fx_history_sources,prepare_fx_event_history}.py，以及纯旧转换/旧配置映射用例；从 deploy/deploy.sh 移除 prepare 调用。
**Interfaces:** config_seed.DEFAULT_CONFIGS 保持现有配置值/类型集合；async seed_current_config() -> list[str]（新播种的键）；async seed_credit_risk_configs() -> list[str]（仅当前参数，无旧映射）；resolve_credit_config(raw: dict[str,str]) -> dict[str,str]（仅当前参数，空配置使用现有 constants，既有非法/不完整风险参数拒绝）。后续 reset 工具消费当前默认集合与参数校验。

- [ ] 把现行播种/验证从 loan_migrate 移出，删除 ALTER、旧借贷时间修补、旧 leverage/threshold 映射；保留默认值、有效参数和管理员开关、不覆写当前配置。更新当前代码/测试 imports，不留旧名称 alias；旧专项映射用例归档，其余风险校验/热冻结用例保留。
- [ ] 删除 FxMarketDataState.last_trade_id 和 runtime legacy_trade_id/旧 cursor 判定；保持 event cursor、source/link integrity、readiness、generation、bounded ring/flush。保留 FxTrade ID/receipt/n_trades 和有真实用途的 candle 来源字段/玩家兼容接口。
- [ ] 当前历史/集成测试若依赖旧离线脚本，只迁移现代 event 路径夹具到真实聚合/存储逻辑；纯旧导入/排序用例归档。新常驻修复工具禁止增加。
- [ ] 在 docs/archive/database-pre-baseline-2026-10-13/ 保留上述原文件和退役原因；原文件使用变更前版本，旧版本恢复仍指向完整旧 Git SHA。
- [ ] 运行相关配置、启动 gating、FX history/candle/stream/integration 与金融现有检查；仅对本 task 的真实覆盖缺口补短用例。报告尚待 Task2/4 替换的旧 baseline/旧 workflow 耦合，不宣称中间分支可部署。commit。

## Task 2: Frozen full baseline and schema-owned startup

**Files:** backend/alembic/versions/2026_10_13_business_baseline.py；app/core/schema.py；app/core/database.py、app/main.py、backend/init_db.py、backend/alembic/env.py；deploy/deploy.sh schema/旧 rollout 检查部分；迁移/启动测试。原25revision与纯历史迁移测试归档到 active 搜索路径外。
**Interfaces:** BASELINE_REVISION='business_baseline_20261013'；async require_schema_current(engine) -> None 用于全部实例启动；classify_schema(connection) -> str 输出 empty/upgradeable，旧/未知/无版本部分库拒绝，用于普通部署只读预检。Alembic env 支持 config.attributes['connection'] 的标准实际 connection 注入，供 Task3 空目标 upgrade，不忽略 settings 正常 CLI 行为。

- [ ] 根据 Task1 最终模型生成并冻结完整 DDL，处理 User/Title 循环 FK；不能 import 当前 models 来 create_all。固定 schema 创建全部必要索引/约束；SQLite 整数 ID 表的水位机制可供重置恢复，PG 正常 sequence。
- [ ] 新 baseline 为空库真正建表，空库 downgrade 往返可用；populated baseline downgrade 在首次 DDL 前拒绝整库丢数据。旧25revision归档，只有一个 active baseline head；不改旧归档内容来迎合新模型。
- [ ] 所有实例启动先只读校验当前 schema revision；owner 才配置播种/调度，移除应用 create_all/DDL。移除 core 无其他调用的 init_db。init_db.py 改为明确空库 bootstrap（Alembic upgrade），非空库拒绝，不再通用 drop-all+stamp；开发示例初始化保留范围仅在原有需要时沿既有方式，不创建新功能。
- [ ] 普通 deploy 在停写前识别 empty/可升级新基线库，拒绝旧/未知/部分 schema；空库/已存在库同走 upgrade，移除旧首次开空/FX schema Guard 调用，保留备份/审计/启动健康。
- [ ] 复用现有迁移测试位置验证实际空 PG/SQLite schema、模型读写、外键/索引/约束，已安装当前版检查与旧版拒绝，空 downgrade 和 populated 拒绝。纯历史迁移测试归档，不能减少仍有效的当前数据完整性覆盖。相关通过后 commit。

## Task 3: Business reset core and explicit preservation

**Files:** 新 backend/scripts/business_reset.py；小规模真实数据库测试 tests/test_business_reset.py（必要时复用 existing rebuild 夹具）；现有 seeding/模型消费，不修改 schema 和金融服务。
**Interfaces:** CLI stage=config|preview|export|import|verify，参数 --run-id（1..20数字）、--directory（私有目录）、--expected-initial-balance（仅比较，不覆盖源配置）、--after-cutover（仅 verify）。CLI 固定 Compose source postgres:5432/thccb，用 run_id 派生 thccb_reset_RUNID/thccb_before_RUNID；测试核心函数接受显式 source/target URL。export_database(source_url, expected_initial_balance=None)->manifest；import_database(source_url,target_url,manifest)->summary；verify_database(target_url,manifest)->summary。可使用 async 实现；报告最终 exact signatures 交 Task4。

- [ ] source 仅反射保留字段：全部 is_bot=false 用户和完整四张称号表；闭包保留引用机器人身份并停用、0现金/债务/佩戴为空，无 bot 业务。真人保留原角色/状态/SSO/同意时间，资金及版本按 spec；新审计锚点只代表本次新开局。
- [ ] 当前声明的有效配置集合保留并校验，未声明/废弃键不复制；缺省来自 Task1；记录真实 initial_balance，关闭五经济门/冻结新风险，保留 .env 之外的有效运营选择。
- [ ] manifest 格式 business-reset-v1、固定政策、保留行、完整旧整数ID水位与计数；源快照只读一致，输出仅摘要；unknown 保留字段/FK、金额/版本溢出、initial_balance 变化、源目标相同或非空目标拒绝。私有文件权限0600/目录0700，拒绝覆盖现有重置导出。
- [ ] target 使用 Task2 实际 baseline（标准 Alembic connection 注入）而非 create_all/stamp；导入循环 FK 先暂存用户佩戴NULL，导入称号，再还原。在新增自动分配审计等 ID 前设旧序列水位，不只导入后调水位；保留的原ID不改变。
- [ ] verify 比较全部保留字段/称号关联、B/0债务/版本+1、依赖停用、当前配置/关闭门、业务表全空、新审计与序列/新 schema。不得把旧业务/兑换审计复制进目标。
- [ ] 短真实行为用例覆盖无SSO/停用admin/机器人持有或授予称号、兑换全部清空、配置保留现金B、旧版本单调/ID不复用、非空/同库/非法数据拒绝及中断事务回滚。SQLite核心与一次隔离PG全流程；不搭建新的独立验证框架、不替代真实PG证据。commit并报告接口。

## Task 4: Production reset route, remaining archive and README

**Files:** .github/workflows/business-reset.yml、deploy/business_reset.sh；.github/workflows/ci.yml、deploy/deploy.sh；scripts/README.md；docs/{deploy,migrations,architecture,fx}.md 与新的 business reset runbook；旧 fx-rebuild.yml、deploy/fx_rebuild.sh、check_fx_rebuild_deploy/check_fx_short_rollout/rebuild_user_database/production_fx_rebuild 和对应纯历史测试归档。
**Interfaces:** 消费 Task3 exact CLI。新 workflow 接 action preview|execute、build_run_id、expected_initial_balance、confirmation RESET；execute 需要 DATABASE_RESET_PENDING=true，成功 current main push CI不可变镜像/前端，固定owner与thccb-production并发锁。普通 CI Deploy 仅在 DATABASE_RESET_PENDING != true 时执行；旧变量不留常驻兼容。

- [ ] 复用现有 runner 的固定PG、private backup/stage路径、停止writer和source无其他连接检查、完整dump+实际恢复验证、保留旧镜像/配置/前端、target创建/核对/rename/拒绝覆写，不新增通用任务架构。新CLI政策由源配置决定，不允许 operator 任意覆盖现金。
- [ ] 只读验收阶段必须真正防止目标库写入（现有 THCCB_READ_ONLY_INSTANCE 不阻止所有认证写入）；在该目标库为应用角色设置临时 default_transaction_read_only=on，记录/恢复原值，保持其他数据库/角色不变。owner 启动前恢复，并重建连接。只读审计/verify/健康在保护下完成。
- [ ] 失败前未切换可恢复旧服务；首次rename后失败保持backend停写、保留两个库和阶段记录；owner 已启动不自动回滚。所有核对通过后再删除旧物理库，dump/镜像/配置保留，业务门关闭。成功/失败摘要不可泄露secret/用户/兑换码。
- [ ] 归档剩余历史链及其专用tests，现行 season_reset 继续保护未来兑换权益；seed imports改当前模块，README只列实际保留工具与新真实重置入口，给归档索引，不再列待归档为可执行任务。
- [ ] 用现有 season runner 测试风格覆盖真实 shell 的安全阶段/失败后可观察状态，真实金融导入由Task3PG证据负责；不重复造大型fake层、不仅断言源码/内部命令顺序。bash -n与相关workflow验证，文档/样式无永久新测试。commit。

## Task 5: Integration verification and review evidence

**Files:** 必要的当前测试/夹具兼容修正；docs/business-reset-baseline-validation-2026-10-13.md。代码问题交所属实现者/单一fix wave，不扩大功能。

- [ ] 检查 active Alembic/运行时代码/部署/workflow 没有旧路径引用；README链接和归档来源可追踪。不把支持中的 API/type alias 因legacy命名删除。
- [ ] 运行既有 CI必要 compile/import、FX --noconftest HTTP检查及后端fullsuite；前端无变更复用实际证据，必要CI会build，不加浏览器交互。目标PG空baseline+完整重置/验证结果与未验证Docker/生产步骤如实记录。
- [ ] 新失败只调查具体层：保留有效断言、更新当前fixture，不为归档删断言或新增测试证明删除。相关通过后停止扩展验证。
- [ ] 完成全部Task medium审阅，再整分支medium审阅；记录实际命令/版本/结果/限制。commit。

## Task 6: Approved production execution (controller only)

- [ ] 保存旧生产04c9e7a代码/镜像恢复点；开PR，CI通过前不合并。合并新baseline前设旧FX_REBUILD_PENDING和新DATABASE_RESET_PENDING暂停过渡期CD；用户已授权新基线/业务重置，不反复问已确认范围。
- [ ] 合并后新main构建成功且Deploy按暂停条件跳过；new workflow preview核对source/B/保留计数/删除范围，记录输出；unexpected政策/源状态须先解决，不猜。
- [ ] execute使用预览实际B作为expected值，固定成功main build与RESET；跟进完整backuprestore/export/targetbaseline/import/verify/rename/readonly/owner/olddelete健康结果。不自动开市。
- [ ] 验证新schema、保留账号称号、全清交易/兑换、现金B/债务0/经济版本+1、ID水位、闭门/审计/健康；清理配置的旧行情缓存与重启ring；备份/旧镜像保存。失败遵循明确恢复阶段，不冒称成功。
- [ ] 成功后移除两个暂停变量，记录PR/mergeSHA/workflow/backup路径/新政策计数；保护用户原工作区未提交文件。仅清理本计划SDD scratch并保存必要证据。
