# 管理员后端重启实施计划

> 使用 superpowers:executing-plans 在当前隔离 worktree 内直接实施。用户已确认复用 Docker 自动重启方案，并要求迅速完成。

**目标：** 在站点配置页允许管理员确认重启，并确认新实例恢复。

**架构：** 管理 API 先持久记录操作，再响应，最后向当前 PID 1 发送 SIGTERM。既有单进程 uvicorn 完成停机清理，由 Docker 的 unless-stopped 策略重启；不增加宿主机控制服务或 Docker socket 权限。

**技术：** 既有 FastAPI、SQLModel、Vue、Naive UI。

## 约束与重点

- 仅管理员可读状态和请求重启；默认禁用，Compose 显式启用，非 PID 1 拒绝。
- 复用 SiteConfig 隐藏记录及 config_set 审计，记录操作人和持久冷却时间；并发请求只能接受一个。
- 请求携带当前实例标识，过期请求拒绝；恢复轮询必须看到新实例，超时提示人工检查。
- 未保存草稿不自动保存，重启期间禁用页面配置操作，组件卸载停止轮询。
- 不实际重启线上服务；不自动点击或填写页面，不新增样式测试。

## 实施

- [x] 后端：新增 admin_system API、backend_restart 服务及启用配置，覆盖权限、部署门槛、重复请求、操作落库、过期实例行为；信号发送在测试中替换。
- [x] 前端：新增管理 API 方法，站点配置维护卡片、确认框、带超时的新实例检测；更新部署说明。
- [x] 验证：运行相关后端已有检查及新增行为测试、前端类型检查和构建；审查实际 diff。不自行提交、推送或执行生产重启。

## 执行记录

- 新 API 权限用例先失败（404），实现后通过。相关后端检查 46 项通过，真实 SIGTERM 在测试中替换。
- 独立只读审查发现单项配置保存未跟踪在途请求；已增加 activeSaves 计数，全部完成前禁用重启。未为简单计数另搭测试层，按用户约定使用现有类型、lint、构建检查。
- 重启使用现有优雅停机流程，后端完全无响应时仍使用服务器命令恢复。

验证命令（均退出 0）：

```sh
# 使用临时 SQLite 数据库，未连接生产库
DATABASE_URL=sqlite+aiosqlite:////tmp/touhou-admin-restart-regression.db python -m pytest -q tests/test_admin_system.py tests/test_site_config.py tests/test_credit_config.py tests/test_credit_startup_gating.py tests/test_writer_admin_ops.py
npx eslint src/components/BackendMaintenance.vue src/pages/admin/SiteConfig.vue src/api/admin.ts
npx vue-tsc --noEmit
npm run build
```

构建仍有既有大 chunk 提醒；未验证 Docker 实际退出并重新拉起，未进行页面自动交互验证。
