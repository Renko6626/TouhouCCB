# NetTrade 锁后账本复用

起点 `34044fd`，分支 `perf/writer-locked-reuse-20261018`。仅实施用户批准的 A1+A2。

实现提交：`5cf0ee3 perf: reuse locked NetTrade account rows for risk discovery`，仅包含两个生产文件和直接相关的已有测试文件。提交后 `git status --short` 为空；本报告位于既有忽略目录，留在共享工作树供交付审阅。

## 改动与自审

- `backend/app/services/credit/risk.py` 给 `discover_dependencies` 增加显式 `locked_user`、`locked_short_rows`。只有传入对应输入才跳过该次重复读；提供输入要求 `account_locked=True`。默认入口（包括只有该标志而没有输入）仍刷新权威 User、查询全部本人外币义务。
- `backend/app/services/fx/net_trading.py` 在原来的 discover 位置传入本事务刚锁后的 User 和全部 short rows。空列表与未提供输入用 `is None` 区分；从全部 rows 按 `principal>0 OR interest>0` 构造持久欠币快照，保留其他 pair 和仅利息义务。
- `backend/tests/test_credit_risk.py` 复用已有完整抵押集合、旧 User 外部冻结用例，增加锁后输入分支；新增一个真实 DB 行为用例证明其他 pair 的仅利息义务会拒绝无资金增险，归零 short 不进入义务集合。

自审确认没有改 Pair/User/资金行锁、populate_existing、autoflush、利息 T、版本、幂等、flush、借款与成交事务、审计或历史。其他本人持仓/钱包/当前规则的 SQL 和一次已提交价格捕获的位置保留。没有跨请求缓存、额外状态、旧 BorrowBuy 优化或 UNION 改写。

## 实际验证

使用项目已有 venv `/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python`，工作目录为该工作树的 `backend/`。数据库明确为一次性 SQLite；没有 PostgreSQL、监听式 HTTP 服务、浏览器或负载测试。

集中命令（临时 observer 只记录 SQL，不进入产品或永久测试）：

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-writer-locked-reuse-20261018.sqlite \
PYTHONPATH=/tmp:/tmp/sunyunbo/thccb-single-writer-20261018/backend \
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest \
  -p thccb_locked_reuse_observer_20261018 \
  tests/test_credit_risk.py tests/test_fx_net_trading.py \
  tests/test_fx_short_trading.py tests/test_fx_borrow_buy.py
```

结果：**118 passed，1 failed，244.41s**。唯一失败是新建的仅利息 fixture 没有填 `interest_last_accrued_at`，触发现有 `ck_fx_short_position_state` 约束；补齐为 `NOW` 后只重跑该用例，生产代码未作额外调整。

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-writer-locked-reuse-20261018.sqlite \
PYTHONPATH=/tmp/sunyunbo/thccb-single-writer-20261018/backend \
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest \
  tests/test_credit_risk.py::test_locked_discovery_keeps_other_pair_interest_and_ignores_closed_short
```

结果：**1 passed，9.72s，退出0**。没有重复执行整组。`git diff --check` 退出0。

本组覆盖了净交易跨零、融资仅供新多头、第二腿失败回滚、统一结息 T 与 audit 回放、经济版本/幂等、完整抵押集合、默认入口旧现金/冻结刷新，以及公共 short/旧融资入口的原有资金行为。已有 ASGI 用例在测试进程内部执行，没有启动网络服务。SQLite 的缺少 advisory lock 提示是既有预期限制。

## 临时 SQL 观测

插件：`/tmp/thccb_locked_reuse_observer_20261018.py`；观测原始记录：`/tmp/thccb-locked-reuse-observation-20261018.json`。它对真实 `discover_dependencies` 调用范围内的 SELECT 进行记录，不加固定永久 SQL 数断言。

同一已有完整抵押集合用例：

|入口|short输入|实际SELECT|
|---|---|---:|
|默认权威发现|未提供|5|
|显式锁后复用|已锁空列表|3|

默认5条是 User、Position/Outcome关联、FxWallet、FxShortPosition、SiteConfig；复用3条仍是 Position/Outcome关联、FxWallet、SiteConfig，恰好减少 User重读及short列读。NetTrade实际用例中21次复用调用（short行数0/1/2，分别12/8/1次）均记录3条SELECT。未把拿锁本身省掉，也未把这些数值解释成数据库执行时间或吞吐改善。

测试日志：`/tmp/thccb-writer-locked-reuse-tests-20261018.log`、`/tmp/thccb-writer-locked-reuse-rerun-20261018.log`。

## 未验证项

按用户要求暂缓 PostgreSQL 真锁并发、HTTP 服务实测及压力 A/B；未跑全仓测试。没有延迟或吞吐提升结论。显式复用输入的约定仍是同session、同User、当前持锁事务及全部short行；本次唯一生产调用点正好满足这些前提，不引入新的锁证明或缓存机制。
