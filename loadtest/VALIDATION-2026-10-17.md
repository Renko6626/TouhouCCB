# FX HTTP 压测套件验证记录（2026-10-17）

实现位于 `ralph/2026-10-17-fx-loadtest`，隔离目录 `/tmp/sunyunbo/thccb-fx-loadtest-20261017`。本记录仅验证套件及限流改动，不是目标服务器的容量报告。未修改线上配置、在线上创建账户、成交、清理或运行压测。

## 已执行检查

Python 使用已有 `/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python`，无新增依赖。

| 检查 | 实际命令/方式 | 结果 |
| --- | --- | --- |
| 后端全量既有检查 | 在 `backend/`：`timeout 300 <python> -m pytest -x -q` | 1521 passed，1 skipped，46 deselected，291.67 秒；1 项现有 aiosqlite 连接回收 SAWarning |
| 相关 SSE 与驱动回归 | `<python> -m pytest -q tests/test_fx_loadtest_driver.py tests/test_realtime_broker.py tests/test_realtime_namespace.py tests/test_fx_stream.py tests/test_api_stream_namespace.py` | 34 passed；包含新增调度延迟回归 |
| 管理 CLI 的 PostgreSQL 行为 | `TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55569/fx_loadtest_test <python> -m pytest -q -m pg tests/pg/test_fx_loadtest.py` | 2 passed：准备 100 个普通账户、短期 JWT、真实 net 成交、preview/清理及原数据保留；重复 prepare 与外部玩家参与拒绝 |
| Python 编译和导入 | 编译全部 `app/**/*.py`、新 CLI/驱动/测试；`import app.main` | 通过；隔离开发环境无 Casdoor 的提示符合预期 |
| Shell/空白格式 | `bash -n deploy/fx_loadtest.sh`、`git diff --check` | 通过 |
| CLI 入口 | `python -m scripts.fx_loadtest --help`、`mint`、`inspect`、`cleanup --execute --confirm-stopped` | 后三项实际连接本轮临时 PG，凭证没有打印；清理后 SOURCE 恢复 trading，本轮账户剩余 0 |

全量测试在新增驱动回归加入前已开始收集，因此该新增用例通过独立相关检查补验，不把两个检查的测试数量相加。

## 实际 HTTP 与 Nginx

使用本轮创建的临时 PostgreSQL 14、正常 FastAPI lifespan 和 loopback HTTP socket；应用端口 55570。没有使用 ASGI mock transport替代这部分网络验证。

为核对 Nginx，临时解包系统仓库的 Nginx 1.18 二进制，没有安装到系统或修改宿主机服务。测试配置从当前模板生成，只替换临时端口、上游端口和缓存/日志路径，并移除线上证书配置。`nginx -t` 通过；二进制启动时提示默认 `/var/log/nginx/error.log` 不存在，实际日志写临时配置路径。生产证书、站点副本、CDN 和 reload 尚未验证。

- 1 人真实 HTTP 冒烟成功；最终 Nginx 冒烟有 5 次成功成交，公开 snapshot 和私人 sync 均有效。
- 经 Nginx 的 2 人缩短版 full 流程完成 smoke、ramp、steady、burst，无错误结果，合计 20 次成功成交。只用于检查阶段执行、采样和报告生成，不作为性能基线。
- 同一 loopback IP、两个不同 token，各 30 次 `/api/v1/auth/me` 查询：60 次均为 200。相同 IP 的 70 次匿名 `/api/v1/fx/pairs` 查询中，42 次为 200、28 次为 429，证明匿名没有因空限流 key 绕过限制。
- 100 个独立账户成功建立 100 条公开 FX SSE 和 100 条私人 SSE。随后用每 0.5–0.7 秒一轮、每档仅 3 秒的激进本地参数，触发应用连接池 `size 10 overflow 20` 超时和断流，套件报告错误并停止升压。**这不等于百人稳态容量通过，也不能推广为实机性能。** 没有为本轮工具修改游戏连接池、交易锁或数据路径。

固定观察者按成交 ID 匹配广播；报告将请求开始到该观察者收到成交的时间与依赖时钟的事件年龄分开。报告不含 token、Authorization 或账户完整响应。

## 独立审阅及修正

完成一次独立只读代码审阅，重点检查限流分桶、SSE 优先级、普通账户属性、成功吞吐口径和清理边界。发现调度器在慢响应后重设时间会抹掉错过节奏的延迟，已修正为基于本轮实际开始时间计算下一预定时间，保留下一次开始的真实晚到量，不累计补发队列。

新增一条直接调用真实 `Runner.stage` 的回归，用 60ms 串行服务和 10ms 节奏验证延迟至少 30ms且最大在途为 1。恢复原调度写法时观察到失败（最大约 1ms），修正后通过。审阅指出的过时 Action 计划和匿名限流注释同时修正；未追加复审轮次。

## 初版收尾及边界

