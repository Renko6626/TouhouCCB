# 业务重置、新数据库基线与历史工具归档

状态：设计待用户确认；不得在确认前执行生产重置或实现新的架构。
现有生产代码：04c9e7ad2081337677cd4a1e82515c4a9849f76e。独立工作分支：feat/business-reset-baseline。

## 用户已经确定的需求

- 开始一次新的经济活动，清空全部交易资产、债务、市场、成交、行情与旧交易审计。
- 保留账号身份、SSO 绑定、账号状态、管理员权限，以及已有称号与佩戴关系。
- 清空全部兑换数据，包括合作方、库存、兑换码、购买、核销和弹幕兑换记录。
- 真人现金沿用赛季重置口径：统一设为源库当前 siteconfig.initial_balance；不猜金额，不使用旧换库工具的默认 0。
- 创建新的 Alembic baseline，归档历史迁移和历史过渡脚本。
- backfill_fx_candles.py 也归档，不另造事件版手工重建 CLI；在线事件聚合、物化 K 线与实时通知仍保留。
- backend/scripts/README.md 列清保留工具的用途、入口、数据库影响和调用条件。

## 推荐架构与取舍

继续使用 PostgreSQL、SQLAlchemy/SQLModel、Alembic、现有 Compose 和 GitHub Actions，不更换技术栈，不建立新的迁移任务框架。

新 baseline 是冻结的完整 schema DDL：空数据库可以直接 alembic upgrade head；后续正常增加增量 revision。revision 不调用运行时模型的 create_all。现有 25 个 revision 移到 active versions 目录之外，旧版本保留为不可变 Git tag/commit 与归档资料。新主线不支持直接升级旧 revision 的数据库；旧备份先用旧镜像/代码恢复。

应用启动不再 create_all、ALTER TABLE、修补旧字段或映射旧配置。启动验证已安装 schema revision 与当前代码相容；配置播种独立为当前功能，只补缺省配置，不覆写运营选择。已有历史 schema 兼容退出正常部署和启动路径。

这里选择新完整 baseline 的理由：统一空库与已有库入口，避免现有 stamp-only 起点的特殊初始化流程。代价：旧环境必须完成这次切换，旧备份不能直接由新代码自动升级。

## 数据保留与重置

保留真人账号定义为 is_bot=false，不以 casdoor_id 是否为空判断，避免误删历史账号。保留字段：id、casdoor_id、username、email、is_active、is_superuser、is_bot、tos_accepted_at、equipped_title_id。

真人现金=固定导出时的 initial_balance，debt=0，debt_last_accrued_at/last_liquidated_at=NULL，credit_frozen=false；economic_version 使用旧值+1，保持前端账户更新单调，拒绝旧版本交易请求。不得将经济版本降回 0。

推荐保留完整称号子系统 title、user_title、title_code_batch、title_code，既有授予、佩戴和称号码状态均保留；market_required_title 随旧市场清空。这是实施方为保持“账号和称号”完整提出的保留范围，须随本设计确认。

被保留称号记录引用的 bot/system 操作者仅作为依赖身份保留原 ID，设为停用、现金/债务 0，不恢复 bot_profile/bot_suspicion。不清空授予者字段来绕过外键。

推荐保留现有有效的当前配置键（当前声明的配置集合），补缺省项，去除已废弃键；initial_balance 原值保留。环境文件及 Casdoor/JWT 密钥不更换。重建后 fx_enabled、fx_short_enabled、loan_enabled、liquidation_enabled、pve_enabled 关闭，credit_new_risk_frozen=true，待管理员准备新市场后主动开启。这是推荐的暂停运营政策，须随设计确认。

所有市场/outcome/position/transaction/candles、FX pair/pool/wallet/execution/trade/short/candles/state/events、旧 ledger/audit/liquidation 与机器人业务记录清空。兑换五张业务表清空。新库仅新增必要的用户开户余额审计和当前配置初始化审计，作为新一轮审计起点。

保留原整数 ID 的 sequence 高水位，不复用旧 market/outcome/fx_pair/execution 等 ID；称号与用户 ID 不变。重启缓存与行情 ring，清理服务端旧行情缓存。旧客户端未完成订单应因旧对象不存在或账户版本不匹配而拒绝；不新增业务代际字段或新接口。

## 生产重建流程

生产目标沿用现有固定 Compose PostgreSQL postgres:5432/thccb，由同一部署账户操作。先在隔离库验证全部流程，准备新的业务重置维护入口，复用已有停写、备份恢复验证、切库与健康检查骨架，不原样复用旧权益保留政策。

