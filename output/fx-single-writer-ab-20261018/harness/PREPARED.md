# FX 唯一 writer 最终 A/B 准备记录

当前状态：最终源码与观测脚本已就绪，等待主 agent 单独发出 START。本记录尚无性能结果。准备期间没有连接 PostgreSQL、启动服务或执行负载，也没有修改产品代码、主工作树或旧实验目录。PostgreSQL 生命周期由主 agent 独占管理。

## 固定来源与隔离范围

- before：`330f4e683ec5564dea6ea59983351c3c8e128d9d`。
- after：`6c7675722cda265bd205dced6ce0f939ae7ac769`，已按明确 FINALREF 授权通过 `git archive COMMIT backend` 提取。
- 最终 writer SHA256：`140a786908a8d7a5e5a25a3013e4004ba2b16de95d6800d00afa4ef4aa2e84c0`。与已复核候选 `b413408` 完全相同；GATE 移除、删除状态及异常类型扩展不改变观测边界。
- runtime：`/tmp/sunyunbo/fx-single-writer-ab-20261018`。
- output：`/data/sunyunbo/www/TouhouCCB/output/fx-single-writer-ab-20261018`。
- 一次性库：`fx_single_writer_before_test`、`fx_single_writer_after_test`。
- 隔离 PG14：socket `/tmp/sunyunbo/fx-pool-incident-20261017/socket`，port `55489`。实验不停止 PG，也不接触生产数据。

`before-source.json`、`after-source.json` 保存全部归档文件及初始化 helper 的 SHA256；两份源码逐文件哈希复核通过。`source.diff` 保存归档差异。`harness-manifest.json` 保存既有 harness 来源哈希、新 harness 镜像哈希及观测参考提交。`harness/` 是可复用脚本镜像。before 的 369 个 backend 文件与上一轮相同基线逐字节一致。

## 完全沿用的负载和保护

HTTP driver `fx_http.py`、readheavy 扩展 `compare_driver.py`、本地资源 sampler `local_server.py` 与上一轮 request-snapshot 实验逐字节一致。保留 seed `20261017`、100 用户、公共/私人 SSE 各 100、cash/finance/short 角色 60/20/20、连接池 10+20、单 Uvicorn `127.0.0.1:18543`。

| 阶段 | 原定负载 |
|---|---|
| smoke / join / role preparation | 原 driver 时序；smoke 10 秒，join 间隔 0.3 秒；准备写入单独记录 |
| readheavy | 60 秒，1200 次固定读取与 30 个定时真实 quote/trade cycle，固定读取并发上限 10 |
| readonly | 30 秒 |
| normal | 60 秒，沿用原活动用户与间隔映射 |
| active | 45 秒，沿用原活动用户、历史请求与间隔映射 |
| stress | 30 秒，名义合计约 25 cycles/s |
| burst | 原突发下单及 10 秒观察窗口 |

**25 cycles/s 是客户端名义循环负载，不能写成成功交易/s。** 成功吞吐必须取实际成功成交数除以明确阶段时长；取消、拒绝、超时和错误分别保留。实际命令、HTTP 请求结果、循环结果、SSE 帧/字节和 freshness、各阶段保护触发点、排空边界及未执行阶段均保留原始数据。原 30 秒窗口错误率 >5%、成功交易 P99 >2000ms、已准入 SSE 断开等停止保护保持不变。没有新增工作负载、profiler 或压测平台。

baseline 与最终 after 的 `realtime.py` 都是每 `(topic, ip)` `MAX_PER_IP = 200`，允许本机同一 loopback IP 的 100 用户/200 SSE 场景。主工作树另有无关生产限制 10，整合时应保持 10。本实验没有修改生产 IP 策略，也不证明生产环境同 IP 可接入 100 用户。

## 观测口径与局限

`count_server.py` 对两个版本使用相同 SQL cursor、HTTP、pool、eventloop、资源观测。`credit.gates` 为可选导入；移除时写明 `absent_in_source`，GATE 分布为空、队列 holder/waiter 为 null，不能当作实测零耗时。

`lock-query-elapsed.json` 分开记录 User、FX pair 和其他 `FOR UPDATE` 查询的 cursor 执行耗时及错误结果。这包含 PostgreSQL 执行、可能的锁等待、传输和调度，**不是纯锁等待时间**。PG 原有 0.5 秒采样查询仅增加 query-kind 分类表达式，单独统计 `wait_event_type=Lock` 与 blocking PID 样本，不增加连接或轮询查询。实际观测到的 Lock 等待状态与查询总耗时分别报告，不能由采样推导精确逐查询锁等待时长。

