# PR3 文档重写计划 — 2026-06-08

分支 `feat/2026-06-08-docs-rewrite`。承接 PR1/PR2（见 `oss-cleanup-2026-06-08.md`）。

**已定方向**：① 中文为主（README 中文版保留）② 全面重写，以「陌生人首次 clone」为主线
③ CLAUDE.md 原样保留 + 另建人类向 CONTRIBUTING.md。

## 交付物

### 新增
- `CONTRIBUTING.md`（root）— 人类向贡献指南，从 CLAUDE.md 提炼（去 AI 专属内容）：
  栈约束、Decimal 精度 6/8、`lazy="raise_on_sql"` ORM 守则、分支/commit 规范、验证流程、高敏感区
- `docs/architecture.md` — 系统总览：仓库地图、后端分层、前端结构、核心概念
  （LMSR/SSE/认证/MTM·LCV/账本/精度）、数据流

### 重写
- `docs/development.md` — 完整本地 onboarding（修端口 8000→8004，补后端/前端/DB/测试/Casdoor 说明）
- `docs/README.md` — docs 导航中枢，去 superpowers 引用

### 整理（保留+清理）
- `docs/deploy.md` — 删过期一次性迁移节、修节号、修 init_db vs alembic 矛盾、加 Casdoor 前置占位指引
- `docs/migrations.md` — 修 init_db 矛盾，与 deploy 统一
- `docs/api.md` — 去 superpowers 引用、补 ledger 接口说明
- `docs/schema-conventions.md` — 去私有 commit/内部引用
- 代码注释里 `docs/superpowers/specs/...` 悬空引用（约 10 后端文件 + quant docstring）→ 删/改指现存文档
- `docs/archive/` AI review 文件 → 加免责声明 header

### 不动
`holdings-value-semantics.md`、`style.md`、`rules-raw.md`+`latex/`、`tos.md`

### 留 PR4
完整 Casdoor 自建指引、一键全栈 compose、`quant/docs/sse-contract.md`

## 执行方式
sonnet subagent 并行写互不相干的新文档（CONTRIBUTING / architecture / development）；
敏感整理（deploy/migrations 矛盾、代码注释清理、api/schema 私有引用）由主 agent 自己改。
所有命令/路径以读实际代码为准，不臆造。
