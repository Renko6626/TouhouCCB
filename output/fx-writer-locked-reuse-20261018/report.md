# NetTrade锁后账户与空仓复用

已完成用户批准的A1+A2，固定提交5cf0ee30ae7c364d456307828a6bae3c3982565f，独立sol审阅批准，无Critical/Important/Minor；已合入主工作区。

NetTrade把本事务刚锁定并刷新过的User和全部short rows交给discover_dependencies，省掉重复账户刷新和short列读。其他调用者默认权威刷新保持，空列表表示已知无行，其他pair及仅利息义务仍计入。完整其他持仓/钱包/当前规则查询保留；没有改锁、资金、利息T、幂等、版本、flush、行情捕获/发布时序，也未实施旧BorrowBuy或其他方案。

临时真实SQL观测：默认依赖准备5条SELECT，显式锁后复用3条；真实NetTrade用例21次调用（short行数0/1/2）均为3条。普通无债现金路径本来跳过完整发现，没有该项收益；未把减少查询转换成延迟或吞吐承诺。

隔离树4文件集中119项：118通过；新增fixture缺少计息时间触发现有schema约束，补齐后仅该项定向重跑通过。不是最终整组命令一次全绿；命令、日志、SQL观察和边界见[实施报告](validation/report.md)及[独立审阅](validation/review.md)。

主工作区3文件与审阅提交逐字一致，333个未涉及后端源文件hash保持，git apply --check先通过再应用，有原文件备份；git diff --check退出0。主工作区以显式一次性SQLite运行 `venv/bin/python -m pytest -q tests/test_credit_risk.py::test_locked_discovery_keeps_other_pair_interest_and_ignores_closed_short`，1 passed in 2.45s。

按用户要求没有PG真锁并发验证、HTTP监听服务或压力测试；未推送或部署。分支perf/writer-locked-reuse-20261018与隔离工作树保留。
