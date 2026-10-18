# 独立审阅

结论：**批准**。审阅范围为 `34044fd..5cf0ee30ae7c364d456307828a6bae3c3982565f` 的三个文件；符合已批准的 A1+A2。Critical：0；Important：0；Minor：0。

## 核对证据

- 唯一新增生产复用调用位于 `backend/app/services/fx/net_trading.py:290`。它仍先锁 Pair、再经 `lock_user` 锁 User、核对版本、锁钱包、调用 `_lock_short_rows(db, user_id)`，随后直接传入当前 User 和全部 short rows。`lock_user` 的 `FOR UPDATE + populate_existing=True` 与 `_SHORT_ROWS_LOCK` 的本人全量查询、排序、`FOR UPDATE + populate_existing=True` 均保留；中间没有事务提交或账本修改。复用满足当前事务、当前账户、完整集合的输入约定。
- `backend/app/services/credit/risk.py:523` 只有显式提供 User 才省账户刷新；`:536` 使用 `locked_short_rows is None` 区分未提供与已锁空列表。未提供仍走原来的权威 User 刷新和 short 列查询，包括仅传 `account_locked=True` 的调用。全仓搜索新增参数只找到上述生产调用及相关测试。
- 复用分支的 `principal_foreign > 0 or interest_foreign > 0` 与原 SQL 条件一致，不按当前 pair 缩减集合。仅利息债务保留、已归零 short 排除；本金、利息、结息时间均进入原来的 `ShortDebtSnapshot`。其他持仓、钱包、实时规则读取和最后一次同步捕获已提交价格的位置保留。
- 三文件 diff 未改锁、autoflush、利息 T、资金写入、经济版本、幂等、flush、历史或旧 BorrowBuy；没有缓存、UNION 改写或新增恢复机制。显式输入的同 session、同账户及当前锁事务要求由现有唯一调用点满足，无需为本次局部优化增加锁证明机制。
- 修改的已有完整抵押测试分别走默认发现及真实锁后复用，覆盖空 short 集合；已有同 session 旧 User 外部冻结测试增加真实锁刷新分支，检查权威版本和实际拒绝结果。新增用例通过真实持久行、真实发现及 `check_new_risk`，检查另一 pair 的仅利息义务导致无资金增险被拒绝，同时排除关闭行。没有以源码文本或永久 SQL 总数断言代替行为。

## 验证证据与限制

本轮按要求只读审阅，未重跑测试、启动 PostgreSQL/HTTP 或压测。采用 `report.md` 已有证据：相关四个测试文件集中运行 118 passed、1 fixture 约束失败；补齐新 fixture 的结息时间后该单项 1 passed，生产代码未变；`git diff --check` 退出 0。临时真实 SQL 记录显示同一发现范围由 5 次 SELECT 降至 3 次，减少的正是重复 User 和 short 读取；该观测未固化为永久测试。

SQLite 证据不证明 PostgreSQL 真锁并发、HTTP 延迟或吞吐提升；这些均按本次已批准范围暂缓，不构成本轮阻塞项。
