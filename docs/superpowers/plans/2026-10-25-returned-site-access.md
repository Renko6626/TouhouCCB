# Returned 封站兼容修复

> 执行：使用 executing-plans 在当前会话实施，完成后独立审阅一次。用户已明确授权修复并发布服务器。

**目标：** 封站期间，有效 Returned 联动密钥继续查询和推进竞猜，健康检查继续正常。

**已确认方案：** 沿用现有联动密钥摘要验证和全部业务鉴权，仅在 Returned API 范围内允许有效来源凭据通过封站层。容器及现有部署、维护探针统一使用已有的 `/health`，它检查进程与数据库。不新增依赖、接口、数据库字段或恢复机制。保留线上封站开关当前值。

**约束：** 不代替朋友取消或重放竞猜，不修改余额，不执行浏览器交互。沿用 main CI、SHA 镜像和 Coolify 自定义部署流程。用户已批准本方案和发布，无需重复确认。

## Task 1：联动凭据通过封站层

文件：`backend/app/core/site_access.py`、`backend/app/services/returned_config.py`、`backend/app/api/v1/returned.py`、`backend/tests/test_returned.py`。

- [x] 在现有真实 API 测试中重现：付费竞猜后封站，来源查询、封盘、结算或取消应成功，并验证最终余额。验证普通账号、错误密钥继续被拒绝，来源密钥不能访问其他业务接口。
- [x] 执行新增测试，确认失败原因是封站层误把联动密钥当 JWT。
- [x] 提取 `matches_secret(credential, config)` 供现有来源鉴权与封站层复用；仅启用联动且路径为 `/api/v1/returned/` 时允许匹配密钥通过。其余请求保持现有管理员校验。

## Task 2：健康探针与发布

文件：`backend/Dockerfile`、`deploy/deploy.sh`、`deploy/season_reset.sh`、`deploy/business_reset.sh`、`backend/tests/test_season_reset_runner.py`、`docs/returned-integration.md`。

- [x] 将实际容器及现有运维探针地址改用 `/health`，同步现有维护脚本测试协议。
- [x] 集中执行：`pytest tests/test_returned.py tests/test_returned_config.py tests/test_site_access.py tests/test_http_middleware.py tests/test_season_reset_runner.py -q`，以及受影响脚本 `bash -n`、`git diff --check`。
- [ ] 独立只读审阅一次，处理具体问题后提交并推送 main，等待既有 CI/CD 成功。
- [ ] 线上只读确认镜像 SHA、三个容器健康、封站值保持、管理员可查询竞猜、普通请求仍被拒绝。尽可能从发送端现有私有配置在内存使用实际密钥验证 GET，不输出密钥或摘要；拿不到原密钥则明确这项限制，不重设密钥。

## Review Focus

- 来源豁免不得覆盖其他 API 或依赖普通用户 JWT 的个人操作。
- 错误密钥、未配置密钥、停用联动不得获得封站豁免。
- 管理员 JWT 续期和旧 SQLAdmin 登录继续保持。
- 封站保持开启时，部署探针仍需成功。
- 线上校验只读，不新建、取消、结算真实竞猜。

## 执行记录

- 根因已由上一轮线上只读调查确认：47 轮全部终结；封站后来源 GET 返回 401；`/health` 正常但 Docker 旧探针返回 403。

- 新增三个回归场景修复前均返回 401 失败；修复后相关 72 项测试通过（21.71 秒），bash -n 与 git diff --check 通过。
- 独立只读审阅无阻断项；不重复全量本地测试，沿用既有 CI 全量检查后部署。
- 发送端不在本 VPS，现有本机配置也无原始密钥；上线后优先观察其真实轮询恢复，不重置密钥。
