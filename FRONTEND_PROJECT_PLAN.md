# 东方炒炒币前端项目规划文档

## 项目概述

**项目名称**: 东方炒炒币前端 (Touhou Exchange Frontend)  
**技术栈**: Vue 3 + TypeScript + Naive UI  
**后端API**: 基于FastAPI的预测市场交易平台  
**项目目标**: 构建一个功能完整、用户体验优秀的预测市场交易前端界面

## 1. 技术选型

### 1.1 核心框架
- **Vue 3** - 最新版本，使用Composition API
- **TypeScript** - 类型安全，提高代码质量
- **Vite** - 下一代前端构建工具，开发体验优秀

### 1.2 UI组件库
- **Naive UI** - 功能丰富、设计优雅的Vue 3组件库
- **UnoCSS** - 原子化CSS引擎，样式开发高效
- **图标库** - 使用Naive UI内置图标或自定义图标

### 1.3 状态管理
- **Pinia** - Vue官方推荐的状态管理库
- **Vue Router 4** - 路由管理
- **Axios** - HTTP客户端，API请求封装

### 1.4 数据可视化
- **ECharts** - 强大的图表库
- **vue-echarts** - ECharts的Vue 3封装
- **Day.js** - 日期时间处理

### 1.5 开发工具
- **ESLint** - 代码质量检查
- **Prettier** - 代码格式化
- **TypeScript ESLint** - TypeScript代码检查

## 2. 项目目录结构

```
thccb-frontend/
├── public/                    # 静态资源
├── src/
│   ├── api/                  # API接口封装
│   │   ├── index.ts          # API配置和拦截器
│   │   ├── auth.ts           # 认证相关API
│   │   ├── market.ts         # 市场交易API
│   │   ├── user.ts           # 用户资产API
│   │   ├── chart.ts          # 图表数据API
│   │   └── stream.ts         # 实时流API
│   ├── assets/               # 静态资源
│   │   ├── css/              # 全局样式
│   │   │   ├── global.css    # 全局样式
│   │   │   ├── variables.css # CSS变量
│   │   │   └── theme.css     # 主题样式
│   │   └── images/           # 图片资源
│   │       ├── logo.png      # 项目Logo
│   │       └── icons/        # 图标资源
│   ├── components/           # 全局组件
│   │   ├── layout/           # 布局组件
│   │   │   ├── AppHeader.vue # 顶部导航
│   │   │   ├── AppSidebar.vue # 侧边栏
│   │   │   ├── AppFooter.vue # 页脚
│   │   │   └── Breadcrumb.vue # 面包屑导航
│   │   ├── market/           # 市场相关组件
│   │   │   ├── MarketCard.vue # 市场卡片
│   │   │   ├── OutcomeCard.vue # 选项卡片
│   │   │   ├── TradePanel.vue # 交易面板
│   │   │   ├── QuotePanel.vue # 预估面板
│   │   │   ├── OrderBook.vue # 订单簿
│   │   │   └── MarketStatus.vue # 市场状态
│   │   ├── chart/            # 图表组件
│   │   │   ├── PriceChart.vue # 价格图表
│   │   │   ├── CandleChart.vue # K线图
│   │   │   ├── DepthChart.vue # 深度图
│   │   │   └── ChartToolbar.vue # 图表工具栏
│   │   ├── user/             # 用户相关组件
│   │   │   ├── UserInfo.vue  # 用户信息
│   │   │   ├── HoldingsList.vue # 持仓列表
│   │   │   ├── TransactionHistory.vue # 交易历史
│   │   │   └── AssetSummary.vue # 资产概览
│   │   ├── admin/            # 管理员组件
│   │   │   ├── MarketForm.vue # 市场表单
│   │   │   ├── ActivationCodeList.vue # 激活码列表
│   │   │   └── SystemStats.vue # 系统统计
│   │   └── common/           # 通用组件
│   │       ├── Loading.vue   # 加载组件
│   │       ├── ErrorBoundary.vue # 错误边界
│   │       ├── EmptyState.vue # 空状态
│   │       ├── ConfirmDialog.vue # 确认对话框
│   │       └── Notification.vue # 通知组件
│   ├── composables/          # Vue组合式函数
│   │   ├── useAuth.ts        # 认证状态管理
│   │   ├── useMarket.ts      # 市场数据管理
│   │   ├── useTrade.ts       # 交易逻辑
│   │   ├── useChart.ts       # 图表数据处理
│   │   ├── useSSE.ts         # SSE实时流管理
│   │   ├── useForm.ts        # 表单处理
│   │   └── usePagination.ts  # 分页逻辑
│   ├── layouts/              # 页面布局
│   │   ├── DefaultLayout.vue # 默认布局（带导航）
│   │   ├── AuthLayout.vue    # 认证页面布局
│   │   ├── AdminLayout.vue   # 管理员布局
│   │   └── TradingLayout.vue # 交易页面布局
│   ├── pages/                # 页面组件
│   │   ├── auth/             # 认证页面
│   │   │   ├── Login.vue     # 登录页
│   │   │   ├── Register.vue  # 注册页
│   │   │   └── Activate.vue  # 激活页
│   │   ├── market/           # 市场页面
│   │   │   ├── MarketList.vue # 市场列表
│   │   │   ├── MarketDetail.vue # 市场详情
│   │   │   ├── TradingView.vue # 交易视图
│   │   │   └── Leaderboard.vue # 排行榜
│   │   ├── user/             # 用户页面
│   │   │   ├── Dashboard.vue # 仪表盘
│   │   │   ├── Portfolio.vue # 资产组合
│   │   │   └── Transactions.vue # 交易记录
│   │   ├── admin/            # 管理员页面
│   │   │   ├── MarketManage.vue # 市场管理
│   │   │   ├── ActivationCodes.vue # 激活码管理
│   │   │   └── SystemMonitor.vue # 系统监控
│   │   └── home/             # 首页
│   │       ├── Home.vue      # 首页
│   │       └── About.vue     # 关于页面
│   ├── router/               # 路由配置
│   │   ├── index.ts          # 路由主文件
│   │   ├── routes.ts         # 路由定义
│   │   └── guards.ts         # 路由守卫
│   ├── stores/               # Pinia状态管理
│   │   ├── auth.ts           # 认证状态
│   │   ├── market.ts         # 市场状态
│   │   ├── user.ts           # 用户状态
│   │   ├── ui.ts             # UI状态
│   │   └── notification.ts   # 通知状态
│   ├── types/                # TypeScript类型定义
│   │   ├── api.ts            # API接口类型
│   │   ├── market.ts         # 市场相关类型
│   │   ├── user.ts           # 用户相关类型
│   │   ├── chart.ts          # 图表相关类型
│   │   ├── trade.ts          # 交易相关类型
│   │   └── common.ts         # 通用类型
│   ├── utils/                # 工具函数
│   │   ├── auth.ts           # 认证工具
│   │   ├── formatter.ts      # 数据格式化
│   │   ├── validation.ts     # 表单验证
│   │   ├── constants.ts      # 常量定义
│   │   ├── helpers.ts        # 辅助函数
│   │   └── storage.ts        # 本地存储
│   ├── App.vue               # 根组件
│   └── main.ts               # 入口文件
├── index.html                # HTML模板
├── package.json              # 项目依赖
├── tsconfig.json             # TypeScript配置
├── vite.config.ts            # Vite配置
├── uno.config.ts             # UnoCSS配置
├── .eslintrc.js              # ESLint配置
├── .prettierrc               # Prettier配置
├── .env.example              # 环境变量示例
└── README.md                 # 项目说明
```

