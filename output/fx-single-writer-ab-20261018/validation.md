# 唯一 writer 迁移验证记录

固定功能候选：6c7675722cda265bd205dced6ce0f939ae7ac769；分阶段、整体与文档修正复审均批准。固定代码 A/B 已完成并保留保护停止结果；主工作区已集成，未推送或部署。

- 核心 FX 与其余 FX 写入口、LMSR 生命周期、普通快照读取已分阶段实现和独立审阅，详见 validation/ 内报告。
- 风控最终相关 354 个单元场景均有通过证据：集中执行后，对测试夹具/旧语义断言的修正仅补跑受影响项；未宣称最终完整命令一次全绿。另有 61 个相关管理、消费、空头 API/集成场景通过。
- 45 个 PostgreSQL 场景均有通过证据：最终七模块集中命令 44 通过、1 个新夹具版本预期修正后单项通过，无跳过。临时数据库 fx_single_writer_correctness_test，经本机 Unix socket55489 访问。
- 新增跨两个 FX writer 同一用户融资：自有现金可付两笔，第二笔读取第一笔的新债务、持仓和版本，由真实信用检查拒绝，非现金不足。
- 强平采用一次已发布外市场行情；操作后复读本人账本与实际目标市场新状态，继续使用原外市场行情。100 人并行强平首轮允许原有有限选择重试耗尽，波次结束后仅对该部分用户做一次已有调用补扫；最终仍断言100唯一资金动作、事件和账户结果。生产排序与3次重试策略未改变。
- 前端交易 store 19 项通过；指南12页，XeLaTeX 两次编译通过，未执行浏览器交互。
- 后端测试仅收集检查：1528选中/1587总数，59个PG默认取消选择，无导入错误；这不是全仓测试执行。
- 控制器实际运行 git diff --check 330f4e6 b413408，退出0。

实际命令、初始失败、局部修正及限制见 [最终风险报告](validation/task-4-report.md)、[四文件测试报告](validation/task-4-tests-report.md)、[普通读取报告](validation/task-4a-report.md)。

A/B 两侧固定代码均保留测试基线的每IP200连接额度，以支持同一loopback的100模拟用户；用户主工作区原有每IP10额度会保留，迁移不修改此策略。未把本机单次实验解释为生产容量保证。

## 集成检查与收尾

主控制器在 `/data/sunyunbo/www/TouhouCCB/backend` 执行：

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/sunyunbo/fx-single-writer-root-verification-20261018.db venv/bin/python -m pytest -q tests/test_fx_writer.py tests/test_market_read_state.py tests/test_trading_context.py tests/test_trading_stream.py tests/test_admin_fx.py tests/test_fx_trading_service.py tests/test_fx_net_trading.py
```

结果：115 passed in 22.18s。主工作区 `git diff --check` 退出0。这不是全仓测试。

集成前521个源文件/文档哈希未变，迁移patch先通过 `git apply --check` 再应用；70个迁移路径有备份。集成后454个未涉及文件哈希保持不变；迁移文件仅保留原排行榜接口与API说明差异，原 `realtime.py` 每IP10限额保持。没有reset/stash/整目录覆盖。

A/B 两边都因原有30秒窗口成功trade P99>2000ms保护停止，均未执行burst；这不是名义合计25cycles/s容量通过。HTTP/monitor/driver进程已退出，临时token已移除；主控制器随后 `pg_ctl -D /tmp/sunyunbo/fx-pool-incident-20261017/pgdata -m fast stop` 成功停止独立PG。

审阅结论及暂缓事项见 [code-review.md](code-review.md)，性能见 [report.md](report.md)。

最终计划记录提交 `58101e1` 只更新文档，实测后端仍是 `6c76757`；隔离分支和工作树保留，未推送或部署。
