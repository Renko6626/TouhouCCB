# 内存行情迁移验证记录

生产源码范围：`a820157..e50b0b3`；最终测试隔离修正：`330f4e6`。实施分支 `perf/committed-market-reads-20261018`，隔离目录 `/tmp/sunyunbo/thccb-fx-read-checkpoint-20261017`。

## 自动检查

全部命令在隔离目录的 `backend` 执行，解释器 `/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python`。

- FX 全部提交入口、管理及快照相关组：244 passed in45.14s。
- LMSR writer/state/admin/context/buy/sell/liquidation/creation：42 passed in8.49s；最后仅新增安装失败用例，受影响 admin 文件6 passed in1.05s。
- 公共行情/context/SSE wire：21 passed in3.58s；修正 SQL 观察后，warm输入用例及交易/session兼容41 passed in7.76s。
- 完整迁移的相关文件并集317项：315 passed /2 failed in60.84s。失败为启动测试留下的 Registry 内存配置指向另一测试库；调用方市场实际存在。修复只在现有测试数据库夹具中重置 Registry，并恢复普通测试的读库模式，未改生产实现或放宽断言。最小复现组合修复后3 passed in0.43s；受影响 fixture 组合52 passed in10.08s。未重新运行全部317项。

修复后的相关组合命令：

```sh
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q tests/test_credit_startup_gating.py tests/test_fx_trading_service.py tests/test_market_read_state.py tests/test_market_writer_state.py tests/test_market_writer_loop.py tests/test_writer_admin_ops.py tests/test_trading_context.py::test_pricing_publisher_shares_serialization_and_filters_dependencies tests/test_fx_market_data_integration.py::test_warm_public_quotes_share_committed_inputs_without_pair_sql
```

真实 PostgreSQL 14 的最终三文件并集在最终生产源码上执行，18 passed in13.33s，无 skip：

```sh
TEST_PG_DATABASE_URL='postgresql+asyncpg://sunyunbo@/fx_market_memory_test?host=/tmp/sunyunbo/fx-pool-incident-20261017/socket&port=55489' /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q -m pg tests/pg/test_pg_trading_context.py tests/pg/test_pg_fx_history_locking.py tests/pg/test_pg_fx_trigger_orders.py
```

## 审阅及范围

基础组件、FX 写入口、LMSR 生命周期、公开行情四组均独立审阅通过。公开行情测试隔离修正由原审阅者单独复审通过。整组审阅和性能报告另见本目录后续记录。

已验证的核心行为包括提交后安装顺序、回滚/旧重放不倒退、触发单和强平真实提交入口、费率数据库精度、创建与晚到冷查询竞争、LMSR不可变投影及重载、账户上下文GATE覆盖、公共SSE版本回退和原历史锁证明、读取只读实例回退、暖态市场输入零SQL、多人广播复用。

未运行全仓测试或浏览器交互；不更改生产配置、不部署。目录列表、认证/用户依赖、成交量/历史SQL及公共SSE共享行锁仍保留。SSE消费者任务/取消优化不属于本次迁移。

## 回退

回退本次内存迁移代码并重启即可，无schema回退；保留此前融资及用户依赖查询优化。维护时直接SQL改当前市场状态后仍需重启或走现有reload，以恢复进程内状态。

整组独立代码审阅已通过，无未解决Critical/Important/Minor；见[code-review.md](code-review.md)。审阅通过的a820157..330f4e6限定补丁已应用主工作区，共33文件；应用前保护文件hash未变，应用后所有迁移文件与审阅分支逐字节相同，仅market.py保留既有排行榜差异（原差异hunk逐项相同）。没有合并分支、推送或部署。

负载结束后复用现有历史滚动窗口检查（未新增测试）：`/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q tests/test_fx_history.py -k volume_24h`，4 passed/25 deselected in0.84s。覆盖窗口边界、flush读取一致性、未就绪和空市场；这不等同于真实等待24小时到期的演练。
