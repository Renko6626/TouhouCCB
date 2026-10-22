# Coolify Migration Implementation Plan

**Goal:** 将 TouhouCCB 前后端及 PostgreSQL 交给 Coolify 管理。
**Architecture:** CI 构建并推送同 SHA 的前后端 ACR 镜像，成功后触发 Coolify Git Compose 应用。复用生产数据卷、配置和备份目录。
**Tech Stack:** Docker Compose, Nginx, PostgreSQL 16, GitHub Actions, Coolify 4.4.7。
**Spec:** docs/superpowers/specs/2026-10-23-coolify-design.md

## Global Constraints
- 不重置业务数据，不并行运行挂载同一数据卷的 PostgreSQL。
- 不恢复旧 Nginx 公网监听，不自动交互测试。
- 保留停写、备份、schema 迁移和账本校验。

## Review Focus
- external 数据卷名称、已有 UID/GID 和宿主机路径正确。
- SSE、限流、真实客户端 IP 和历史缓存保留。
- 新旧后端不同时写，前后端 SHA 一致。
- CI 等待检查成功，部署失败可观察。
- 专用维护流程不再误操作旧部署。

## Tasks
- [x] 增加前端运行镜像、容器 Nginx 配置和 Coolify Compose；用 Nginx 与 Compose 配置检查验证。
- [x] 在 Coolify 自定义启动命令中接入现有备份迁移顺序，更新 CI 双镜像构建与 API 触发；运行 shell 静态检查及必要现有测试。
- [x] 准备 Coolify 资源、环境变量、镜像、回退记录；首次切换，验证数据库版本与 HTTP 健康。
- [x] 更新维护入口和部署文档；记录实际验证结果。

## Progress
- 已完成只读生产调查，用户已确认路线。实施在独立分支 ops/coolify-migration。

- 生产资源 `yootagfyu21rjdfspwmrdxny`，Coolify 状态 running:healthy，成功部署 `hqmprpbnfvzarjj1bjqd4bnc`。
- CI run 39829754509 检查和双镜像构建成功，首次发布手动调用相同 API 发布脚本；CI 发布暂停变量已恢复 false。
- Compose/Nginx/bash 检查通过，维护 runner 34 项通过，CI 后端 1547 passed / 1 skipped，FX 独立用例 14 passed。
- 源站前端、源站 API、公网首页 HTTP 200，TLS 校验成功；未进行浏览器交互或业务重置。
- 首次 GitHub App API 连接重置，旧服务恢复后重试成功；保留日志。ESA 强制 HTTPS 导致 ACME 首次签发失败，已有有效证书已导入 Traefik ACME 存储，HTTPS 验证路由接到 acme-http@internal。未实际执行未来续期。
