# FX统一多空实施验证（2026-10-11）

本次在隔离工作树 `/data/sunyunbo/.codex/worktrees/2781/TouhouCCB`、基线 `52e65a44c138742cdfeeba63b3d463a220a369b1` 实施。用户已批准统一API、输入单位、显式融资和最小成功回执。2026-10-11实施验收时尚未提交或推送，未执行生产迁移或部署；限价单未实现。

## 实际检查

以下后端命令均在工作树的 `backend` 目录执行。SQLite库都是本任务新建的 `/tmp` 文件；旧记录兼容由迁移行为测试证明，没有清空业务数据。

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/fx-net-baseline-impl-20261011.db venv/bin/python -m pytest -q tests/test_fx_amm.py tests/test_fx_trading_in_session.py tests/test_fx_short_trading.py
```

基线：89 passed，11.83s。

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/fx-net-task1-impl-20261011.db venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_amm.py tests/test_fx_short_trading.py
```

报价阶段：68 passed，9.61s。包含真实只读分类、非零费用、已有外币利息、预算零前后和异常双方向阻塞。

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/fx-net-review-red-20261011.db venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_migration.py
DATABASE_URL=sqlite+aiosqlite:////tmp/fx-net-review-green-20261011.db venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_migration.py
```

审阅回归先出现3 failed / 27 passed：融资中间信用入账越界、前段手续费可用于后段真实借币库存、NULL关联腿约束。修复后30 passed，5.53s。池价格变化将原纯回补变为新增多头的场景也在该组验证：请求明确保存 allow_growth，未允许增长时409重报，账户版本不变也不会静默开仓。

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/fx-net-final-backend-20261011b.db venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_amm.py tests/test_fx_trading_in_session.py tests/test_fx_borrow_buy.py tests/test_fx_short_trading.py tests/test_fx_liquidation_service.py tests/test_credit_risk.py tests/test_fx_migration.py tests/test_fx_api.py tests/test_fx_audit_replay.py tests/test_fx_short_audit_replay.py tests/test_fx_candle_pipeline.py tests/test_fx_market_data_integration.py
```

最终后端：215 passed，32.66s。新net测试23项，迁移及已有检查复用既有夹具与生产AMM/风险/账务。真实第二腿已经flush后注入故障的场景，断言第一腿、还款、用户、钱包、池子、treasury、成交、审计、回执全部回滚；不是只检查预检拒绝。

```bash
TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55541/thccb_fx_net_test DATABASE_URL=sqlite+aiosqlite:////tmp/fx-net-final-pg-bootstrap-20261011.db venv/bin/python -m pytest -q -m pg tests/pg/test_pg_fx_short_concurrency.py
```

最终真实PostgreSQL：14 passed，11.38s，未skip。使用专用临时集群 `/tmp/sunyunbo/thccb-fx-net-pg-20261011/data` 的一次性测试库。覆盖同编号同时跨零只保存一份回执/两腿、不同编号抢同仓位拒绝旧版本后重新报价分类，以及旧现货入口与net入口争用编号；保留原冻结、锁金、贷款及门闩场景。

前端命令均在工作树的 `thccb-frontend` 目录执行，未安装或改动共享依赖：

```bash
npm run test:unit
npm run type-check
npx eslint src/api/fx.ts src/api/fxNetTrading.ts src/composables/useFxNetQuote.ts src/pages/Fx.vue src/types/fx.ts src/utils/fxPresentation.ts src/api/__tests__/fxBorrowBuy.spec.ts src/composables/__tests__/useFxShortQuote.spec.ts src/utils/__tests__/fxPresentation.spec.ts
npm run build
```

最终：16 files / 193 unit tests passed（911ms）；type-check和只读ESLint exit 0；build exit 0，19.35s。构建仍有已有的empty charts chunk、auth静态/动态导入和大chunk提示。请求恢复测试复用于旧融资与net身份：未知结果重挂载保留编号、账号切换隔离、存储先于HTTP、写不进存储不发送、原结果未确认阻止第二笔。报价测试验证350ms、防止旧响应覆盖新账号及行情参考价变化不发新的组合请求；展示测试验证真实腿分类和私人分组。

工作树根目录 `git diff --check` exit 0。未执行浏览器自动点击、填表或交易交互；本次也未做截图。构建和unit是代码检查，不代表完整浏览器产品验收。

## 小范围运行成本对照

脚本仅在 `/tmp/fx-net-perf-20261011.py`，输出 `/tmp/fx-net-perf-20261011.json`。命令在backend目录：

```bash
PYTHONPATH=. DATABASE_URL=sqlite+aiosqlite:////tmp/fx-net-perf-bootstrap-20261011.db venv/bin/python /tmp/fx-net-perf-20261011.py > /tmp/fx-net-perf-20261011.json
```

只操作临时PG的 `thccb_fx_net_perf` 库；每个场景恢复同一小账户/池子的初始状态。SQL计数由SQLAlchemy执行事件观测；风险与发现调用计数包装实际生产函数；门闩入口断言等待前session不处于事务。争用场景由另一个任务持有目标门闩25ms。

| 场景 | SQL | 最终风控次数 | 完整发现次数 | gate进入次数 | gate等待ms | 总耗时ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 旧现金买入 | 15 | 0 | 0 | 1 | 0.041 | 48.041 |
| net现金买入 | 17 | 0 | 0 | 1 | 0.065 | 32.620 |
| net现货减仓 | 17 | 0 | 0 | 1 | 0.076 | 53.542 |
| net平多后开空 | 46 | 1 | 2 | 2 | 0.177 | 142.362 |
| 同pair争用下跨零 | 46 | 1 | 2 | 2 | 25.204 | 83.905 |

