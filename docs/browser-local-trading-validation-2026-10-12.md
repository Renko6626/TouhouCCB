# 浏览器本地交易预估与账户同步：实施验证记录

日期：2026-10-12。工作区 `/tmp/sunyunbo/thccb-browser-local-trading-20261012`，分支 `ralph/2026-10-12-browser-local-trading`，基线 `559552f`。状态：Task0–6实施及medium最终复审完成；本记录截至提交发布前。用户已授权推送main并触发现有CI/CD，部署结果以该提交的流水线记录为准。以下证据不表示浏览器产品验收。

## 已确认参数

本地金额／仓位预估沿用6位小数业务精度，已有费率与中间数学输入不提前截断。账户事件合并200ms、活跃页面轻量版本校对15000ms、本地计息1000ms，作为代码常量／文档注释，不新增后台配置项。实现模型gpt-6.1-sol low，审阅gpt-6.1-sol medium。

## 基线

- frontend：`npm run test:unit`，16 files／201 passed，exit0，833ms。
- backend：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-local-baseline-20261012-escalated.db venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_credit_valuation.py tests/test_credit_lmsr_quote.py`，53 passed，exit0，7.87s。
- 同一后端源码首次在沙箱内使用新临时SQLite库，于aiosqlite初始化fixture超时；沙箱外以上命令通过。没有为此放宽timeout或修改测试。

## 前置调查与审阅

Task0数值探针采用真实Python生产AMM／计息／LMSR组清算和已有JS工具，无数据库查询。FX已测定点向量（含8dp费率、exact-output两次向上取整）结果一致；价格饱和与大份额下LMSR旧价格闭式有具体失真，新的组合清算使用完整q稳定算法。大额非线性利息存在末位差异，按6位业务预估与边界未知处理，不创建通用区间算术框架或新依赖。

报告与审阅在 `.superpowers/sdd/2026-10-12-browser-local-trading-account-sync/task-0-report.md`、`task-0-review.md`；Spec PASS／Quality PASS，无未解决Critical／Important。它们是前置方案证据，不是新产品验收。

## 环境与限制

本任务独立PostgreSQL14集群只监听127.0.0.1:55544，库名thccb_browser_local_test，data在`/tmp/sunyunbo/thccb-browser-local-pg-20261012/data`。Task1已完成新功能PG读取窗口验证；结束时停掉本任务集群，保留验证产物。

不访问业务数据库、未进行浏览器自动点击／填表／真实交易，未提交推送、合并或部署。主工作区原有文档和截图保持原样。后续阶段实际验证与未完成范围将在完成后补录。

## Task1：账户聚合与定价版本

实现账户交易基准、依赖定价组、覆盖下限和轻量版本／补价接口，保持账户与定价两个版本域。medium审阅Spec PASS／Quality Approved，无Critical／Important；配置冷缓存批量读取建议列入后续审阅。

- `DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-trading-context-task1-root.db venv/bin/python -m pytest -q tests/test_trading_context.py tests/test_user_summary_contract.py tests/test_credit_valuation.py tests/test_credit_lmsr_quote.py`：28 passed／3.84s，exit0。
- `TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55544/thccb_browser_local_test DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-trading-context-task1-pg-root.db venv/bin/python -m pytest -q -m pg tests/pg/test_pg_trading_context.py`：2 passed／1.01s，exit0，无skip。

以上测试由主agent执行。子agent曾停在工具提权等待，未启动测试进程；后续后端验证统一由主agent运行，不将该等待报告成产品回归。

## Task2：提交后通知与私有交易流

相关后端检查首轮共240项：236 passed、3 failed、1 skipped，37.97s。失败涉及新增测试夹具位置及既有测试适配器缺少真实AsyncSession接口；修复后相关三文件23 passed／3.36s。最后凭证到期处理改动通过私有流四项检查（4 passed／0.52s）。没有放宽行为断言或重复整套检查。

- 首轮命令：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-trading-stream-task2-root.db venv/bin/python -m pytest -q tests/test_account_events.py tests/test_trading_stream.py tests/test_fx_net_trading.py tests/test_loan_api.py tests/test_admin_amnesty.py tests/test_credit_liquidate_group.py tests/test_writer_admin_ops.py tests/test_loan_sweep.py tests/test_redemption_service.py tests/test_danmuku_service.py tests/test_site_config.py tests/test_credit_execution.py tests/test_fx_trading_service.py tests/test_fx_short_trading.py tests/test_credit_rate_maintenance.py tests/test_credit_wp6b_admin.py tests/test_loan_admin.py tests/test_admin_fx.py tests/test_auth.py`。
- 修复检查：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-trading-stream-task2-fix-root.db venv/bin/python -m pytest -q tests/test_account_events.py tests/test_admin_amnesty.py tests/test_fx_trading_service.py`，exit0。
- 最后检查：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-trading-stream-task2-expiry-root.db venv/bin/python -m pytest -q tests/test_trading_stream.py`，exit0。

