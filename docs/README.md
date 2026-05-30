# 东方炒炒币 (TouhouCCB)

模拟 Polymarket 风格的预测市场交易小游戏。用户用虚拟货币买卖事件结果份额，价格由 LMSR 算法驱动。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.13 + FastAPI + PostgreSQL (SQLAlchemy 2.0 + SQLModel) |
| 前端 | Vue 3 + TypeScript + Vite + Pinia + Naive UI + UnoCSS + lightweight-charts |
| 实时 | Server-Sent Events (SSE) |
| 定价 | LMSR（对数市场评分规则） |
| 认证 | Casdoor SSO（OIDC .well-known 自动发现） |
| 部署 | Docker Compose + GitHub Actions CI/CD + nginx |

## 项目结构

```
TouhouCCB/
├── .env.example              # 唯一配置模板（Docker + 后端共用）
├── docker-compose.yml        # 服务编排（backend + postgres）
├── deploy/
│   ├── nginx.conf            # nginx 反代 + 速率限制
│   └── deploy.sh             # 部署脚本（备份 + pull + 健康检查）
├── .github/workflows/ci.yml  # CI/CD（Docker 构建 + rsync + 部署）
├── backend/
│   ├── Dockerfile
│   ├── app/
│   │   ├── api/v1/           # 路由 (auth/user/market/chart/stream/loan/title/redemption/danmuku/site-config + admin_*)
│   │   ├── models/           # SQLModel 数据模型
│   │   ├── schemas/          # Pydantic 请求/响应 schema
│   │   ├── core/             # 配置、数据库、OIDC 客户端
│   │   └── services/         # LMSR 算法、SSE 实时推送
│   ├── init_db.py            # 数据库初始化
│   └── requirements.txt
└── thccb-frontend/
    └── src/
        ├── api/              # Axios 请求封装
        ├── components/       # 组件 (layout/, market/, chart/)
        ├── composables/      # 组合式函数
        ├── pages/            # 页面
        ├── router/           # Vue Router
        ├── stores/           # Pinia 状态管理
        └── types/            # TypeScript 类型
```

## 快速启动

### 生产部署（Docker Compose）

```bash
cp .env.example .env          # 编辑填入实际配置
docker compose up -d           # 启动 PostgreSQL + 后端
docker compose exec backend python init_db.py  # 首次初始化数据库
```

第一个通过 SSO 登录的用户自动成为管理员。

详细部署文档：[docs/deploy.md](deploy.md)

### 本地开发

```bash
# 后端（SQLite 模式）
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# 编辑 ../.env，设置 DB_BACKEND=sqlite
python init_db.py && python run.py

# 前端
cd thccb-frontend
npm install && npm run dev
```

## 功能

| 模块 | 状态 | 说明 |
|------|------|------|
| SSO 登录 | ✅ | Casdoor OIDC，首个用户自动成为管理员 |
| 市场列表 | ✅ | 搜索、状态筛选 |
| 交易（买/卖） | ✅ | LMSR 定价，实时报价预估，滑点保护 |
| K 线图 | ✅ | lightweight-charts，周期可切换，MA10 均线 |
| 价格走势 | ✅ | Area 渐变图，涨绿跌红，时间范围可调 |
| 实时推送 | ✅ | SSE 连接，断线自动降级轮询 |
| 持仓管理 | ✅ | LMSR 清算价值（含滑点），按市场分组 |
| 交易历史 | ✅ | 按类型/时间筛选 |
| 财富/消费排行榜 | ✅ | 含按净值自动判定的称号 |
| 保证金借贷 | ✅ | 杠杆买入、按日计息、维持保证金 |
| 强制平仓 | ✅ | 后台定时扫描，跌破维持保证金自动平仓（HALT 期间豁免） |
| 称号系统 | ✅ | 兑换码解锁、可佩戴、可作市场准入门槛 |
| 兑换码 | ✅ | 现金兑换码 + 合作方批量发码（CSV 导入） |
| 弹幕兑换 | ✅ | 现金兑换弹幕额度（HMAC 签名对接） |
| 反作弊 | ✅ | X-Client-Token 校验 + 行为信号检测 + 封禁 |
| 管理后台 | ✅ | 创建/熔断/结算市场，用户管理，调整现金 |

