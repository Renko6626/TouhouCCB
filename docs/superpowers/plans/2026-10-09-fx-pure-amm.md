# FX 央行与新闻清理、数据库重建 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for inline execution, or superpowers:subagent-driven-development if the user selects delegation. Steps use checkbox (`- [x]`) syntax for tracking. 代码实施与隔离验证已完成，正式生产切换未执行。验证证据见 [实施记录](../../fx-pure-amm-validation-2026-10-09.md)。

**Goal:** 删除央行及新闻模块，采用精简 schema 重建业务库，迁移用户身份／权限／个人资料并重置游戏经济状态。

**Architecture:** 基于最新 origin/main；保留 AMM、借贷和全仓强平内核及已优化行情。注撤资移到 liquidity 模块，删除事件引擎和新闻调度；追加 schema 清理 revision，在独立空库初始化并按用户白名单导入，验证后切换。

**Tech Stack:** 现有 FastAPI、SQLModel／SQLAlchemy async、PostgreSQL、SQLite、Vue 3、TypeScript；不增加依赖。

**Spec:** [修订后的设计和用户迁移规则](../specs/2026-10-09-fx-pure-amm-design.md)。用户已确认经济状态按新开局重置，不结算旧头寸。

## Global Constraints

- 起点为最新 origin/main；本次已检查 1af6678。保护当前分支和两份既有 2026-10-01 未跟踪文档，将本次设计／计划带入实施分支。
- 保留借贷和强平算法、费用、精度、GATES、OWNERSHIP、幂等性、提交后通知规则。保留 FxTreasury 的借币和手续费职责。
- 新闻全停，不建立文字新闻服务，不保留旧事件运行兼容层；废弃表／列从新 schema 物理移除。
- 源库只读导出，目标必须为空且不同于源库；旧库不能在用户数据导出和恢复验证之前删除。
- 用户身份与经济字段分开迁移，余额为明确的 B，债务和持仓清零。称号／已购权益按保留清单连同必要依赖处理。
- 不重写既有 Alembic 历史；不在源库调用 init_db、重置脚本或删除 migration。新库切换必须绕开普通部署对源库自动 upgrade 的路径。
- 新增测试只覆盖真实风险，删除测试不另写删除证明。UI 最多截图；不运行自动交互或全仓 lint --fix。
- 实施准备和正式生产切换分开交付；本轮请求仅为修改计划。

## Review Focus

1. SSO 绑定、停用状态和管理员权限迁移后完整且不提权：任务 5。
2. User 内嵌债务／冻结／强平状态及外部欠币不能随资料复制到新开局：任务 5。
3. 称号佩戴、已购兑换权益及操作者引用不能悬空，也不能恢复重复领取资格：任务 5。
4. 新闻表删除后首页、SSE、启动、市场归档和赛季重置不能继续查该表：任务 2—4。
5. 新库 ID／幂等键复用不得串入旧行情缓存或旧待确认订单：任务 5—6。

## Task 1：main 基线与资金模块分离

**Files:** 新建 `backend/app/services/fx/liquidity.py`；修改 `backend/app/services/fx/scheduler.py`、`backend/app/api/v1/admin_fx.py` 和原有注撤资测试调用方。

**Interfaces:** 迁移 `fund_pair(db, pair_id, gold_amount, foreign_amount, operator_user_id)`、`withdraw_pair(...)` 和私有辅助函数，API 路径与返回 FxPairAdmin 保持不变。

- [x] 刷新远端并记录 SHA，在独立实施分支工作；核对已有 FX 增量行情代码完整。
- [x] 原样迁移注撤资，更新调用方；保持发行规则、锁序、最新库存读取、版本推进和审计。
- [x] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_credit_wp6b_fx_ops.py tests/test_admin_fx_multi_pair.py tests/test_fx_short_trading.py`，记录基线与改动引起的差异。
- [x] 提交资金模块分离。

## Task 2：删除央行及新闻运行路径

**Files:** 删除 `backend/app/services/fx/engine.py`、`randomness.py`、搬空的 `scheduler.py`；修改 `backend/app/main.py`、`backend/app/api/v1/admin_fx.py`、`backend/app/api/v1/fx.py`、`backend/app/api/v1/fx_stream.py`、`backend/app/schemas/fx.py`、`backend/app/services/fx/market_data.py`；适配 `backend/scripts/season_reset.py` 和受影响测试夹具。

**Interfaces:** 移除 /admin/fx/events 全部路由、/fx/pairs/{pair_id}/news、新闻 schema 和 SSE news；保留行情 SSE 和所有资金 API。

- [x] 删除引擎、新闻调度生命周期和新闻查询／发布入口；旧路由返回正常 404。不新建替代新闻服务或兼容事件状态。
- [x] 清除 SSE 首包与 frame 中的新闻读取／字段，保留行情、游标、history tail 和成交增量。
- [x] 移除市场删除／归档中的事件存在性检查及事件清理操作，保留真实持仓／欠币／成交历史约束。
- [x] 更新 main、season_reset、共享夹具的模型引用和启动接线；保留行情 consumer/flusher/publisher、贷款与强平调度。
- [x] 删除 test_fx_engine.py、test_fx_events.py、test_fx_scheduler.py 中仅服务废弃功能的测试；其余文件只移除新闻／干预断言，保留真实交易和行情覆盖。混合用例用已有真实用户成交取代引擎模拟价格变化。
- [x] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_fx_api.py tests/test_admin_fx.py tests/test_admin_fx_multi_pair.py tests/test_fx_stream.py tests/test_fx_publication_e2e.py tests/test_fx_season_reset.py`；不为证明文件删除新增测试。
- [x] 提交运行路径清理。

