# context 依赖复用性能对照

唯一业务差异：read_trading_context 从已读 AccountBasis 构建依赖集合，省掉 read_dependencies 的三次 SQL。两个快照都包含上一轮短认证 session 修复；before 为 bde43b0 后端快照加该认证修复，after 在此基础上替换本次 trading_context.py。

真实 PostgreSQL 14、项目 backend/venv、单 Uvicorn worker、池10+20、loopback HTTP。前后依次运行，各用新建数据库、100个普通账户、每人现金100000、相同源池储备1000000/200000、相同开关和60/20/20角色；驱动随机种子20261017。异步调度和经济时间仍有差异，并非每笔请求时序完全相同。没有CPU profiler，仅有双方相同的查询计数与资源采样挂钩。

context 专项使用初始空仓账户加当前FX市场；混合阶段会建立真实仓位。每版先预热100次 context，再以并发10运行3轮、每轮200次 context。随后用既有真实混合驱动：冒烟10秒、入场间隔0.3秒、只读30秒、正常60秒、活跃45秒、加压30秒、集中下单后观察10秒；保持原滚动错误率和成交P99保护，条件单跳过。短阶段仅作对照，不作为持续容量验收。

| context 专项 | 原版 | 简化版 |
| --- | ---: | ---: |
| 成功请求数 | 600 | 600 |
| 总计吞吐（请求/s） | 100.7 | 112.73 |
| 平均延迟（ms） | 98.16 | 87.6 |
| 全轮池占用峰值 | 25 | 20 |
| 每次context SQL | [12.0, 12.0, 12.0] | [9.0, 9.0, 9.0] |

专项吞吐变化 11.9%，平均延迟变化 -10.8%。这是本机三轮短测的观测值，受运行顺序、缓存及机器负载影响；确定性的变化是每请求少3次数据库执行。

| 版本 | 混合阶段 | 成交成功/循环 | 成交/s | 成交P99 ms | HTTP分类 |
| --- | --- | --- | ---: | ---: | --- |
| before | normal | 153/153 | 2.508 | 377.38 | {'ok': 1169} |
| before | active | 334/339 | 7.187 | 1241.967 | {'ok': 2022} |
| before | stress | 332/379 | 9.437 | 14641.81 | {'ok': 1995, 'timeout': 23, 'version_conflict': 20} |
| after | normal | 150/150 | 2.459 | 298.956 | {'ok': 1157} |
| after | active | 339/343 | 7.315 | 767.395 | {'ok': 2039} |
| after | stress | 341/389 | 9.723 | 14641.2 | {'ok': 2053, 'timeout': 23, 'version_conflict': 16} |

before 停止原因：`30s window: successful trade P99 exceeds 2000ms`。后端池耗尽文字 0 次，异常堆栈 0 次。

after 停止原因：`30s window: successful trade P99 exceeds 2000ms`。后端池耗尽文字 0 次，异常堆栈 1 次。

报价未通过、版本冲突、成功写入、客户端超时须分别看原始summary；超时不代表未提交，没有自动重试。保护停测后的SSE 0/0包含客户端主动取消，不代表全部自然断流。

原始证据：每版 micro.json、sql-counts.json、http/summary.json、http/requests.jsonl、resources.jsonl、backend.log。临时入口：`backend/venv/bin/python /tmp/sunyunbo/fx-context-read-ab-20261017/run.py`。没有部署或压线上。


本轮结论：context 的数据库执行由12降至9（减少25%）；专项吞吐100.70→112.73请求/s，平均延迟98.16→87.60ms。活跃阶段成交P99 1241.97→767.40ms；正常阶段377.38→298.96ms。这些是单次前后顺序短测的观测值，尚无重复混合轮次证明其稳定幅度。高压成功成交仅9.44→9.72笔/s，P99均约14.64秒，均触发保护停止，未进入burst；仍不能承受合计25次/s的目标交易负载。现有串行交易路径的容量问题仍然存在。

简化版日志出现一次连接终止时的 CancelledError 堆栈，位于保护停止、取消HTTP/SSE请求附近；两版均未出现QueuePool耗尽报错。未将该堆栈当作业务请求500，也未隐去该观察。正常及活跃阶段两版HTTP采样全部成功，200条SSE保持在线；高压有客户端超时及版本冲突，详见上表。

验证：`cd backend && venv/bin/python -m pytest -q tests/test_trading_context.py`（2项通过）；指定临时PG测试库后执行 `venv/bin/python -m pytest -q -m pg tests/pg/test_pg_trading_context.py`（5项通过）。临时 audit_actual.py 导入最终生产转换函数，SQLite/PG各8组新旧依赖集合一致，保留零仓位、缺失身份、当前市场和用户隔离；真实PG并发新增/删除钱包均触发2次读取，连续变更3次后返回409。明细见 audit-results.jsonl。未执行全量测试、线上压测或部署。