## 页面路由

（以 `thccb-frontend/src/router/routes.ts` 为准）

| 路由 | 页面 | 权限 |
|------|------|------|
| `/` | 首页 | 公开 |
| `/auth/login` | → 跳转 Casdoor 登录 | 公开 |
| `/auth/register` | → 跳转 Casdoor 注册 | 公开 |
| `/auth/callback` | OAuth 回调处理 | 公开 |
| `/market/list` | 市场列表 | 已认证 |
| `/market/:id/trade` | 交易视图 | 已认证 |
| `/market/leaderboard` | 财富 / 消费排行榜 | 已认证 |
| `/user/portfolio` | 资产持仓 | 已认证 |
| `/user/transactions` | 交易记录 | 已认证 |
| `/loan` | 保证金借贷 | 已认证 |
| `/redemption` | 兑换码商店 | 已认证 |
| `/my/redemptions` | 我的兑换码 | 已认证 |
| `/redeem-title` | 称号兑换 | 已认证 |
| `/danmuku/exchange` | 弹幕兑换 | 已认证 |
| `/admin/market-manage` | 市场管理 | 管理员 |
| `/admin/users` | 用户管理 | 管理员 |
| `/admin/site-config` | 站点配置 | 管理员 |
| `/admin/wealth-stats` | 资产统计 | 管理员 |
| `/admin/bot-review-ban` | 反作弊审核 | 管理员 |
| `/admin/titles` · `/admin/title-codes` | 称号 / 称号码管理 | 管理员 |
| `/admin/redemption/partners` · `/admin/redemption/batches` | 兑换码合作方 / 批次 | 管理员 |
## 图表架构

K 线和走势图的数据不是只查目标选项的交易记录，而是查**整个市场所有选项的交易**，逐笔重放 shares 状态，计算目标选项的瞬时价格。这是因为 LMSR 中交易任何选项都会改变所有选项的价格。

## 称号系统

全站按 `net_worth` 单一口径定档，阈值集中在 `backend/app/services/rank.py`（财富榜 / 消费榜的分桶见 `backend/app/services/wealth_stats.py`，与此一致）。

| 净值 | 称号 |
|------|------|
| > 30000 | ZUN |
| > 10000 | 炒炒币大亨 |
| > 3000 | 妖怪操盘手 |
| > 1000 | 天狗交易员 |
| > 300 | 人里居民 |
| ≤ 300 | 人类灵(已爆仓) |

> 此处是「按净值自动判定」的称号（rank）。另有一套可佩戴的**称号系统**（兑换码解锁 / 市场准入门槛），见 `docs/api.md` 的「称号」一节。

## 文档导航

| 文档 | 用途 |
|------|------|
| `docs/api.md` | 后端 REST API 规范（按代码核对） |
| `docs/development.md` | 开发工作流与技术栈约束 |
| `docs/deploy.md` | 部署：Docker Compose / CI-CD / nginx / 回滚 |
| `docs/migrations.md` | Alembic 数据库迁移工作流 |
| `docs/schema-conventions.md` | 数据库字段 / Decimal 序列化约定 |
| `docs/holdings-value-semantics.md` | 持仓估值双口径（MTM 展示 / LCV 风控） |
| `docs/rules-raw.md` | 游戏规则书（Markdown，另有 `docs/latex/rulebook.pdf` 印刷版） |
| `docs/style.md` | 前端工业风设计系统 |
| `docs/tos.md` | 用户服务条款 |
| `docs/ralph-log.md` | 开发迭代日志（历史记录） |
| `docs/superpowers/specs/`、`plans/` | 各功能设计稿与实施计划（历史） |
| `docs/archive/` | 已归档的代码审查 / 安全审计 / 性能调研快照 |

> 量化交易机器人的文档另见 `quant/README.md` 与 `quant/docs/`。