FX actor observer 挂接 `_bootstrap`、`_consume` 及每个 actor 的 queue `put_nowait/get`，不依赖固定命令类型、不包装具体金融操作、不改命令/future/事务。已验证 `_bootstrap` 创建 queue 和 consumer task 后没有新的 await，因此 hooks 在新 consumer 首次运行前安装。

- 队列等待：成功接受 enqueue 的时间戳至 consumer dequeue 返回，包含调度延迟。
- 执行耗时：dequeue 返回至下一次 get 调用或 consumer 退出，包含 handler、commit、发布、通知、future 处理及异常恢复，排除空闲等待 queue.get 的时间。不是纯数据库事务时间。
- 已完成/取消 future 的排队跳过另列，不虚构执行时间；关停/删除直接 drain 的命令没有 dequeue timing。
- 记录 pair、运行时命令名、提交 route、dequeue 所在 phase。跨阶段命令按其执行边界解释。
- consumer SQL 标记为 `background:fx_writer`；bootstrap SQL 为 `background:fx_writer_bootstrap`。SQL 已转到后台执行，不能只比较 HTTP route 的 SQL 得出总量下降。组件标签是归属标记，不是完整异步因果跟踪。
- 两个版本加载同一个 observer；before 没有 FX actor 时记录 unavailable，不能以 before=0 比较队列/执行耗时。after actor timing 是迁移后机制本身的实测。

观测额外成本为每命令少量 monotonic 读数、pending-future 字典操作、counter 和 sample append，以及每个 actor/bootstrap 生命周期的 contextvar scope。没有额外 task、await、SQL、I/O、轮询或 profiler。观测成本未做独立 benchmark、未扣除；共有观测在两边一致，after 另承担适用的 actor 观测成本。

本基准只使用一个 FX 目标市场，主要检验普通读取与执行排队的解耦；**不能据此宣称跨市场性能提升**。跨市场资金正确性由实现阶段的真实并发检查支持。保留精确历史协议，公共历史组合的 `FOR SHARE` 仍可能等待数据库；不能宣称所有 SSE 包都不等待数据库。

## 已执行的准备校验

```bash
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-single-writer-ab-20261018/prepare.py --after-commit 6c7675722cda265bd205dced6ce0f939ae7ac769
python3 /tmp/sunyunbo/fx-single-writer-ab-20261018/check_observer.py --commit 6c7675722cda265bd205dced6ce0f939ae7ac769
python3 -m py_compile /tmp/sunyunbo/fx-single-writer-ab-20261018/*.py
```

均通过。observer 检查只从 `git show` 加载 committed actor/state/reload class AST，替换外部依赖，以两条内存命令走真实 bootstrap/submit/consumer 逻辑，核对队列/执行样本数量、非负耗时和 actor context 标签。它没有导入生产应用/数据库模块，没有启动服务；这是接线验证，不能当作资金并发或性能验收。未带 `--start-authorized` 调用 runner 已按预期在任何子进程/数据库动作之前拒绝。

`instrumentation-status.json` 已标记 `after_ready: true`，`load_start_authorized: false`。尚未验证项：真实 PG 下新 SQL 分类表达式、归档后应用启动/迁移/seed 兼容性、两边实际 HTTP/SSE 和性能数据；这些仅在 START 后由既定实验执行。

## 等待 START 后的准确命令

```bash
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-single-writer-ab-20261018/run.py --start-authorized
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-single-writer-ab-20261018/analyze.py
```

runner 依次运行 before、after，建立全新实验库；若数据库或 variant 输出目录已存在则停止，不能复用或覆盖旧实验。每边初始化后执行的 driver 参数如下，最终绝对路径命令保存在各 variant 的 `actual-command.json`：

```text
python compare_driver.py --base-url http://127.0.0.1:18543 --tokens VARIANT/run/private/tokens.json --mode full --users 100 --smoke-seconds 10 --min-interval 1 --max-interval 1.5 --join-interval .3 --readonly-seconds 30 --normal-seconds 60 --active-seconds 45 --stress-seconds 30 --burst-observe-seconds 10 --orders-seconds 0 --output OUTPUT/VARIANT/http
```

结束时 runner 只停止它自己启动的 HTTP/monitor 子进程并移除临时 token，保留日志/raw 指标，PG 留给主 agent。最终中文结果报告须继续保留各阶段成功吞吐与错误分类、读取 P95/P99、writer queue/execution、查询耗时/锁采样、SQL、CPU、SSE、保护/排空边界及未执行项，不将中间候选或上一轮结果当作本轮结果。
