# 东方炒炒币 (TouhouCCB)

模拟 Polymarket 风格的预测市场交易小游戏。用户用虚拟货币买卖事件结果份额，价格由 LMSR 算法驱动。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.13 + FastAPI + SQLite (SQLAlchemy 2.0 + SQLModel) + JWT 认证 |
| 前端 | Vue 3 + TypeScript + Vite + Pinia + Naive UI + UnoCSS + ECharts |
| 实时 | Server-Sent Events (SSE) |
| 定价 | LMSR（对数市场评分规则） |

## 项目结构

```
TouhouCCB/
├── backend/             # FastAPI 后端
│   ├── app/
│   │   ├── api/v1/      # 路由层 (auth, market, user, chart, stream)
│   │   ├── models/      # SQLModel 数据模型
│   │   ├── schemas/     # Pydantic 请求/响应 schema
│   │   ├── core/        # 配置、数据库、用户管理
│   │   └── services/    # 业务逻辑 (lmsr.py, realtime.py)
│   ├── data/thccb.db    # SQLite 数据库
│   ├── init_db.py       # 数据库初始化
│   └── run.py           # 启动入口
└── thccb-frontend/      # Vue 3 前端
    └── src/
        ├── api/         # Axios 请求封装
        ├── components/  # 可复用组件 (layout/, market/, chart/)
        ├── composables/ # 组合式函数
        ├── layouts/     # 页面布局模板
        ├── pages/       # 页面组件
        ├── router/      # Vue Router 配置
        ├── stores/      # Pinia 状态管理
        ├── types/       # TypeScript 类型定义
        └── utils/       # 工具函数
```

## 启动方式

```bash
# 后端
cd backend
python init_db.py    # 首次运行初始化数据库
python run.py        # 启动开发服务器 (localhost:8000)

# 前端
cd thccb-frontend
npm install
npm run dev          # 启动开发服务器
npm run type-check   # TypeScript 检查
npm run lint         # ESLint 检查
npm run build        # 生产构建
```

## 功能完成状态

| 功能模块 | 后端 | 前端 | 说明 |
|---------|------|------|------|
| 用户注册/登录 | ✅ | ✅ | JWT 认证，激活码机制 |
| 账号激活 | ✅ | ✅ | 一次性激活码 |
| 市场列表 | ✅ | ✅ | 含状态筛选 |
| 市场交易（买/卖） | ✅ | ✅ | LMSR 定价，含报价预估 |
| 价格图表 | ✅ | ✅ | 分时图 + K 线图，ECharts |
| 深度图 | ✅ | ✅ | 订单簿可视化 |
| 用户持仓 | ✅ | ✅ | 含市值计算 |
| 交易历史 | ✅ | ✅ | 含筛选 |
| 财富排行榜 | ✅ | ✅ | 含称号系统 |
| 实时数据 (SSE) | ✅ | 🟡 | API 封装完成，页面联调中 |
| 管理员-激活码 | ✅ | ✅ | 生成/列表/作废 |
| 管理员-市场管理 | ✅ | 🟡 | 创建/熔断/恢复/结算，待完整联调 |
| 管理员-系统监控 | ✅ | 🟡 | 部分为汇总数据，非实时统计 |

## 页面路由

| 路由 | 页面 | 权限 |
|------|------|------|
| `/` | 首页 | 公开 |
| `/auth/login` | 登录 | 公开 |
| `/auth/register` | 注册 | 公开 |
| `/auth/activate` | 账号激活 | 公开 |
| `/market/list` | 市场列表 | 已认证 |
| `/market/:id/trade` | 交易视图 | 已认证 |
| `/market/leaderboard` | 财富排行榜 | 已认证 |
| `/user/portfolio` | 资产持仓 | 已认证 |
| `/user/transactions` | 交易记录 | 已认证 |
| `/admin/markets` | 市场管理 | 管理员 |
| `/admin/activation-codes` | 激活码管理 | 管理员 |
| `/admin/system-monitor` | 系统监控 | 管理员 |

## 称号系统

净值排位对应称号：

| 净值 | 称号 |
|------|------|
| > 50000 | 大天狗的座上宾 |
| > 10000 | 守矢神社的 VIP |
| > 2000 | 命莲寺的赞助者 |
| > 500 | 人间之里的小商贩 |
| 其他 | 初入幻想乡的无名氏 |

## 待办事项

- [x] SSE 实时数据驱动图表更新（连接时停用轮询，断线时降级为 6s 轮询）
- [x] 管理员市场管理：状态列动态显示、创建表单提交期间禁用输入
- [x] 用户资产/交易记录：增加错误状态（API 失败显示提示+重试按钮）
- [x] 持仓市值计算修正（使用后端 holdings_value，而非错误的 amount 累加）
- [x] 订单簿：移除不存在的 SSE 事件订阅，改为 API 轮询+优雅空状态
- [x] SystemMonitor 成交样本扩大（5→10 个市场，最多显示 50 条）
- [ ] 测试覆盖（Vitest + Playwright）
- [ ] 移动端响应式优化