## Task 3：精简 schema、配置及接口

**Files:** 修改 `backend/app/models/fx.py`、`backend/app/schemas/fx.py`、`backend/app/api/v1/admin_fx.py`、`backend/app/services/site_config.py`、`backend/app/services/loan_migrate.py`、资金服务中 daily_spend/spend_date 快照引用；新增 `backend/alembic/versions/2026_10_09_1200-fx_remove_intervention.py`。调整 `backend/tests/test_fx_migration.py` 及现有相关模型／审计测试。

**Interfaces:** PairCreate/PairPatch 去掉 target 字段并拒绝额外参数；FxPairAdmin/Detail 去掉目标和干预预算字段；新 schema 无 fx_event、target 三列、treasury 两个日预算字段。保留 initial_price 开盘元数据。

- [x] 从模型、API、默认配置播种和有效配置规则移除设计列出的表／列及七项配置；不留隐藏目标价默认值。
- [x] 清理仍运行的 shorts/trading/liquidity/audit 快照对已删预算列的读取；只改无经济意义的元数据，不改金额和债务逻辑。
- [x] 追加 revision：按实际 Alembic head 挂接，先移除依赖约束／索引再删列／表，删除七个废弃配置行。支持 PostgreSQL 与 SQLite batch 操作。禁止把“某 revision 永远是 head”写成断言。
- [x] downgrade 恢复旧结构和必要默认配置，仅承诺结构恢复，不能宣称恢复已删除新闻／旧参数值；新库重建及恢复流程使用备份。
- [x] 在现有迁移用例中验证实际最终 schema、升级时非目标用户行与借币余额保持、回滚结构可用。仓库 baseline 为 stamp-only：空目标使用当前 metadata 建表后 stamp 实际 head；迁移测试另外从真实前一版结构升级，验证两条路径的最终 schema 一致。
- [x] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_fx_migration.py tests/test_fx_models.py tests/test_admin_fx.py tests/test_credit_config.py tests/test_fx_short_trading.py tests/test_fx_audit_replay.py`。
- [x] 提交 schema/API 清理；revision 不在真实源库执行。

## Task 4：清理玩家和管理端界面

**Files:** 修改 `thccb-frontend/src/pages/admin/FxManage.vue`、`src/pages/Fx.vue`、按引用涉及的 `src/pages/home/FxHome.vue`、`src/api/fx.ts`、`src/types/fx.ts`、`src/utils/configMeta.ts`、`src/utils/fxPairSetup.ts` 及现有相关单测。

**Interfaces:** `buildFxPairSetup({price, gold, buyPct, sellPct})` 保留储备／开盘元数据／费率结果，删除涨跌范围和 target 输出；前端不再请求新闻 API 或解析新闻状态。

- [x] 删除新闻管理表单、新闻列表和历史、新闻轮询／切币加载／SSE 合并逻辑以及专属样式，页面不留下“未加载新闻”或持续报错占位。
- [x] 删除目标价、涨跌区间、噪声、预算、半衰期配置和主动干预历史面板；资金、强平成交和借币库存信息保留。
- [x] 调整开户参数辅助函数与现有测试，保留 BigInt 精度和储备范围验证；展示实际初始储备比值。
- [x] 运行 `cd thccb-frontend && npm run test:unit && npm run type-check && npm run build`；必要时仅截图核对。
- [x] 提交 UI 清理。

## Task 5：实现用户白名单迁移和新开局工具

**Files:** 新增 `backend/scripts/rebuild_user_database.py`、`backend/tests/test_rebuild_user_database.py`；复用 `backend/app/models/{base,title,redemption,audit}.py` 的真实模型与现有配置／审计服务；参考 season_reset 的权益依赖识别，不执行其原库清理逻辑。

**Interfaces:** CLI 提供 inspect/export/import/verify 阶段；源／目标连接由各自环境变量传入，policy 文件声明账号范围、金额 B、保留字段／表和依赖。输出计数及校验结果，不输出密码、完整邮箱、兑换码等敏感内容。默认只读预检；导入必须显式选择空目标库。

- [x] inspect 统计真人账号、权限、现金／债务／持仓分布、称号和已购权益依赖；生成可审阅保留清单，列出余额 B 的具体来源和数值。未识别的用户权益／外键依赖阻止 import，不能悄悄丢弃。
- [x] export 按已确认字段白名单导出身份／权限／资料及必要权益依赖。采用一致快照，源库不写；文件设为仅操作者可读，不进入 git。
- [x] import 拒绝源目标相同、非空目标和重复导入；在已验证为空的目标库注册当前模型、create_all、stamp 实际 Alembic head 并播种必要配置，不调用破坏性的 init_db，不生成示例市场。按外键顺序事务导入，保留用户 ID 和 SSO；修正相关序列。
- [x] 设置 cash=B、debt=0，清空旧计息／强平／经济冻结状态，不导入旧 LMSR/FX 持仓、锁金、债务、强平任务或交易。写入符合现有回放语义的新开局锚点；保留账户禁用状态和管理员权限。
- [x] 新版本默认配置按白名单覆盖必要站点参数；所有经济写入 gate 在切换前保持关闭。核对 deploy 的环境门闸，不能只重置 fx_enabled。
- [x] verify 比对身份映射、账号数量、权限、保留资料／权益、余额总量=N×B、零债务及空持仓、外键完整性、序列；失败不进行切换。若保留少量依赖账号，余额总量分别按政策统计，不混入真人重置总量。
- [x] 新增一组小型双库集成测试，使用真实模型覆盖身份权限保留、债务归零／无旧仓位、称号或兑换关联、非空目标拒绝和事务失败回滚；避免复制迁移逻辑计算期望。
- [x] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_rebuild_user_database.py`；在隔离 PostgreSQL 做一次同样的导出／导入／校验演练，记录实际命令与结果，覆盖序列行为。
- [x] 提交迁移工具及证据，不连接或删除生产数据库。

