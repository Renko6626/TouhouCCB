# 4Hz基线下ASGI中间件对照

用户批准三组小对照：A当前4Hz，B只将访问策略层改纯ASGI，C访问策略及风控记录两层都改。100用户、200条SSE、合计20次流程/秒、40秒测量及原10秒预热，各一轮。

复用output/sse-cadence-ab-20261023/driver.py与sampler.py，variant固定current；同机本地PostgreSQL、Unix socket，每组新建fxcapacity_*_test数据库，事务持久化开启。保留当前风险记录、4Hz节拍、SQL、两个应用请求/读取池及独立SSE池。只改变中间件转发实现，不启用旧asgi_cadence8或split_reads原型。

按相邻时段A/B/C顺序运行，单轮结果用于方向性判断，不推断最大吞吐。检查POST及完整流程延迟、窗口吞吐、CPU/SQL/客户端迟滞/SSE/队列、连接与资金/事件完整性。公共visibility仍仅首观察者，不证明私人通知逐条必达。

代码在隔离worktree评估，原主工作区保持65897eb。试验结束保留报告、候选提交及审阅证据；不部署生产。
