# FX 央行与新闻清理验证记录

基线：`origin/main` 的 `1af6678`。实施分支：`feat/fx-pure-amm`，工作区 `/tmp/sunyunbo/touhouccb-pure-amm`。

用户指定执行为 GPT-6.1 Sol low，审阅为 GPT-6.1 Sol medium；后端、前端、用户迁移和集成验证分别有明确文件所有权。此记录只涉及代码和隔离测试，未推送、合并、部署、连接生产库或执行生产数据删除。

## 功能范围

- 删除 FX 目标回归、噪声及事件引擎、新闻 UI/API/SSE/轮询、废弃表列和配置。
- 保留 AMM、注撤资、借贷／开空／回补、全仓风控和既有强平算法；注撤资代码移至 liquidity。
- 保留 main 的增量行情、ring、K 线持久化与恢复。
- 提供用户白名单 inspect/export/import/verify 工具，保留身份、权限、资料和已购权益依赖，现金按显式政策重置，旧债务和持仓清零。
- 普通部署对旧干预 schema 在停服务之前拒绝自动迁移；正式切换使用新库重建操作单。

## 验证证据

以下命令在实施工作区执行。backend 的 `venv` 和 frontend 的 `node_modules` 复用现有安装；PostgreSQL 14.24 是本次在 `/tmp` 下建立的独立实例，端口 35449，三个 fx_amm_*_test 数据库均为合成测试数据。

| 检查 | 实际结果 |
| --- | --- |
| 前端 `npm run test:unit` | 14 文件、151 项通过 |
| 前端 `npm run type-check`、`npm run build` | 通过；保留已有 bundle chunk-size 告警 |
| 后端同步夹具／schema／API／历史审计相关检查 | 59 项通过 |
| 补齐历史系统买卖审计夹具后 replay／reset | 7 项通过 |
| 后端 AMM、现货、多空、强平、统一配置及行情相关回归 | 首轮 271 通过，1 个旧模块导入失败，2 个推送测试因目录保护跳过；修复导入并换直接 /tmp 测试库后，这 3 项全部通过 |
| 独立端到端＋部署 schema 保护 | 18 项通过；保护脚本异常信息调整后又补跑 4 项通过 |
| PG 强平／空头并发两文件 | 首轮 11 通过，1 个适配后 source 字符串预期错误；修正为实际 player 后该项通过 |
| 用户重建 SQLite 双库集成 | 首轮 2 项通过；审阅发现身份缺字段风险后补充真实回归，3 项通过 |
| 用户重建 PG 双库演练 | 通过：1 个禁用管理员身份、2 条称号资料、余额／债务重置、重复导入拒绝、市场／选项／FX pair 序列高水位 777／888／999 后新 ID 均不复用 |
| PG 信贷 schema／约束文件 `tests/pg/test_credit_pg.py` | 4 项通过 |
| PG 清理 migration 升降级与 metadata 对照 | 通过；随机隔离 schema，检查结构及用户现金／债务、池子与借币余额保留，事务回滚清理 |
| Python compileall、部署脚本 bash -n、git diff --check | 通过 |

后端集中回归命令（backend 目录；显式隔离 DATABASE_URL，外层 timeout 480）：

```bash
venv/bin/python -m pytest -q tests/test_fx_amm.py tests/test_fx_trading_service.py tests/test_fx_trading_in_session.py tests/test_fx_short_trading.py tests/test_fx_short_api.py tests/test_fx_short_liquidation.py tests/test_fx_liquidation_service.py tests/test_credit_sweep.py tests/test_fx_short_audit_replay.py tests/test_fx_season_reset.py tests/test_credit_wp6b_fx_ops.py tests/test_credit_config.py tests/test_fx_candle_pipeline.py tests/test_fx_market_data_integration.py tests/test_fx_history.py tests/test_fx_stream.py tests/test_fx_publication_e2e.py
```

补跑命令（DATABASE_URL 指向 `/tmp/fx-pure-amm-targeted-test.db`）：

```bash
venv/bin/python -m pytest -q tests/test_fx_short_liquidation.py::test_treasury_operations_preserve_borrowed_stock_and_unknown_debt_scan tests/test_fx_publication_e2e.py
```

初次沙箱内 aiosqlite 夹具挂起的会话已停止，未计为通过；改在允许本机测试资源访问的环境运行后得到上述结果。环境未安装 pytest-timeout，pytest 发出两个既有未知配置告警；集中命令外层另设系统 timeout。工具的实际 SSO 远端登录、Docker 内部署命令和生产切换未执行。无浏览器自动交互；本次未额外截图，也没有新的生产性能测量。

## 审阅与修复

- 前端 medium scoped review：通过，无问题。
- 集成／部署保护 medium scoped review：通过，无问题；Docker 实际调用仍属未验证项。
- 重建工具 medium review 发现损坏 manifest 缺少 is_active 时可能被默认值激活；已改为要求完整身份键集，在接触目标库前拒绝，回归检查目标文件未创建。修复复审通过。
- 后端 medium review：资金函数逐项核对为原样迁移，未发现生产逻辑缺陷；要求恢复原 frame 测试的 price/spread/volume 断言，已原位恢复并通过该文件 4 项测试。
- PG schema 补充检查已通过；最终整分支审阅完成后追加结果。

## 实施中修正的计划假设

仓库最早 Alembic revision 仅作版本标记，空库不能直接 upgrade head 建出全部表。按现有项目机制改为先确认目标完全为空、当前模型建表、stamp 实际 head；另用旧 schema 的 migration 升降级验证结构及用户资金保留。若两条结构不一致会导致启动失败或读写错误，因此专门核对 metadata 与迁移结构，不直接调用有清库行为的 init_db。

## 正式切换前需要固定的值

实际源／目标库、用户导入清单和新开局金额由部署操作单明确。SQLite 没有 AUTOINCREMENT 的旧市场表无法仅凭 MAX(id) 恢复已删历史 ID，工具要求显式提供经核对的高水位；不能把缓存清除代替服务端 ID 不复用保证。正式删除旧库前必须完成一致备份、恢复验证和新库核对。
