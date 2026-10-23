# TouhouCCB 4Hz与双层ASGI发布记录

核验时间：2026-10-23T07:49:09+00:00。用户明确授权提交、推送并通过Coolify部署。

- 发布代码：`5a57577a3ea7e25ea91ad90f650536409875920f`，推送main后由现有CI/CD执行。
- [GitHub Actions运行](https://github.com/Renko6626/TouhouCCB/actions/runs/39919090995)：Backend Check & Build、Frontend Check & Build、Deploy均success。
- Coolify应用：`yootagfyu21rjdfspwmrdxny`；部署：`uekwal3egt4uzgyf7sbrdmbu`，状态finished。
- 前后端ACR镜像均使用上述完整SHA，前端、后端、PostgreSQL容器均healthy。发布前镜像为8904d1a。
- 沿用现有Coolify自定义部署流程；完整备份恢复验证通过，备份 `/home/deploy/TouhouCCB/backups/thccb_20261023_074437_1409832.dump`，随后迁移/账本校验与健康检查由脚本执行成功。没有修改代理、数据库卷或旧容器状态。

## 云端核验

- 后端镜像内APP_BUILD_SHA等于发布SHA。
- 公共FX与私人pricing的PUBLISH_INTERVAL均为0.25秒。
- 注册的自定义中间件顺序为RiskRequestMiddleware、AccessPolicyMiddleware、CORSMiddleware。
- 公网首页HTTP200，内容SHA256与新前端容器index.html完全一致：`431c83c0cb2cd93b7e7149f1418e3c3fb05fe88ec88547b977c8f8586d6c3e92`。
- `/api/v1/market/list` HTTP200、合法JSON、Cache-Control包含no-store。
- 未认证 `/api/v1/stream/trading` HTTP401。

本轮没有在生产发起交易、浏览器自动交互或压力测试。性能数字仍属于开发机实验；生产核验覆盖发布版本、基础可用性、配置与未认证访问边界。

此记录及维护说明以文档提交更新，使用skip ci避免再次触发相同代码部署；线上运行版本仍为上面的发布SHA。