1. 在合并含新 baseline 的代码前暂停普通 CD；新代码只构建镜像/前端，不能自动升级旧生产库。
2. 对当前生产源做只读预览，输出真人/依赖账号/称号数量、initial_balance、源 revision 与旧业务记录计数，不打印身份、兑换码或连接凭据。
3. 停止全部经济写入者；固定源库、代码 SHA 和保留政策。使用一致导出，重新核对 initial_balance；现有备份保留，并保存停止写入后的最终快照及旧不可变镜像/部署配置/前端。
4. 在独立空目标运行新 baseline，导入身份/称号/配置/新审计锚点，核对全部保留字段、外键、现金/债务、经济版本、空业务表及 sequence 水位。禁止通过 create_all+stamp 冒充升级。
5. 全部核对成功后交换固定数据库名，先只读启动新应用，验证 schema、审计与健康；再启动 owner，经济功能开关仍关闭。不自动开市。
6. 新库验收完成后删除旧物理数据库，完整 dump 和旧代码仍保留。此步骤纳入明确的业务重置，不将普通部署变成清库入口。
7. 解除普通 CD 暂停。记录源/目标库名、构建 SHA、实际政策/计数、备份与各项核对结果。

任何未知保留字段、非法金额、外键缺失、来源或目标混同、非空目标、额外写入者、数据核对或健康失败均中止。切换后失败保持停写，依据备份和准确阶段恢复；新 owner 已产生新账时不得自动切回旧库。

源导出只依赖固定的保留字段和数据库反射，不导入旧 Alembic 历史或旧业务模型。新目标初始化由新 baseline 负责。新的 business_reset 工具属于真实维护功能，具有 preview/execute/verify 阶段、固定保留政策和明确权限；不发展为通用迁移系统。

普通部署移除旧 FX 重建、首次开空和历史转换步骤；空库/已有新基线库均通过 Alembic 初始化或升级，保留备份、审计、启动及健康检查。旧 revision 或未知 schema 在停写前拒绝普通部署，提示走业务重置流程。重置维护 workflow 沿用 thccb-production 并发锁和仓库所有者权限；必要的暂停变量在切换期衔接，切换后不保留旧兼容分支。

## 脚本归档清单

归档 8 个文件：backfill_fx_candles.py、fx_history_sources.py、prepare_fx_event_history.py、check_fx_rebuild_deploy.py、check_fx_short_rollout.py、migrate_loan_v1.py、rebuild_user_database.py、production_fx_rebuild.py。

同步归档旧 .github/workflows/fx-rebuild.yml、deploy/fx_rebuild.sh、旧换库 runbook，以及只验证已退休历史迁移/转换的测试。归档目录推荐 docs/archive/database-pre-baseline-2026-10-13/，位于 Alembic、pytest 和运行时代码加载路径之外。归档资料用于查看；真正恢复旧数据库使用保存的旧代码版本。

保留 8 个有实际功能的文件：audit_export.py、audit_verify.py、backfill_market_prices_post.py、backfill_outcome_candle.py、change_loan_daily_rate.py、season_reset.py、gen_lmsr_golden.py、simulate_pve_event.py。

season_reset 继续作为常规赛季维护工具，保留其未来赛季的兑换权益保护；本次业务重置的“兑换全清”由新重置入口执行，不悄悄改变未来常规赛季的政策。LMSR 两个回填工具属于现有预测市场功能修复，用户目前仅要求归档 FX 回填，不扩展删除范围。

scripts README 每项列用途、入口、是否读写数据库、需不需要停写、自动还是手动调用，并说明开发工具不接生产库。归档 README 说明退役原因、旧代码版本与恢复边界，不给出可直接误用的旧生产执行命令。

## 隐藏兼容与测试收敛

- 将 loan_migrate 的当前配置播种/校验职责拆到当前配置模块；归档启动补列、旧 leverage/threshold 映射及历史债务时间修补；更新当前功能引用，不保留旧名称兼容 alias。
- 删除 FxMarketDataState.last_trade_id 这一旧行情游标及依赖它判断旧库/回填的分支；保留真实 FxTrade、成交 receipt、n_trades，以及有当前用途的成交来源引用。不能因名称有 trade/legacy 就删掉现有玩家接口。
- 删除混合成交/资金审计到事件的历史转换和部署接入；保留实时同事务事件写入、有界消费、物化/行情读取、generation 与一致性检查。
- 历史迁移/转换专用测试与源文件一起归档；当前金融、API、安全、审计、事件行情、称号/兑换功能与日常维护行为继续验证。夹具改用当前真实逻辑，不削弱有效断言来换通过。
- 新 baseline 验证空 PG/SQLite 建完整 schema、模型兼容、索引/约束，以及未来增量升级方式。新增永久测试聚焦新保留/重置不变量、schema 版本边界、事件路径和恢复风险，不复制多层矩阵。
- UI 最多截图，不自动点击/填写或跑浏览器交互；不加入新的全站性能矩阵。

## 推荐取舍待最终确认

1. 保留完整称号体系，包括称号码和批次；用户已明确保留已有称号，但没有单独说明未使用称号码。
2. 保留有效当前配置而非全部恢复默认，经济功能关闭等待重新开市。
3. 用独立空库切换及新 baseline，旧版本只留在归档/旧镜像，不维护旧库自动升级兼容。

本设计确认后才写实施计划并执行代码；未经确认不触发生产重建、暂停生产部署或删除数据库。