跳过项为既有兑换最后一个码并发场景；SQLite不支持FOR UPDATE SKIP LOCKED，本轮未验证该PG专属场景。权限已切换full access，无需审批，后续验证直接执行；仍只使用任务临时库。

Task2 medium审阅发现持续帧使定时检查饥饿，已修复：检查独立于出队成功，出队后再检查跨期窗口。新增持续排队情况下规则更新／封禁关闭ASGI覆盖。`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-trading-stream-task2-reviewfix2-root.db venv/bin/python -m pytest -q tests/test_trading_stream.py`：6 passed／0.85s，exit0；medium复审I1 ADDRESSED，无新增Critical／Important。两个Minor（嵌套回滚过期ORM导致外层提示可能丢失、定价排队第二次提交指定证据未覆盖）留待最终审阅，末条提示丢失仍由版本校对恢复。

## Task3：本地纯计算

真实后端无SQL探针获取净额交易及第二腿固定结果；新增黄金输入保留来源SHA、费率、储备、时点。前端针对性命令 `npm run test:unit -- src/utils/__tests__/fxSimulation.spec.ts src/utils/__tests__/tradingRisk.spec.ts src/utils/__tests__/lmsr.spec.ts src/utils/__tests__/fx.spec.ts`：4 files／79 passed／670ms；`npm run type-check` exit0。

审阅发现债务估值失败误报健康、融资中间余额／逐腿价格上限缺失，已修复；复审又发现共用报价上限影响合法估值，已将普通买卖的价格上限限定到订单准入。最终相关两文件13 passed／368ms，type-check exit0；medium第二轮复审PASS，无新增Critical／Important。原Minor（回补受无关利息近似影响而过度保守）列入最终审阅。未进行页面运行验收。

## Task4–5与最终检查

Task4认证流／会话／账号绑定请求26项针对性行为检查通过，type-check及新增文件只读ESLint通过。medium复审确认前台恢复权威屏障、强制刷新不被旧候选取消、旧身份HTTP错误不能影响新登录，三个Important均已修复。

Task5 medium复审确认完整q计算LMSR交易后现金／风险、旧short exact-output部分回补、首页风险组件兼容三个Important均已修复。最后改动后6 files／75 passed，type-check exit0；只读ESLint helper／组件通过，TradingView仍有5项既有错误（NTag、realtimeStatusType、三处any），未执行--fix。`npm run build` exit0／20.39s，保留empty-charts、auth动态／静态导入和chunk-size警告。

Task6在最后纯函数改动后执行一次 `npm run test:unit`：21 files／242 passed，exit0；test时间863ms，总1.12s。没有重复未改变的type-check／build。上述都是单元／构建证据，未进行浏览器截图或交互验收。

后端最终代码自Task2后未改变，由主agent执行以下命令（cwd backend）：

- `venv/bin/python -m compileall -q app`：exit0。
- `DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-local-final-import.db venv/bin/python -c 'import app.main'`：exit0；开发随机SECRET、Casdoor未配置提示，无生产dotenv。
- `DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-local-final-pytest.db venv/bin/python -m pytest -x`：1435 passed、1 skipped、40 PG deselected，1 warning，246.00s，exit0。
- `TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55544/thccb_browser_local_test DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-local-final-pg.db venv/bin/python -m pytest -q -m pg tests/pg/test_pg_trading_context.py tests/pg/test_pg_credit_liquidation.py tests/pg/test_pg_credit_admission_contention.py tests/pg/test_pg_fx_short_concurrency.py`：22 passed，27.01s，exit0，无skip。

唯一SQLite skip是既有兑换最后一码的FOR UPDATE SKIP LOCKED并发场景；指定PG清单不覆盖该场景。warning来自未修改的rebuild_user_database.py:78的aiosqlite连接垃圾回收，不代表已诊断新的SSE泄漏。

## 验收对应与证据边界