## 3. 核心功能模块

### 3.1 认证模块
- **用户登录** - JWT令牌获取和管理
- **用户注册** - 新用户注册流程
- **账号激活** - 使用激活码激活账号
- **自动刷新** - JWT令牌自动刷新机制
- **路由守卫** - 页面访问权限控制

### 3.2 市场交易模块
- **市场列表** - 展示所有活跃市场，支持搜索和筛选
- **市场详情** - 显示市场基本信息、选项和当前价格
- **交易面板** - 买入/卖出操作界面
- **交易预估** - 实时计算交易成本和收益
- **订单簿** - 显示市场深度和挂单信息
- **市场状态** - 显示市场状态（交易中、熔断、已结算）

### 3.3 图表数据模块
- **价格图表** - 分时图显示价格走势
- **K线图** - OHLCV K线图表，支持多种时间周期
- **深度图** - 市场深度可视化
- **技术指标** - 可选的技术分析指标
- **图表工具栏** - 时间周期切换、指标选择等

### 3.4 用户资产模块
- **资产概览** - 显示现金、负债、持仓市值和净值
- **持仓管理** - 查看和管理当前持仓
- **交易历史** - 查看历史交易记录
- **财富排名** - 用户财富排行榜
- **称号系统** - 根据净值显示不同称号

### 3.5 实时数据模块
- **SSE连接** - Server-Sent Events实时数据连接
- **市场更新** - 实时接收市场状态变化
- **交易推送** - 实时接收成交信息
- **价格更新** - 实时更新选项价格
- **断线重连** - 自动重连机制

### 3.6 管理员模块
- **市场管理** - 创建、关闭、结算市场
- **激活码管理** - 生成、查看、作废激活码
- **系统监控** - 查看平台统计数据
- **用户管理** - 用户信息查看和管理

