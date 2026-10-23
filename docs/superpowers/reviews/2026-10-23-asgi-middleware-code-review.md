# ASGI B/C independent code review

审阅结论：B、C 均满足本次已批准实验的规格，未发现 P0/P1/P2 代码问题；可以进入同为 4 Hz 的 A/B/C 对照。此结论仅针对候选实现和已报告的集中验证，不代表已批准合入、部署或已经证明性能收益。

审阅对象：A `65897eb`；B `1f78cd0`（access-only 固定 tree）；C `9224cec6a2070ae7a8d0a071a7a223f903f7e190`。已读固定 `review.diff`、`brief.md`、`implementation-report.md`，并核对 B/C 代码、现有 `risk_log.record`、ownership、current 容量采样入口。审阅未修改生产代码，未重跑测试/负载，未访问生产，未委派子 agent。

## B

- **Spec：通过。** `main.py` 用公开 `app.add_middleware(AccessPolicyMiddleware)` 替代访问层 decorator；risk decorator 保留，外到内仍为 ServerError → risk BaseHTTP → access ASGI → CORS → Exception/router。风险层策略未随 B 改写。
- **Code quality：通过。** 独立模块仅承接既有访问策略，没有 task group、请求缓冲、队列、重试或额外共享状态；依赖由 main 指向 middleware。`scope["path"]` 用于现有前缀判断，原始 receive 和 body message 直接透传。
- 对照 C 中相同实现的行号：`backend/app/core/http_middleware.py:48` 保留只读/ownership 检查及真实 `require_writes`；`:53` 的提前 503 保留基线没有访问日志、API 缓存头、内层 CORS 的行为；`:57` 保留跳过前缀；`:68` 用 MutableHeaders 只改 API 缓存头，不折叠 Set-Cookie；`:67`/`:83` 使 headers 前异常记 EXC，headers 后 body/background/send 异常不重复记访问日志。

## C

- **Spec：通过。** `backend/app/main.py:378`、`:381` 保持 risk 在 access 外、CORS 在 access 内的职责顺序；仅风险层由 BaseHTTP 改为 ASGI，4 Hz/SQL/资金/auth/risk_log 内部/前端/部署没有改动。
- **Code quality：通过。** `backend/app/core/http_middleware.py:98` 对安全方法、非 API、非 HTTP 直接透传；`:106` 的 Request(scope) 仅供现有 risk_log 读取 URL/header/client 元数据，没有 receive 或请求体消费。`:114` 在 record 和上游 send 之前标记 headers 已开始，`:115` 在转发响应头前等待真实 record；`:121` 只对 headers 前的普通异常补一条 500。headers 后异常保留实际响应状态且不会补第二条记录；CancelledError 保持传播，不转成 500。
- 原有 `backend/app/core/risk_log.py:170`–`:201` 保留写失败吞掉并报 error 的实现，middleware 没有复制其策略，也没有增加恢复机制。已有 authenticated bot 查询开销和失败策略仍属于原实现，未扩展为本次新需求。

## 验证证据与边界

以下来自 implementer 的报告，审阅者未重复执行：

- B：相关集中命令 43 passed；日志提前到 await send 之前的修正后，中间件 6 passed。
- C：集中命令 50 passed / 1 failed；37 个既有 guard/stream/history 检查均通过。失败为新增 readonly 夹具只设 `_reason`、未设 `_writes_enabled=False`。修夹具并增加 headers 后取消参数后，中间件 15 passed；此后生产代码未改。两提交 diff --check 均通过。
- 新增用例调用实际生产 middleware 和 FastAPI route，并用真实 risk_log.record 写临时目录，覆盖请求体、重复 cookie、CORS、缓存、503 在 send 前只记一次、headers 前后异常、后台任务、send 失败、断连/取消、非 HTTP 透传及真实目录 I/O 失败。未另行测试 authenticated bot/DB lookup；该内部逻辑未改。
- 本次未运行浏览器、E2E、全仓测试或性能测试。性能结论需由 controller 用已审 driver 的 current 入口完成 A/B/C 对照。

## 历史实验兼容性

已披露的历史兼容性变化可接受，不作为本次阻塞：`loadtest/fx_capacity_server.py:100`–`:109` 的 asgi_cadence8/split_reads 分支仍引用已移除的 main.log_requests，DirectASGIMiddleware 在 `:32` 还引用旧 main 私有符号。它们应使用历史 checkout，不能在 B/C 上继续冒充有效变体。`:82`–`:98` 的 current 路径不进入这些分支；driver 固定 variant=current，静态核对未发现 current 导入对这些已移除符号的依赖。未为了兼容历史原型保留重复、失效的生产策略函数。

没有需要提交最小修复的 P0/P1/P2 发现。可以继续已授权的有限对照实验。
