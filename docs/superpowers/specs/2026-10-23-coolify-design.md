# TouhouCCB Coolify 迁移

用户已于 2026-10-23 确认：前端、后端、PostgreSQL 全部 Docker 化，由 Coolify 管理发布流程。

选择保留 GitHub Actions 检查和 ACR 镜像构建，完成后触发 Coolify；相比生产服务器从 Git 构建，沿用现有 CI，减少生产机负担。前端新增 Nginx 镜像，保留 SPA、限流、SSE 和历史缓存规则；Coolify Traefik 管理域名和 TLS。

数据库使用 PostgreSQL 16，复用 external 卷 touhouccb_pgdata。配置、业务数据和备份沿用 /home/deploy/TouhouCCB 下现有路径，不移动业务数据。新旧 PostgreSQL 不得同时挂载同一数据卷运行；后端保留单实例。

Coolify 发布必须保留停写、备份及恢复验证、alembic upgrade、audit_verify 和健康检查。前后端使用同一提交 SHA 的不可变镜像。首次迁移先准备资源、镜像和回退配置，再停旧容器、切换并验证域名。保留旧配置，不删除数据库卷。

常规部署不再 rsync 前端或 SSH 执行旧发布。赛季维护等专用流程应指向 Coolify 实际 Compose；业务清库工作流不得继续操作旧部署。验证以 Compose/Nginx/脚本检查及真实 HTTP 只读检查为主，不自动操作页面或执行业务重置。
