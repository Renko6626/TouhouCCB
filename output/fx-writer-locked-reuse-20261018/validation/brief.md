# 已批准局部优化

用户明确批准迅速实现writer只读方案A1+A2：NetTrade同事务锁后的User与全部short rows传给discover_dependencies复用，省重复账户刷新和short列读。仅限此路径；通用默认入口继续刷新权威User/查short，真实其他持仓/钱包/规则查询与已提交价格捕获时点均保持。空列表表示已锁而无行，不能当未提供。筛选principal>0 OR interest>0，保留其他pair债务和只剩利息。不得复用排队前对象/跨请求缓存，不改锁/资金/利息T/幂等/版本/flush/history，不改旧BorrowBuy或UNION等后续方案。

起点34044fd，分支perf/writer-locked-reuse-20261018。所有权risk.py/net_trading.py及最直接相关已有tests。项目AGENTS优先，测试简短真实行为，不机械新增大量用例或固定永久SQL总数断言。不启动PG/HTTP/压力测试（用户明确暂缓）；项目venv，明确一次性SQLite。先使用之前审计报告已有证据，简明实现/自审/验证/提交，report.md留具体命令结果和限制。你不独占代码库，不覆盖回滚他人改动。遇到超出A1+A2的新架构决策报主agent。