## 4. 页面设计

### 4.1 首页 (Home)
- 平台介绍和特色功能展示
- 热门市场推荐
- 快速登录/注册入口
- 财富排行榜预览

### 4.2 登录/注册页 (Auth)
- 简洁的登录表单
- 用户注册流程
- 激活码激活功能
- 忘记密码（如有需要）

### 4.3 用户仪表盘 (Dashboard)
- 资产概览卡片
- 快速交易入口
- 最近交易记录
- 市场状态概览

### 4.3 市场列表页 (Market List)
- 市场卡片网格布局
- 搜索和筛选功能
- 排序选项（按创建时间、交易量等）
- 分页加载

### 4.4 市场详情/交易页 (Market Detail/Trading)
- **左侧栏**：市场信息和选项列表
- **中间区域**：价格图表（可切换分时/K线）
- **右侧栏**：交易面板和订单簿
- **底部区域**：成交记录和市场状态

### 4.5 资产组合页 (Portfolio)
- 资产分布图表
- 持仓列表（可展开查看详情）
- 持仓收益计算
- 快速交易入口

### 4.6 交易历史页 (Transactions)
- 交易记录表格
- 时间范围筛选
- 交易类型筛选
- 导出功能（如有需要）

### 4.7 管理员页面 (Admin)
- **市场管理**：创建市场表单、市场列表操作
- **激活码管理**：生成、查看、作废激活码
- **系统监控**：用户统计、交易统计、系统状态

## 5. 组件设计规范

### 5.1 组件命名规范
- PascalCase命名（如：MarketCard.vue）
- 目录使用kebab-case（如：market-card/）
- 组件前缀：业务领域 + 功能（如：MarketCard, UserInfo）

### 5.2 Props设计
- 使用TypeScript接口定义Props类型
- 提供合理的默认值
- 添加必要的验证
- 使用v-model实现双向绑定

### 5.3 状态管理
- 使用Pinia管理全局状态
- 组件内部状态使用ref/reactive
- 复杂逻辑使用Composition API封装
- 避免过度使用全局状态

### 5.4 样式规范
- 使用UnoCSS原子类为主
- 组件样式使用scoped CSS
- 主题变量统一管理
- 响应式设计使用断点

## 6. API集成方案

### 6.1 API层封装
```typescript
// api/index.ts
import axios from 'axios'
import type { AxiosInstance, AxiosRequestConfig } from 'axios'

const api: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000',
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 请求拦截器 - 添加认证令牌
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截器 - 统一错误处理
api.interceptors.response.use(
  (response) => response.data,
  (error) => {
    // 统一错误处理逻辑
    return Promise.reject(error)
  }
)

export default api
```

### 6.2 类型定义
```typescript
// types/market.ts
export interface Market {
  id: number
  title: string
  description: string
  liquidity_b: number
  status: 'trading' | 'halt' | 'settled'
  created_at: string
  winning_outcome_id?: number
  settled_at?: string
  settled_by_user_id?: number
}

export interface Outcome {
  id: number
  label: string
  total_shares: number
  current_price: number
  payout?: number
  is_winner?: boolean
}

export interface TradeRequest {
  outcome_id: number
  shares: number
}

export interface TradeResponse {
  shares: number
  cost: number
  new_cash: number
  message: string
}
```

### 6.3 状态管理
```typescript
// stores/market.ts
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Market, Outcome } from '@/types/market'
import { marketApi } from '@/api/market'

export const useMarketStore = defineStore('market', () => {
  const markets = ref<Market[]>([])
  const currentMarket = ref<Market | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  // Getters
  const activeMarkets = computed(() => 
    markets.value.filter(m => m.status === 'trading')
  )

  // Actions
  const fetchMarkets = async () => {
    loading.value = true
    error.value = null
    try {
      markets.value = await marketApi.getMarkets()
    } catch (err) {
      error.value = '获取市场列表失败'
      console.error(err)
    } finally {
      loading.value = false
    }
  }

  const fetchMarketDetail = async (marketId: number) => {
    loading.value = true
    try {
      currentMarket.value = await marketApi.getMarketDetail(marketId)
    } catch (err) {
      error.value = '获取市场详情失败'
      console.error(err)
    } finally {
      loading.value = false
    }
  }

  return {
    markets,
    currentMarket,
    loading,
    error,
    activeMarkets,
    fetchMarkets,
    fetchMarketDetail,
  }
})
```

## 7. 开发工作流

### 7.1 环境配置
```bash
# 开发环境
VITE_API_BASE_URL=http://localhost:8000
VITE_APP_TITLE=东方炒炒币

# 生产环境
VITE_API_BASE_URL=https://api.thccb.com
VITE_APP_TITLE=东方炒炒币
```

### 7.2 开发脚本