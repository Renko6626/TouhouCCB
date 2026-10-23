# 4Hz 调整记录

状态：最小调整已提交，等待主 agent 安排 Sol High 增量审阅。没有运行200人测试，没有 push 或部署。

- 工作目录：`/tmp/sunyunbo/thccb-sse-20261023/implementation`
- 分支：`perf/sse-cadence-20261023`
- 8Hz 历史基线保持：`e9c0242019cc5aecd4cbcd29ca5d27b4191cee62`
- 本次 commit：`7406d9c6caabfd3501c5f1279f8e6c3db22e86ae`

开始前确认 HEAD 为 e9c0242，tracked 文件没有未提交更改；四份未跟踪的已有文档保留，未加入 commit。

确切改动仅四行：公共 FX publisher、私人 pricing publisher 两处 `PUBLISH_INTERVAL` 从 `0.125` 改为 `0.25`；对应公共实际发布开始间隔和私人实际读取开始间隔的两个测试阈值从 `>= 0.125` 改为 `>= 0.25`。没有需要更改的125ms命名或注释。公共空闲等待、事件触发完整窗口、FIFO批预算、生命周期及私人wake算法均未改动；LMSR公共tick没有调整。

在 backend 目录执行：

```bash
env -u DATABASE_URL /tmp/sunyunbo/thccb-pr23-review-20261023/review-venv/bin/python -m pytest -q tests/test_fx_publisher.py tests/test_trading_stream.py tests/test_account_events.py tests/test_fx_stream.py tests/test_tick_broadcaster.py tests/test_market_read_state.py
```

结果：exit 0，55 passed in 10.37s。解释器 Python 3.13.5，数据库仍为测试独立 SQLite，未使用外部 DATABASE_URL。

`git diff --check`：exit 0，无输出。自审差异为四文件各一行替换，未增加测试或修改原有业务断言；没有新增机制、接口、依赖或范围外修改。

这些检查验证4Hz默认间隔及既有调度/消息语义，不构成性能收益、公网交付时限、PostgreSQL锁语义或全部通知必达证据。后续增量审阅请核对两个生产常量和两个独立250ms行为阈值。