| 需求 | 对应证据／限制 |
| --- | --- |
| 初始化跨版本／覆盖下限 | test_trading_context与PG读窗口；store provisional sync、epoch replacement、foreground barrier行为检查。 |
| 双净值、全组清算、未知边界、纯减仓 | fxSimulation/tradingRisk/LMSR纯函数及生产Python黄金输入；完整q极端、含息cover、其他锁金、冻结减少风险。未浏览器验证。 |
| 借款／强平／管理员提交后提示 | 原owner挂钩与相关资金检查通过；并非每个owner均有独立“真实提交后事件”断言，不能视为计划完整矩阵覆盖。 |
| 本人成功响应／连续提示合并 | tradingSession mock transport用例验证200ms、较高目标、去重；实际浏览器网络次数未测。 |
| 末条账户提示／定价遗漏 | store mock版本校对恢复，价变只补价；真实HTTP验证规则／封禁及凭证到期、队列清理。 |
| 有效rule／凭证失效／账号切换 | 后端有效规则与持续队列检查；前端规则身份、延迟HTTP／旧generation隔离。 |
| 旧pending／成功后刷新失败 | 保留既有恢复用例和完整请求；旧spot无持久pending机制，未新增其恢复存储，不能声称覆盖所有旧spot恢复。 |
| 价变零summary／net quote | 页面已接共享会话，store五组行情mock计数零quote/context；不是生产抓包结论。 |

遗留Minor已由最终审阅裁定为不阻塞发布：规则冷缓存五getter可批量优化；嵌套回滚过期ORM可能丢外层提示（当前生产owner未见该嵌套路径，版本校对可恢复）；排队发布期间第二次提交未有指定测试；exact-cover受无关利息近似影响而过度保守；融资checkbox提示与捕获同意金额不完全一致。未擅自扩大修复范围。

本记录保留既有阶段证据，不把检查数、临时指标或文档完成当成产品验收。主工作区四份用户历史文档／截图的106项hash核对由主agent完成且未变；当前尚未提交、PR、合并、推送或部署。最终整段medium审阅与修复结果见末节。

## 小范围临时指标

主agent从backend运行 `DATABASE_URL=sqlite+aiosqlite:////tmp/sunyunbo/task6-trading-metrics-20261012.db venv/bin/python /tmp/sunyunbo/task6-trading-metrics.py`，exit0。匿名账号仅五个FX持仓组，固定储备1000／1000；未成交。真实认证路由通过ASGITransport调用，计数SQLAlchemy执行语句，不启动网络socket／应用lifespan。原始结果在 `.superpowers/sdd/2026-10-12-browser-local-trading-account-sync/task-6-metrics.json`。

| 操作 | HTTP数 | SQL数 | 观察耗时ms |
| --- | ---: | ---: | ---: |
| 首次context | 1 | 21 | 41.220 |
| 已暖缓存、未变versions | 1 | 16 | 14.110 |
| 五组prices | 1 | 9 | 10.061 |
| 合成单个私有队列提示enqueue／读取 | 0 | 0 | 0.046 |
| 合成5个队列提示enqueue／读取 | 0 | 0 | 0.096 |
| 单提示后手动context | 1 | 21 | 16.713 |
| 五提示后手动context | 1 | 21 | 15.445 |

1ms异步ticker取得68个样本，最大额外调度延迟2.912ms。单次观察依赖机器／缓存，未做墙钟性能断言，不能外推吞吐。事件为synthetic hints，手动刷新不是自动合并测量，也不测真实commit-owner发布或SSE网络传输；200ms合并与五组行情零quote/context由独立mock transport store行为测试验证。暖versions仍有16条SQL；API聚合不等于单条SQL。

## 最终整段审阅与定向修复

最终medium审阅的唯一Important为未知估值／初始门槛可能显示健康，已修复共享risk_status传播。无债务保证金豁免和纯减仓路径保持原有规则。原Nullable仓位及初始阈值边界用例新增可见风险状态断言，先复现两项失败，修复后：

- `npm run test:unit -- src/utils/__tests__/tradingRisk.spec.ts src/utils/__tests__/fxPresentation.spec.ts src/utils/__tests__/fxSimulation.spec.ts`：3 files／45 passed／690ms，exit0。
- `npm run type-check`、`npx eslint src/utils/tradingRisk.ts src/utils/__tests__/tradingRisk.spec.ts`：exit0。

单次medium定向复审PASS，无新增Critical／Important。最后完整frontend242项及build20.39s在该纯helper修复之前，最后定向检查与type-check在其之后；没有把旧全量检查或build冒充修复后重跑。后端未变化，既有最终后端结果继续适用。

五项Minor最终均评为不阻塞发布：规则冷缓存额外SQL、无刷新嵌套rollback可能丢提示、排队二次提交证据缺口、无关利息导致回补保守unknown、已捕获融资确认的显示可改善。缺口不是已通过场景；不保证离线通知文案、未进行浏览器交互／真实成交验收。

用户在最终阶段明确授权提交推送并触发现有自动部署。现有`.github/workflows/ci.yml`在push main后构建并部署；不修改工作流。原主工作区106项用户文件／截图哈希核对不变，本任务提交只来自隔离工作区。