跨零先在目标门闩下发现需要信用依赖，完整回滚释放后做锁外发现，再取得完整有序门闩；第二次发现为锁内权威刷新，不是两次整套风控/估值。普通无债现金路径没有完整发现与保证金估值。最终交易通知是commit后的同步轻提示，发布也在门闩释放后排队，没有锁内同步广播。

每个场景只测一次，受冷启动和本机调度影响；耗时不能用作容量、吞吐或net比旧入口更快的结论。计数用于验证本次增加的请求回执/编排成本及无债快路径。

## 边界与未验证项

- 生产存量双方向异常未预检，未读取生产库；统一入口对该异常明确阻塞。上线前仍需独立盘点与处理决定。
- Alembic升级/降级行为在可丢弃SQLite库验证；PG竞争库使用ORM metadata创建schema，没有执行生产迁移或部署演练。
- UI未做浏览器交互或截图。成功回执在刷新前确认，刷新失败提示“已成交、账户更新失败”；实际浏览器网络中断/恢复流程未手动验收。
- 私人历史边界已修复：先按真实腿limit读取，必要时补齐同用户同pair的边界execution兄弟腿，正常最多多返回一条；旧单腿limit行为不变。
- 限价单、预留资金、扫描scheduler、上线迁移及部署均留待后续授权。


## 最终整段审阅后的集中修复

末轮F1–F6已在同一fix wave实施，没有引入新架构、依赖或验证工程：

- F1快捷全卖填数量/closeOnly后只定位面板，不再次setSide清意图；快平仓生成的net请求allow_growth始终false。现有展示工具测试直接覆盖重新报价出现增长时仍不能授权。
- F2资金来源按当前pair含息仓位的本仓锁金和自由现金分配，其他仓锁金不参与；只在报价有新增多头且回补资金已足够时显示新增贷款确认。现金150、自由50、本仓锁100、欠币80、预算200，生产展示函数返回自由现金50、本仓锁100、需新贷款50；纯回补80返回50+30且不提供贷款。贷款请求金额由不依赖报价失效状态的资金计算取得，避免报价清空/回填造成请求循环。
- F3现货卖出模拟也校验首次flush前的cash+全部所得，再模拟还债；真实quote/execute在任何写入前明确拒绝越界，用户/贷款/成交/审计不变。
- F4移动端普通按钮明确买入/卖出，额外平仓按钮按当前真实多头/空头调用快捷全卖/全回补。订单标题按真实legs；1%示意只按新增多头/空头段，纯回补不显示多头敞口。
- F5私人API稳定排序并补齐边界执行的同User/pair兄弟腿；真实两腿卖140后加49条较新旧单腿记录，limit50返回51条，跨零组仍包含100+40，旧单腿limit49仍返回49条，其他用户记录未泄漏。
- F6旧short helper账户变化后的迟到成功返回null；页面成功/失败/finally使用账号generation，所有账号变更（含logout）失效在途short读取。统一pendingRisk含旧short.hasUnresolved；401/403/408/429保留原编号，损坏的已存短单也阻止新写入。

本轮确切检查，在backend目录：

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/fx-net-final-fix-backend-20261011b.db venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_api.py tests/test_fx_history.py
```

48 passed，7.82s（包含25项net行为检查）。初次相关检查47过/1个新边界用例因SQLite大Decimal存储roundtrip尾差断言失败；资金预检已正确拒绝。改为比较真实种子状态与拒绝后完整用户状态，保留明确拒绝及零ledger/成交/审计断言。

本轮前端，在frontend目录：

```bash
npm run test:unit
npm run type-check
npx eslint src/api/fx.ts src/pages/Fx.vue src/utils/fxPresentation.ts src/utils/__tests__/fx.spec.ts src/utils/__tests__/fxPresentation.spec.ts
npm run build
```

16 files /201 unit passed，887ms；type-check、只读ESLint exit0，build exit0 /19.45s，已有构建提示仍保留。末轮新增资金分配/增长方向/旧short账户与保留身份检查复用已有测试文件；未新增浏览器交互或页面测试工程。

`git diff --check` exit0。先前215项后端/14项PG以及成本测量是对应阶段的实际证据，本轮未重复全套、PG竞争或性能采样：改动集中在net预写数值保护、私人读取边界及前端局部行为，锁/事务/回执结构未改变。页面快捷入口调用链、移动按钮定位和账号读取失效由代码审阅核对，工具/数据行为有unit/数据库用例；未冒称已做浏览器产品验收。

## 完成状态

最终定向复查通过：首次整段审阅的六项问题及旧short恢复/账户隔离边界均已处理，未发现修复引入的重要新问题。主agent核对审阅报告与最终diff检查；任务在上述验证范围内完成。

主agent已关闭自行创建的临时PostgreSQL集群，数据与验证产物保留。代码保留在隔离工作树 `/data/sunyunbo/.codex/worktrees/2781/TouhouCCB`，2026-10-11验收时尚未提交、合并、推送或部署。生产存量预检、生产迁移及浏览器验收仍未执行；限价单后置。

## 阶段性提交（2026-10-12）

用户已授权阶段性提交并推送。此阶段使用独立分支 `feat/fx-net-trading`，包含实现、相关测试及计划/设计/验证记录；主工作区原有改动保持原样。未授权合并或部署。本阶段只整理提交，没有新的产品代码修改，沿用上面的实际验证证据；提交前重新检查diff。
