# 私人SSE复用交付与性能实验状态

SSE实现已完成并合入主工作区；独立sol审阅批准，无Critical/Important/Minor。固定功能提交 `d18445b8ca8e24f916a221b7b04e3798d223384d`，基线 `58101e1fc78846bb5c1dc0919374e4ba76753753`。

## 实际改动

- 同一轮pricing发布按相同affected订阅子集构造一次payload、编码一次UTF8 bytes，再放入原有各订阅队列。
- account_changed和fx_order_result在一次发布中各编码一次，各连接复用不可变wire。
- user_trading直接发送预编码wire；原事件标签仍用于account_changed前的依赖查询。
- 公共SSE已有共享bytes，本轮没有修改。没有降低频率、合并/丢弃事件或新增队列；单连接FIFO、sync首帧、15秒独立检查、认证/过期/断连、queue64满关闭和公共history游标/FOR SHARE保持。
- 产品改动仅account_events.py、trading_prices.py、user_trading.py；writer、风控、账户版本、资金事务和每IP限额均未改。

## 功能验证与集成

隔离分支：相关baseline41 passed；实现后5文件66 passed；最后局部自审后3个直接SSE文件及共享fixture业务代表23 passed。这是分批检查，不是最终全仓命令一次通过；具体命令、初始两项失败和修正记录见 [实施报告](validation/sse-report.md)。[独立审阅](validation/sse-review.md)未重复测试。

主工作区仅应用本轮8文件patch，先备份并git apply --check；应用后各文件与候选逐字一致，328个未涉及后端源文件hash未变，realtime每IP10限额保留。主控制器执行以下检查均退出0：

```bash
git diff --check
backend/venv/bin/python -m py_compile backend/app/api/v1/user_trading.py backend/app/services/account_events.py backend/app/services/trading_prices.py
```

没有在主工作区重复运行同代码功能测试；没有浏览器交互或全仓检查。原脏工作区的无关改动保留。未推送或部署。

## 性能实验：主动停止，没有有效A/B结论

原计划固定before58101e1与afterd18445b，沿用上一轮100用户/200SSE、60/20/20角色、seed20261017、pool10+20、完整阶段和停止保护，两边不启用profiler。两个代码归档、hash、harness均已保留。

before初始化、迁移与服务启动正常，但连接阶段宿主机出现明显异常负载。观察到/proc/loadavg为 `1304.81 749.49 326.58`，实际停止时为 `1308.62 786.38 347.74`；连接阶段约60秒仅接入公共/私人各23条。主控制器向本轮runner发送SIGINT，停止测试，runner退出130并执行finally清理。不是产品测试失败，也不是原压测保护触发。

**未进入有效的100人负载对照；after未运行，因此不能声称CPU、吞吐或延迟改善，也不能把本轮半截数据与上一轮混比。** 不修改保护阈值、不把主机异常当成SSE回退、不为取得数字反复重跑。待机器负载稳定时再做一次完整固定代码对照；当前数据只能证明尝试过程。

原始证据：[external-stop.json](external-stop.json)、[before driver日志](before/driver.log)、[before源码hash](before-source.json)、[after源码hash](after-source.json)、[harness镜像](harness/)、[清理记录](cleanup.json)。没有伪造summary/analysis。

临时HTTP/driver/PGmonitor已退出，18543无监听，临时token删除；控制器已fast stop隔离PG。只创建了before临时库sse_reuse_before_test，没有运行after库。未接触生产数据库。

## Writer只读方案

另一个sol只读审计已完成：[writer-sql-audit.md](writer-sql-audit.md)。推荐第一批只复用NetTrade同事务锁后的User和全部short行，完整发现时最多省两条SELECT；普通无债现金路径已跳过发现，不受益。全组合持仓、最新规则、User锁、利息时点、幂等和必要flush保留。旧BorrowBuy锁后入口是独立机会，不捆绑。本任务没有实施writer方案，也没有宣称实测收益。
