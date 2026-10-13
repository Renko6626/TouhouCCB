# 后端脚本

本目录目前有 16 个业务脚本或辅助模块，另有包标记 `__init__.py`。下表按拟保留的功能工具与拟归档的历史工具分组。**归档方案尚待确认和实施，所有文件仍在当前目录，现有部署调用也仍有效。**

命令默认在 `backend/` 下运行，使用项目 Python 环境。连接数据库的脚本读取当前配置；运行前核对目标库。表中的“停写”包括后端 API、经济 writer、机器人及结息/强平调度器等写入进程。只读校验虽然不改库，在线比对仍可能受到并发提交影响。

## 拟保留的功能工具（8 个）

| 文件 | 用途与读写范围 | 停写要求 | 当前调用方式 |
| --- | --- | --- | --- |
| [audit_export.py](audit_export.py) | 导出审计事件及后状态 CSV，或折叠指定时刻快照；数据库只读，导出模式写本地文件 | 无强制停写要求；优先使用只读副本或备份恢复库 | 手工 CLI |
| [audit_verify.py](audit_verify.py) | 折叠事件流、检查内部一致性并比对当前业务表；只读，差异退出码为 1 | 部署及重置后的验收在停写时运行；`--no-live` 仅检查事件流 | 手工 CLI；`deploy/deploy.sh`、`deploy/season_reset.sh`、`deploy/fx_rebuild.sh` 自动调用 |
| [backfill_market_prices_post.py](backfill_market_prices_post.py) | 重放 LMSR，补齐交易中为空的 `market_prices_post`；执行时更新数据库 | 执行回填前应停止相关市场写入；脚本无停写门禁 | 手工 CLI；已有测试调用回填函数 |
| [backfill_outcome_candle.py](backfill_outcome_candle.py) | 按市场清除旧 `outcome_candle` 后重新回填；清除和回填分别提交 | 执行前应停止相关市场写入及 K 线写入；脚本无停写门禁 | 手工 CLI；迁移和启动也有 K 线回填，但未直接调用本脚本 |
| [change_loan_daily_rate.py](change_loan_daily_rate.py) | 按旧利率结清金债和外币利息，再在同一事务切换日利率；写数据库，要求已迁移的 PostgreSQL | 必须停止全部经济写入 | 手工 CLI；无 dry-run，不负责初始化或迁移 |
| [season_reset.py](season_reset.py) | 清理赛季活动数据、重置资产和审计锚点；保留账号、称号和兑换记录 | 执行前必须停止后端及经济写入 | 手工 CLI；`deploy/season_reset.sh` 调用；`--dry-run` 只预览 |
| [gen_lmsr_golden.py](gen_lmsr_golden.py) | 生成前端 LMSR 对拍数据到标准输出；不访问数据库，重定向时写文件 | 无 | 开发工具，手工更新前端 `lmsr.golden.json` |
| [simulate_pve_event.py](simulate_pve_event.py) | 用内存账本比较 PVE 参数；不访问数据库，`--output` 可写 JSON | 无 | 开发 CLI；已有测试调用模拟函数 |

`season_reset.py` 当前保留兑换伙伴、批次、兑换码（含已售记录）、兑换交易、弹幕兑换及兑换审计，避免丢失已经购买的激活码。拟议的业务重置要清除全部兑换数据，其规则与当前赛季重置不同；该业务重置及新的迁移基线尚未实施，不能用现有赛季重置命令替代。

## 拟归档的历史工具（8 个，尚未归档）

这些文件服务于旧数据转换、旧版本准入或数据库重建。部分仍在正常部署链中；归档实施时需要一并处理调用方，当前不能直接移走。

| 文件 | 用途与读写范围 | 停写要求 | 当前调用方式 |
| --- | --- | --- | --- |
| [backfill_fx_candles.py](backfill_fx_candles.py) | 转换固定截止点的 FX 历史来源，重建池事件、K 线和历史游标/版本；不改原始成交、资金审计或余额 | 必须停止经济写入；获取写所有权及 pair 维护锁，dry-run 也执行来源校验和保护流程 | 手工 CLI；由 `prepare_fx_event_history.py` 调用；已有集成测试调用 |
| [fx_history_sources.py](fx_history_sources.py) | 分页投影历史来源并校验时间顺序与储备链；数据库读取辅助库 | 随离线重建的停写要求 | 无 CLI；供 `backfill_fx_candles.py` 导入 |
| [prepare_fx_event_history.py](prepare_fx_event_history.py) | 找出未就绪且有来源的 FX 市场，先全量校验再转换；会写数据库，缺少可重建来源时拒绝执行 | 必须停写，在 schema 升级后、后端启动前运行 | `deploy/deploy.sh` 自动调用；复用 FX 回填工具 |
| [check_fx_rebuild_deploy.py](check_fx_rebuild_deploy.py) | 检查旧干预型 FX schema，发现旧表/字段时拒绝普通部署；只读 | 本身无需停写 | `deploy/deploy.sh` 自动调用；已有测试调用检查函数 |
| [check_fx_short_rollout.py](check_fx_short_rollout.py) | 检查迁移版本与开空闸，区分首次上线和常规升级；只读 | 本身无需停写；首次上线停写后再次确认开空闸关闭 | `deploy/deploy.sh` 自动调用，复查使用 `--require-closed` |
| [migrate_loan_v1.py](migrate_loan_v1.py) | 调用现行幂等借贷配置迁移 `auto_migrate()`；会写数据库 | 手工运行时应停止经济写入；CLI 无停写门禁 | 手工模块入口；后端启动直接调用同一服务，未调用此脚本 |
| [rebuild_user_database.py](rebuild_user_database.py) | 从显式源库导出账号/权益到文件，向独立空目标库导入并验证；导入写目标库 | 最终导出和切换须停写，按既有重建方案执行 | 手工分阶段 CLI；由 `production_fx_rebuild.py` 调用 |
| [production_fx_rebuild.py](production_fx_rebuild.py) | 固定 Compose 数据源的重建适配器；预览/导出写本地文件，导入写目标库，验证读取目标库 | 遵循重建入口的停写和切换流程 | `deploy/fx_rebuild.sh` 调用；复用账号/权益重建工具 |

## 查看参数与只读预览

以下示例只展示已实现的帮助、校验或预览入口，不执行重置、历史转换或数据库导入。数据库预览仍需正确配置目标库；FX dry-run 要求停写。

```bash
python scripts/audit_export.py --help
python scripts/change_loan_daily_rate.py --help
python -m scripts.simulate_pve_event --help
python scripts/audit_verify.py --no-live
python -m scripts.backfill_market_prices_post --dry-run
python -m scripts.backfill_outcome_candle --dry-run
python scripts/season_reset.py --dry-run
python -m scripts.backfill_fx_candles --all --dry-run
```

截断审计流时可用 `audit_verify.py --upto-id <事件ID> --no-live`；历史截止点的状态通常不能直接与当前业务表比对。`prepare_fx_event_history.py` 没有独立 dry-run 参数，直接运行会在校验成功后转换数据；`fx_history_sources.py` 是辅助库，不是操作入口。

相关说明：[审计事件](../../docs/audit-events.md)、[利率维护](../../docs/unified-credit-risk-2026-10.md)、[现有赛季重置](../../docs/season-reset-2026-09-27.md)、[旧 FX 数据库重建](../../docs/fx-database-rebuild.md)、[PVE 模拟](../../docs/pve.md)。
