# 后端脚本

当前有 9 个功能工具，另有包标记 `__init__.py`。历史工具已移入[归档索引](../../docs/archive/database-pre-baseline-2026-10-13/README.md)，不在运行或部署路径。

命令在 `backend/` 下运行。数据库工具读取当前配置，操作前核对目标；停写包括后端、经济 writer、机器人及调度器。开发工具不连接生产库。

| 文件 | 用途与读写范围 | 停写要求 | 当前调用方式 |
| --- | --- | --- | --- |
| [audit_export.py](audit_export.py) | 导出审计事件及后状态 CSV，或折叠指定时刻快照；数据库只读，导出模式写本地文件 | 无强制停写要求；优先使用只读副本或备份恢复库 | 手工 CLI |
| [audit_verify.py](audit_verify.py) | 折叠事件流、检查内部一致性并比对当前业务表；只读，差异退出码为 1 | 部署及重置后的验收在停写时运行；`--no-live` 仅检查事件流 | 手工 CLI；`deploy/deploy.sh`、`deploy/season_reset.sh`、`deploy/business_reset.sh` 自动调用 |
| [backfill_market_prices_post.py](backfill_market_prices_post.py) | 重放 LMSR，补齐交易中为空的 `market_prices_post`；执行时更新数据库 | 执行回填前应停止相关市场写入；脚本无停写门禁 | 手工 CLI；已有测试调用回填函数 |
| [backfill_outcome_candle.py](backfill_outcome_candle.py) | 按市场清除旧 `outcome_candle` 后重新回填；清除和回填分别提交 | 执行前应停止相关市场写入及 K 线写入；脚本无停写门禁 | 手工 CLI；迁移和启动也有 K 线回填，但未直接调用本脚本 |
| [change_loan_daily_rate.py](change_loan_daily_rate.py) | 按旧利率结清金债和外币利息，再在同一事务切换日利率；写数据库，要求已迁移的 PostgreSQL | 必须停止全部经济写入 | 手工 CLI；无 dry-run，不负责初始化或迁移 |
| [season_reset.py](season_reset.py) | 清理赛季活动数据、重置资产和审计锚点；保留账号、称号和兑换记录 | 执行前必须停止后端及经济写入 | 手工 CLI；`deploy/season_reset.sh` 调用；`--dry-run` 只预览 |
| [gen_lmsr_golden.py](gen_lmsr_golden.py) | 生成前端 LMSR 对拍数据到标准输出；不访问数据库，重定向时写文件 | 无 | 开发工具，手工更新前端 `lmsr.golden.json` |
| [simulate_pve_event.py](simulate_pve_event.py) | 用内存账本比较 PVE 参数；不访问数据库，`--output` 可写 JSON | 无 | 开发 CLI；已有测试调用模拟函数 |

| [business_reset.py](business_reset.py) | 固定源 initial_balance 的新基线业务重置；保留身份和完整称号，清空兑换与经济业务；导出写私有文件，导入写独立空目标，验证只读 | 执行必须停止全部写入；目标只读验收后再启动 owner | `deploy/business_reset.sh`，专用 workflow 分阶段调用 |

`season_reset.py` 保留未来赛季的兑换权益和对应审计。本次 `business_reset.py` 清空全部兑换数据，现金由源库 `siteconfig.initial_balance` 决定；expected_initial_balance 只核对源值，不能覆盖金额。

查看帮助和只读预览：

```bash
python scripts/audit_export.py --help
python scripts/change_loan_daily_rate.py --help
python -m scripts.simulate_pve_event --help
python scripts/audit_verify.py --no-live
python -m scripts.backfill_market_prices_post --dry-run
python -m scripts.backfill_outcome_candle --dry-run
python scripts/season_reset.py --dry-run
python scripts/business_reset.py --help
```

重置入口、权限、备份和恢复边界见[业务重置操作单](../../docs/business-reset.md)。审计截断使用 `--upto-id <事件ID> --no-live`，不能把旧截止快照直接与当前库比对。
