# writer只读SQL审计

只读分析用户要求：writer核心风控→买入的SQL优化方案，绝不改产品代码/测试/提交。唯一产物writer-sql-audit.md。基于58101e1(backend6c76757)，同市场唯一writer+User锁+freshownledger+固定跨市场已发布快照已确认。Profile目录主root/output/fx-writer-profile-20261018，report/analysis/raw均可读：stressNetTrade平均17.6SQL、SQLcursor69ms、handler128ms(profile扰动)，discover_dependencies21ms，_simulate0.2ms。无profilehandler88ms，别把SQLelapsed当server执行/纯锁wait。

追踪普通现金买入、显式融资、已有负债/持仓、shortcrosszero相关公共路径，说明真实SQL顺序、每条读写对象/用途、行锁与flush/commit边界、参数/状态是否已有且可复用。分开已证实重复、可在同一锁后事务内复用、需要新内存状态/改变语义需用户批准、必须保留。具体源码行号和rawSQL证据，给最小方案、收益上限/不确定性及验证范围，不预先宣称等价。特别populate_existing、autoflush、利息结算时间T、loan+trade同事务、User版本/幂等/资金/历史必须保持，不能用入队前查询替代执行授权。别扩为全系统审计或建议删锁/风控。禁止启动服务/PG/负载/CPU测试，无新subagent。你与SSEworker共享工作树，不动其文件；报告交主agentreview。
