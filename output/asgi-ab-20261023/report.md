# 4Hz版本的ASGI中间件三组对照

结论：本轮支持将访问策略与风控记录两层都迁移为纯ASGI中间件。两层版本保留了功能和顺序，并在100人、合计20次流程/秒场景下显著减轻排队。用户已确认采用C，两层实现已正式合入主工作区；尚未推送或部署。

## 实测结果

每组100用户、200条SSE，10秒预热、40秒测量、单轮顺序A/B/C，同一4Hz配置。成功吞吐只计测量窗口内返回成功的POST，排空另记。

| 方案 | 成功POST/s | POST P95/P99 ms | 完整流程P99 ms | owner CPU | 采样队列峰值 | 跳过/失败流程 | 公共可见P99 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A 4Hz基线 | 15.250 | 4946/5086 | 5250 | 97.3% | 90 | 86/21 | 5369 |
| B 仅访问层ASGI | 19.300 | 627/687 | 799 | 97.0% | 12 | 0/24 | 1007 |
| C 两层ASGI | 19.475 | 136/191 | 301 | 89.3% | 2 | 0/17 | 499 |

C相比A，窗口吞吐提高27.7%，POST P99降低96.2%。B已消除忙用户跳过，但CPU仍约97%；C进一步把CPU降到约89%，并将POST P99从B的687ms降到191ms。

B和C的吞吐都受本轮20次流程/秒到达率约束，因此两者吞吐接近不表示剩余处理能力相同。C的CPU和延迟改善更能说明第二层转换的价值。P99大幅下降包含从过载排队转为基本可承受负载的效果，不表示每笔业务计算本身快了二十多倍。

## 失败和完整性

三组均exit0、无abort，结束时保持100公共和100私人连接；资金/国库/利息调整后差额为零，trade legs与持久成交事件数相等，首个公共观察者的已确认成交事件缺失数为零。该可见性证据不能扩展为全部客户端或私人通知逐条必达。

失败流程仍须保留在容量判断中：
- A 4Hz基线：计划800，成功完整流程（含排空）693，失败流程21；报价因version_conflict阻断21次，trade结果{'ok': 693}。
- B 仅访问层ASGI：计划800，成功完整流程（含排空）776，失败流程24；报价因version_conflict阻断24次，trade结果{'ok': 776}。
- C 两层ASGI：计划800，成功完整流程（含排空）783，失败流程17；报价因version_conflict阻断17次，trade结果{'ok': 783}。

失败原因的完整分类在comparison.json。模拟每笔先HTTP报价再提交，存在并发版本冲突；当前普通前端主要本地报价，不能把这些失败比例直接视为日常页面失败率。

## 实验边界

- A65897eb；B1f78cd0；C9224cec。相对A，应用源码只改变main.py与新增core/http_middleware.py，SSE4Hz、SQL、资金、鉴权、risk_log内部和客户端流程均相同。没有启用旧asgi_cadence8/split_reads变体。
- 同一Intel Xeon Gold6330开发主机、112逻辑CPU、单owner应用进程；PostgreSQL16本机Unix socket，每组CREATE新的fxcapacity_*_test数据库，fsync和synchronous_commit开启。没有生产代理/TLS/公网。
- 复用已审driver.py和current-only sampler，三HTTP池一致，SSE池400。显式RISK_LOG_ENABLED=1，每组真实风控日志写到该组risk目录；未通过关闭日志获得收益。
- 单热点币种，60%现货、20%融资、20%做空，含跨零及回补；保留报价、交易后GET和版本轮询。保持4Hz聚合，不更改挂单调度或通知可靠性。
- 每组仅一轮、固定A/B/C顺序，主机存在其他负载；没有重复统计、随机化次序或更高到达率搜索。结果支持该场景的改造方向，不是生产最大容量或精确百分比保证。
- 资源与队列按1秒采样，capacity阶段包含部分排空采样；CPU/SQL不是精确40秒积分，队列峰值不是连续峰值。HTTP窗口吞吐独立按截止时间统计。客户端CPU与到达迟滞保留，P99到达迟滞三组约27–30ms。

## 代码与行为验证

两个职责仍为独立中间件，顺序保持ServerError→risk→access→CORS→Exception/FastAPI。正常响应在http.response.start处先记录状态/风险日志，再转发；异常在响应开始前记录EXC/500，开始后不重复记。风险日志继续调用原真实record，Request(scope)只读元信息、不消费请求体。无新队列、任务组、重试或通用框架。

B相关检查43项通过，调整记录时序后的6项中间件检查通过；C已有37项权限/流/历史检查通过，修正新用例fixture后最终15项中间件检查通过，生产代码在该fixture修正时未变。覆盖只读503、缓存/CORS/重复cookie、请求体、日志I/O失败、后台任务、响应前后异常、断连取消和非HTTP。Sol High对B/C规格及代码质量均approved，无P0/P1/P2。

旧loadtest的asgi_cadence8/split_reads分支引用被迁移的main私有符号，仅适合其历史checkout；本次current-only路径已实际跑通。正式采用时应把这一实验入口兼容边界写清楚，不保留重复的生产策略函数。

## 建议

建议下一步选择C正式落地：其额外代码只是把现有risk记录包装为纯ASGI，保留职责和日志内容；本轮较B明显降低延迟和CPU。继续保持4Hz、原查询及资金边界。无需顺势引入多worker、读进程、异步日志队列或更换Web框架。试验完成后用户确认正式采用C；主工作区已快进纳入1f78cd0和9224cec两个已审提交，维护说明随后补齐。

## 产物

- [完整比较数据](comparison.json)、[版本和源码指纹](environment.json)、[执行记录](execution.json)。
- [实施验证记录](validation.md)、[Sol High审阅](code-review.md)、[实验方法](method.md)。
- 原始baseline/access/both目录保留各组请求、资源、真实risk记录、完整性与visibility。只含本机合成账户，token/private目录由driver finally清理。

完成核验：独立PostgreSQL已停止，driver/sampler已退出；三版源码指纹与运行前一致，候选工作区干净，主工作区HEAD仍65897eb且无tracked代码修改。本轮没有推送、合入主工作区或部署。

## 正式采用记录

用户已批准双层纯ASGI方案。main.py、core/http_middleware.py和test_http_middleware.py与已测已审9224cec逐字一致；额外仅修正文档中旧helper名称并注明历史负载变体的checkout限制。继续沿用37项既有检查、15项中间件检查与本轮A/B/C证据，没有重复跑测试/压测，也没有推送或生产部署。