## Task 6：回归与正式重建操作单

**Files:** 更新 `docs/fx.md`、`docs/api.md`、`docs/deploy.md`、`docs/README.md`；新增 `docs/fx-database-rebuild.md`；核对 `deploy/deploy.sh` 的数据库选择及自动 migration 路径。必要的缓存／旧请求失效调整限定于现有 history 和待确认订单接口，不借机改造整个部署系统。

- [x] 回归 AMM、买卖、借款／空头／强平、注撤资、行情派生及赛季重置；不重复已通过且未再改的前端检查。

```bash
# backend/；使用独立测试数据库
venv/bin/python -m pytest -q tests/test_fx_amm.py tests/test_fx_trading_service.py tests/test_fx_trading_in_session.py tests/test_fx_short_trading.py tests/test_fx_short_api.py tests/test_fx_short_liquidation.py tests/test_fx_liquidation_service.py tests/test_credit_sweep.py tests/test_fx_short_audit_replay.py tests/test_fx_season_reset.py
venv/bin/python -m pytest -q tests/test_fx_candle_pipeline.py tests/test_fx_market_data_integration.py tests/test_fx_history.py tests/test_fx_stream.py
venv/bin/python -m pytest -q --noconftest tests/test_fx_end_to_end.py
venv/bin/python -m pytest -q -m pg tests/pg/test_pg_credit_liquidation.py tests/pg/test_pg_fx_short_concurrency.py
# 仓库根目录
git diff --check
```

- [x] 移除旧引擎并发参与者但保留真实用户交易／强平并发断言；调整新闻专用夹具和断言，不删除有效经济行为覆盖。
- [x] 操作单记录停写、备份恢复验证、导出、新库建表／导入、核对、切换、旧库最终删除的顺序。明确“旧数据不会迁入新业务库”，备份保留用于迁移失败恢复。
- [x] 针对新开局清理／隔离 FX 与 LMSR 历史缓存，保留源库市场序列高水位或采用验证过的缓存失效方案。清除客户端旧待确认订单；服务端确保旧 pair/outcome ID 不会指向新市场，避免旧请求错买新市场。
- [x] 对未迁移的旧 /interventions 管理面板路径和新闻路由完成 API 文档清理；旧市场／账务不再兼容新开局，旧计划与活动参数标记失效。
- [x] 写清回退边界：新库尚未接受用户交易前可切回已备份旧环境；接受新交易后不能直接切回丢掉新账。清理 revision 的 downgrade 不恢复数据。
- [x] 交付 diff、保留清单示例、隔离演练结果和未验证项。正式生产操作需明确目标环境及具体清单；本计划修改不执行生产切换或删库。

## 验收终点

新版本无央行／新闻运行模块，无对应表列和配置；用户可用原身份登录，权限与选定资料保持，现金按 B 重置，旧债务和持仓为零；新市场可重新注资使用 AMM、多空借贷和原强平内核。后续统一交易 UI、挂单／止损和强平性能优化另立计划。
