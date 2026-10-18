# SSE任务

用户要求sol审阅优化SSE，主要数据复用和批量消息构造。Profile实测100用户/200SSE，private占CPU样本26%、public约9%、writer约11%。SQLAlchemy30%、asyncio26%、AnyIO9%是重叠另一视角。数学_simulate0.2ms，check_new_risk0.15ms。先等价复用，不能改变消息协议或丢弃/合并/降频。

所有权：backend/app/services/trading_prices.py、account_events.py、realtime.py(若确需公共帧复用)、backend/app/api/v1/user_trading.py、fx_stream.py和直接相关已有tests；其他文件必要时先报告。每个新helper只收敛编码/批量offer，不新增队列/缓存失效/状态机/持久化。先核查现有共享model_dump与公共sse_pack实际位置，避免重复造机制。允许同一已发布行情同一订阅子集只构造/编码一次、复用不可变wire值、批量offer到既有队列。保留全部事件及同连接FIFO，不改变同步初始化/15秒检查/认证/过期/断连/队列64满关闭；account_changed和fx_order_result通知继续正确，精确公共historycursor不放宽。不能动writer/risk/funds。新永久测试只对应复用/事件顺序等具体风险，复用已有夹具。

先写简短preflight说明等价方案和受影响文件，再在本授权局部范围实施/检查/自审/提交。新的重要架构决策先报主agent，不能自行实施。禁止独立启动PG/HTTP/负载或子reviewer；控制器稍后统一性能对照。仅项目venv运行相关已有测试，SQLite明确临时路径，留具体命令/结果/失败修正证据。报告至sse-report.md。你不独占代码库，不回滚他人修改。
