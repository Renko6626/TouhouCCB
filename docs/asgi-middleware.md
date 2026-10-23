# HTTP中间件维护说明

访问策略与风控请求记录已采用纯ASGI中间件，入口为 `backend/app/core/http_middleware.py`，通过 `main.py` 的公开 `app.add_middleware` 注册。用户在4Hz基线的三组对照及Sol High审阅后确认采用双层方案。

入站顺序：ServerError → RiskRequestMiddleware → AccessPolicyMiddleware → CORS → Exception/FastAPI。

- AccessPolicyMiddleware统一执行非安全方法的只读/所有权拦截、API no-store与Pragma、访问日志及跳过前缀。提前503继续保留既有响应头/日志边界。
- RiskRequestMiddleware记录非GET/HEAD/OPTIONS的API请求，其余请求直接透传。继续调用原risk_log.record，读取Request的元信息，不消费请求体；日志I/O失败仍由原record处理。
- 状态和耗时在收到http.response.start、转发前记录；耗时是到响应头就绪，不是完整SSE连接时长。响应开始后的body/background/send错误继续传播，不补记第二条500。取消继续传播。
- 每请求状态放在__call__局部；不增加任务、通道、缓冲或重试。保持receive和body消息转发，以及非HTTP scope的透传。

维护时不要在main里复制策略，也不要让中间件反向导入main。更改注册顺序、请求体读取、异常或日志时序时，应核对对应行为检查。当前SSE公共FX与私人pricing仍4Hz，LMSR公共仍8Hz；资金、鉴权及数据库事务由原业务链处理。

`loadtest/fx_capacity_server.py` 的current入口可采样当前应用。历史cadence8/asgi_cadence8/split_reads变体应在原实验记录的checkout运行，不能把旧的main私有符号替换逻辑用于当前代码。历史报告是历史版本的证据。

验证：37项既有权限/流/历史检查与15项中间件检查通过，Sol High规格和代码质量均通过。正式落地保持已测实现一致，没有重复压测。详细命令、边界及数据见[验证记录](../output/asgi-ab-20261023/validation.md)、[审阅](superpowers/reviews/2026-10-23-asgi-middleware-code-review.md)、[性能报告](../output/asgi-ab-20261023/report.md)。

已于2026-10-23通过GitHub Actions和Coolify发布生产，代码版本5a57577。云端4Hz、双层ASGI、容器健康及公网只读检查均通过，见[发布记录](deployment-2026-10-23-sse-asgi.md)。
