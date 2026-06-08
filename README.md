# 东方炒炒币 TouhouCCB

> 基于 LMSR 的东方 Project 主题预测市场 / 虚拟券交易游戏。
> 用虚拟货币押注「哪个东方角色/作品更受欢迎」，价格由做市商算法实时决定。

[![License: MIT](https://img.shields.io/badge/License-MIT-black.svg)](./LICENSE)

> ⚠️ **同人项目声明**：本项目为基于东方 Project 的非官方、非营利同人创作，与 ZUN 及上海爱丽丝幻乐团无任何官方关联。东方 Project 相关角色名称与世界观版权归 ZUN 所有，依其二次创作规约（仅限非商业用途）使用。MIT 许可证仅覆盖本仓库的**源代码**，不授予任何东方 Project IP 或美术/音频素材的权利。

> 📖 **性质说明（研究 / 学习项目）**：本项目是一个非商业的**社团趣味活动 + 研究学习项目**——借一个东方主题的游戏直观演示并帮助理解**预测市场**、**LMSR 自动做市**、滑点、无风险套利等机制。代码与文档中的「买入 / 卖出 / 杠杆 / 利息 / 清算」等金融术语**仅作学习类比与游戏规则说明，不构成任何投资建议**；站内积分（「金圆券」）是纯虚拟数值，不代表人民币或任何真实货币。完整法律免责声明见规则书末尾的「免责声明」章节（`docs/latex/rulebook.tex` / `docs/latex/rulebook.pdf`）。

---

## 这是什么

TouhouCCB 是一个预测市场（prediction market）玩法的网页游戏：

- 每个「市场」是一个问题（如「最受欢迎的东方角色」），下面有多个「选项」（outcome）。
- 价格由 **LMSR（Logarithmic Market Scoring Rule）** 自动做市商决定——任一选项的买卖会实时影响全市场所有选项的价格。
- 玩家用虚拟货币买入/卖出选项份额，市场结算时按持仓清算盈亏，并有借贷、称号、排行榜等系统。
- 价格、成交、弹幕通过 SSE 实时推送。

## 项目状态

- **2026-05-23**：于第 17 届「东方幻想指南」活动当天正式部署上线，供活动参与者实际游玩。
- **现在**：活动已结束，项目进入**归档与开源整理**阶段——清理代码与文档、移除私有/生产配置，使其可被公开学习与自行部署。

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python · FastAPI · SQLAlchemy (async) · Alembic |
| 数据库 | PostgreSQL |
| 前端 | Vue 3 · Vite · TypeScript · Pinia · Naive UI · ECharts / lightweight-charts · UnoCSS |
| 认证 | Casdoor（自建 SSO，OAuth2 / OIDC） |
| 实时 | Server-Sent Events (SSE) |
| 部署 | Docker Compose · Nginx |
| 量化 bot | `quant/`（独立 Python 包 `thccb_quant`，配套交易机器人） |

## 仓库结构

```
backend/         FastAPI 后端（api / core / models / schemas / services）+ alembic 迁移
thccb-frontend/  Vue 3 前端
quant/           配套量化交易 bot（thccb_quant）—— 需一个运行中的后端实例
docs/            文档（部署、开发、API、数据库迁移、设计规范、玩法规则）
deploy/          部署相关脚本与配置
loadtest/        k6 压测脚本
```

## 快速开始

> **前置依赖**：本项目使用 **Casdoor** 作为 SSO 认证，后端在生产模式下缺少 Casdoor 配置会拒绝启动。
> 部署前需要先有一个可用的 Casdoor 实例（自建或托管）。完整部署步骤（含 Casdoor、Postgres、数据库初始化、前端构建）见 **[docs/deploy.md](./docs/deploy.md)**。

大致流程：

1. 准备 PostgreSQL 与 Casdoor 实例。
2. 复制 `.env.example` → `.env`，填入数据库、Casdoor、密钥等配置（**所有 `SECRET_KEY` 类变量必须改成你自己的随机值**）。
3. 复制 `thccb-frontend/.env.production.example` → `thccb-frontend/.env.production`，填入你的实例地址。
4. 初始化数据库并启动后端 / 前端（详见 deploy.md）。
5. **第一个通过 SSO 登录的账号会自动成为超级管理员。**

本地开发环境搭建见 **[docs/development.md](./docs/development.md)**。

## 文档导航

| 文档 | 内容 |
|------|------|
| [docs/architecture.md](./docs/architecture.md) | 系统架构总览 |
| [docs/deploy.md](./docs/deploy.md) | 生产部署 |
| [docs/development.md](./docs/development.md) | 本地开发环境 |
| [docs/api.md](./docs/api.md) | API 说明 |
| [docs/migrations.md](./docs/migrations.md) | 数据库迁移（Alembic） |
| [docs/schema-conventions.md](./docs/schema-conventions.md) | Schema / 字段约定 |
| [docs/holdings-value-semantics.md](./docs/holdings-value-semantics.md) | 持仓估值口径（MTM / LCV） |
| [docs/style.md](./docs/style.md) | 前端设计系统 |
| [quant/README.md](./quant/README.md) | 量化 bot |

## 贡献

见 [CONTRIBUTING.md](./CONTRIBUTING.md)（技术栈约束、精度规则、ORM 守则、测试与验证流程）。

## License

源代码以 [MIT](./LICENSE) 协议开源。东方 Project IP 与美术/音频素材不在此协议覆盖范围内（见顶部同人声明）。