- 已停本轮临时 Nginx、Uvicorn、PostgreSQL，并删除用于网络验证的临时 token 文件；保留不含凭证的日志、manifest 和报告于 `/tmp/sunyunbo/fx-lt-work`、`/tmp/sunyunbo/fx-lt-http` 等本轮目录。
- 本地真实成交后的 CLI 清理覆盖 132 条成交及执行回执、行情记录、钱包等依赖；恢复原 SOURCE 市场，100 个本轮账户均删除。
- 当前机器没有可用 Docker daemon，服务器 `deploy/fx_loadtest.sh` 未经过实际 Docker Compose 停服/备份/维护/恢复演练，仅完成语法检查；底层 Python CLI 已通过上述 PG 与本机命令入口验证。
- 初版未验证实机百人正常频率与集中下单、线上真实 Nginx/CDN 行为、服务器资源指标、融资/做空/挂单、百人同时登录及刷新。实际操作见 [FX 操作说明](FX_README.md)。

## 扩展版验证：混合角色与加密凭证

扩展沿用上述隔离分支与本机临时 PostgreSQL，未部署、未触发真实 Action，也未向目标域名发压测流量。

- 相关检查实际命令：`<python> -m pytest -q tests/test_fx_loadtest_driver.py tests/test_fx_loadtest_credentials.py tests/test_realtime_broker.py tests/test_realtime_namespace.py tests/test_fx_stream.py tests/test_api_stream_namespace.py`，结果 **37 passed，5.54 秒**。新增行为覆盖真实融资/还款、跨零/回补与成交后刷新；SSE 较新版本覆盖旧版本查询，避免多余修复请求；加密导出删除服务器明文、错误私钥及被修改的密文解密失败。
- 管理 CLI 的既有 PostgreSQL 检查再次执行：`TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55569/fx_loadtest_test <python> -m pytest -q -m pg tests/pg/test_fx_loadtest.py`，**2 passed，1.66 秒**。
- Python 编译、`bash -n deploy/fx_loadtest.sh`、`git diff --check` 和 actionlint 1.7.12 检查 `.github/workflows/fx-loadtest.yml` 均通过。扩展只改工具、工作流和文档，未再次运行后端全量检查；上文全量结果属于初版。
- 使用真实 FastAPI lifespan、PostgreSQL 和 loopback HTTP，10 个账户按 6 现金/2 融资/2 空头运行缩短版 full。报告覆盖现金买卖、6 次融资、6 次卖出还款、2 次跨零、4 次回补、2 次开空；6 张条件单中 3 张撤销、3 张实际成交，收到 3 条私人订单结果事件。报告无 abort，10 条公开与 10 条私人 SSE 均保持连接。这验证路径及采样，不能推断百人容量。
- 修正后单独复跑突发恢复：10 笔写请求全部成功，实际提交窗口 **2.001 秒**；突发阶段 11 次版本查询，最后一次在最后成交约 **13.55 秒后**完成，20 条 SSE 保持连接，证明恢复观察期仍有轻量 HTTP 请求。缩短阶段不能拿名义稳态速率做性能结论。
- 本地 CLI keygen、服务器 export 及本地解密实际通过，10 个测试账户凭证可还原且 export 删除明文。仅查询 GitHub secret 名称确认既有 DEPLOY_HOST/USER/KEY 存在，没有读取其值。真实 SSH、Docker Compose 维护、Actions artifact 上传/下载尚未执行；须先合入 main 并正常部署 fx-loadtest-2。
- 完成后停止本轮 Uvicorn；用真实 CLI 清理了 173 条成交、155 条执行、12 张条件单、35 条账本记录及相关池子/行情/持仓，10 个测试账户全部删除。数据库查询确认原 ORIGINAL 市场仍为 trading，保留原用户 retained。随后停止本轮 PostgreSQL，删除网络测试 token 和本地接收私钥。

扩展日志与报告位于 `/tmp/sunyunbo/fx-lt-expanded/`，相关检查位于 `/tmp/sunyunbo/fx-lt-work/expanded-final-related.log`。正式操作及负载参数见 [FX 操作说明](FX_README.md)。

## 实机执行补记

用户随后授权完整实机流程，已发布后端 ce09c41，维护脚本最终修正至 9a1acb1。服务器实际执行验证补齐了运行镜像固定、Compose 参数兼容、标准输入隔离及临时维护容器 UID/GID；本地 Docker 未演练的边界如实保留在前述记录。

真实目标 thccb.secret-sealing.club：100 个普通账户、200 SSE 入场及 121 秒只读通过。正常阶段名义 2.5 次尝试/s，实际 75 个循环、10 个成功成交，274 个 HTTP 结果中 184 个超时，因滚动错误率超过 5% 自动中止；后续高压和条件单阶段未执行。服务端明确记录 size10+overflow20 连接池超时。Nginx 站点写权限不足，限流补丁未安装，线上配置未改变。

Action cleanup 成功删除 100 个本轮账户、117 条成交及依赖，恢复原市场 ID8 trading；公开 FX 和 market/list 都返回有效 JSON 200，复核耗时 175.4ms / 84.4ms。开发机 token 与接收私钥已删除；备份/manifest 留存。完整采样、诊断、Actions 链接及性能表在开发机 output/fx-live-20261017/report.md。本轮未修复业务性能问题，连接具体被哪条调用链占用仍待诊断。
