# SSE数据复用对照准备

before固定58101e1（后端与6c76757一致），after等待实现与独立审阅。沿用前轮100人200SSE相同HTTP/SSE负载、连接池和停止保护；无profiler。不改实验源每IP200策略，root生产cap10保留。两边FXwriter均存在，observe执行归后台，SQL总量不能只看HTTP归属。服务/PG/负载尚未启动；主agent拥有统一运行和清理。
