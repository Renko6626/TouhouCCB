# FX 蜡烛统一 LMSR 口径实施计划

> **For agentic workers:** 使用 superpowers:executing-plans 按任务执行，完成后独立只读审查。

**Goal:** FX 的单笔成交蜡烛显示交易前到交易后的变化，历史与实时使用相同 OHLC 定义。

**Architecture:** 沿用现有 FxTrade、蜡烛聚合、runtime ring、SSE 和前端引擎。通过已有 pre_gold_reserve / pre_foreign_reserve 推导交易前边际价，按 post_price 的六位精度取值；新增公开 pre_price，不公开储备。一次数据迁移仅失效 fx_candle 和 fx_market_data_state，部署停 writer 后迁移，启动 owner 使用已有全量回填生成新版本。

**Tech Stack:** 现有 Python / SQLAlchemy / Alembic、Vue / TypeScript / Vitest，无新增依赖。

**Spec:** 用户已确认完整对齐 LMSR：O=首笔交易前池价，C=末笔交易后池价，H/L 包含所有交易前后池价；成交量不变。真实非交易池价变化允许跳价，不以任意前收盘覆盖真实开盘价。

## Global Constraints

- 不新增数据库字段，不变更金额、借款、钱包、原始成交或审计记录。
- 既有覆盖游标、重复成交跳过、乱序回补及段版本失效规则保留。
- 页面验证仅截图，不自动点击、填写、拖动或实际交易。
- 遵循用户测试约定，复用已有测试，只补本次真实行为和迁移数据保留缺口。
- 用户现有四份文档改动不纳入提交；已授权完成后提交推送及自动部署。

## Review Focus

- 单笔涨/跌交易的开盘、高低价；跨桶首尾在无额外池变更时连接。
- 同时间不同 id、跨批乱序、forming 空桶收到首笔交易。
- 新公开价格字段有效性及隐私白名单，旧服务缺字段时不伪造交易前价。
- 派生数据失效同时归零游标、轮换版本；升级/回滚不改金融源数据。
- 启动回填、HTTP 尾段及 SSE 使用相同价格精度。

### Task 1: 历史与实时后端统一价格来源

**Files:** backend/app/services/fx/{candles,market_data,market_state,market_reads}.py、backend/scripts/backfill_fx_candles.py；已有 test_fx_candle_storage.py、test_fx_market_data.py、test_fx_candle_pipeline.py、test_fx_stream.py。

**Interfaces:** trade_pre_price(trade) -> Decimal（由原储备计算并量化到六位）；公开增量加入 pre_price 十进制字符串。O=最早 pre_price，C=最晚 post_price，H/L=所有 pre/post 极值，金侧量原规则。

- [x] 在已有测试中用真实不同 pre/post 值证明单笔/跨桶/乱序和公开帧，运行并确认预期失败。
- [x] 实现共享价格推导，补齐 runtime、HTTP 尾段和离线重建数据库投影，统一聚合与公开字段白名单。
- [x] 运行相关已有后端测试，保持金侧量和游标断言。

### Task 2: 前端增量与历史一致

**Files:** thccb-frontend/src/{types/fx.ts,utils/fxHistory.ts,utils/fxCandle.ts}；已有 fxCandle.spec.ts、fxHistory.spec.ts、fxStream.spec.ts。

**Interfaces:** FxTradeTick.pre_price -> FxCandleTrade.prePrice，前价有限且非负（六位量化后可为零），后价保持正且有限；缺失或非法成交前价不进入引擎。原 post_price / price 为收盘来源。

- [x] 调整现有行为用例，先验证跨桶10→11、空桶首笔、乱序前价和译码失败。
- [x] 实现前价校验和桶内 O/H/L；保留去重、preload 时序不明重载及 MA。
- [x] 运行全前端单测、类型检查、局部 ESLint 和构建。

### Task 3: 派生历史失效与发布

**Files:** 新 Alembic 数据迁移（接 fx_borrow_buy_20261010）、已有 test_fx_migration.py。

**Interfaces:** upgrade/downgrade 均仅删除派生 candle 和 checkpoint，由对应版本启动代码按交易账本回填。复用现有部署的停 writer、备份恢复验证、迁移和 startup 生命周期，不改部署方式。

- [x] 测试升级/回滚实际删除两份派生数据并保持原始交易、钱包、现金记录和 schema；新增迁移前用例失败。
- [x] 实现迁移，运行后端相关检查和完整套件，运行隔离 FX 端到端测试。
- [x] 只读截图看涨跌蜡烛、均价线及同步增高布局；独立审查。
- [ ] 仅提交本任务变更，合入 main 推送，确认 CI/CD 和远程健康检查。

## 执行记录

- 已确认现有部署先停止 backend writer，再备份及恢复验证、运行 Alembic，随后启动 owner；缺 checkpoint 时 startup 分页回填全部 FxTrade 并生成新历史版本，完成前不接受请求。
- 不使用 FX_REBUILD_PENDING / fx-rebuild 流程，该流程会重开业务数据，与本次派生历史重建无关。

- 回归测试先确认后端旧定义 4 项失败、SSE 新字段 2 项失败、前端旧定义 5 项失败；迁移文件缺失用例先失败。实现后相关后端 99 passed、全前端 185 passed，隔离 FX 端到端 14 passed；类型检查、局部 ESLint、构建、Python 编译和 diff 检查通过，构建保留既有 chunk 提示。
- output/fx-candle-lmsr 的桌面做多、回补及手机截图由真实 compute_fx_candle_rows 输出的 160 根模拟交易蜡烛供给，所有相邻 O/C 连接。无页面运行时异常、横向溢出或成交写请求，未自动点击/填表/实际交易。

- 完整后端检查：timeout 300 env -u DATABASE_URL venv/bin/python -m pytest -q，1408 passed、1 skipped、34 deselected，exit 0，保留既有 SQLite 连接回收警告。隔离端到端命令 timeout 180 env -u DATABASE_URL venv/bin/python -m pytest -q --noconftest tests/test_fx_end_to_end.py，14 passed。
- 独立审查发现合法极低前价可能六位量化为零，被前端拒绝；新增真实历史译码/引擎用例先失败，修正后全前端 186 passed。仅前价与历史 OHLC 接受非负值，成交后价仍保持原正值校验，负值和缺失前价继续拒绝。
- 显式固定前价 ROUND_HALF_EVEN。已知限制：历史 spot 的 PostgreSQL Numeric(16,6) 半值舍入与已有 short 显式舍入不同，极端半值可产生 0.000001 的末位差；本轮保持原始 post_price，不调整金融源数据或伪造前收盘。审查追加确认无必要修复问题。
- 最后相关后端 99 passed、前端类型检查及局部 ESLint 通过；末轮构建沿用既有 chunk 提示。页面截图未自动操作按钮、填表或执行交易。
