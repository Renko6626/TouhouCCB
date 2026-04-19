# TouhouCCB 前端代码审阅报告

> 审阅日期：2026-04-13
> 审阅范围：thccb-frontend/src 全部核心页面、组件、状态管理、API 层

---

## 一、架构与文件结构问题

### 死代码文件（未被任何模块引用）

| 文件 | 说明 |
|------|------|
| `components/icons/IconCommunity.vue` | Vue 脚手架残留，无引用 |
| `components/icons/IconDocumentation.vue` | 同上 |
| `components/icons/IconEcosystem.vue` | 同上 |
| `components/icons/IconSupport.vue` | 同上 |
| `components/icons/IconTooling.vue` | 同上 |
| `components/market/QuotePanel.vue` | 未被任何页面引用 |
| `components/chart/DepthChart.vue` | 未被任何页面引用 |
| `components/chart/ChartToolbar.vue` | 未被任何页面引用 |
| `api/api-client.ts` | 已在此次审阅中删除 |

### composables 使用情况

| 文件 | 引用方 |
|------|--------|
| `useSSE.ts` | TradingView.vue |
| `useChartData.ts` | CandleChart.vue, PriceChart.vue |
| `useMarketData.ts` | 未确认引用 |
| `useTradeLogic.ts` | 未确认引用 |
| `useFormatters.ts` | 未确认引用 |

---

## 二、交易视图 (TradingView) 审阅

### 严重问题

1. **PriceChart.vue 双重 ref 绑定（第137行 + 第166行）**
   - 外层容器和内层 div 同时绑定了 `ref="chartRef"`，ECharts 可能初始化到错误的元素上

2. **交易执行后无结果验证（TradingView.vue:216-238）**
   - `executeTrade()` 没有检查 API 返回是否成功，也没有向用户显示成功/失败反馈

3. **SSE 竞态条件（TradingView.vue:89-94）**
   - `watch(marketId)` 切换市场时没有先 `sse.disconnect()` 旧连接
   - 可能收到多个市场混杂的数据

4. **报价字段语义混乱（TradingView.vue:178-186）**
   - `cost: result.data.net` — 将 `net`（净成本）赋给 `cost`（总成本），语义不一致
   - TradePanel 只显示一个数字，用户看不到 gross/fee/net 的分解

### 高优先级

5. **移动端交易面板难以访问（TradingView.vue:268）**
   - 单列布局下交易面板在底部，需大量滚动
   - 建议：移动端改为底部 sticky 或 bottom sheet

6. **图表区域移动端过高（TradingView.vue:288）**
   - 480px 占移动端视口 60%+，看不到下方内容
   - 建议：改为 `h-[300px] sm:h-[400px] md:h-[560px]`

7. **TradePanel props 过多（8个props + 4个emits）**
   - `quoteResult: any` 和 `userHolding: any` 丧失类型检查
   - 建议：定义 TradeConfig 接口或使用 provide/inject

8. **交易按钮缺少关键保护**
   - 没有二次确认弹窗（交易不可撤销）
   - 没有防双击机制

### 中优先级

9. **三个完全相同的 SSE 处理函数（TradingView.vue:108-124）**
   - `handleRealtimeSnapshot`、`handleRealtimeTrade`、`handleRealtimeStatus` 逻辑一模一样
   - 应合并为一个 `handleRealtimeEvent`

10. **OutcomeCard 价格变动计算假设基准价 0.5（OutcomeCard.vue:13-17）**
    - `(current_price - 0.5) / 0.5 * 100` 硬编码基准
    - 应使用实际的历史价格或 previous_price

11. **OrderBook 不跟随 SSE 实时更新（OrderBook.vue:25-42）** — ⚠️ **作废（2026-04-19）**
    - 原审阅项：组件只在挂载时加载一次
    - **后续判定**：项目使用 LMSR 自动做市商架构，没有"挂单簿"概念，后端从未实现 `/orderbook` 接口；该组件从未被任何页面引用，本次代码清理已删除 `OrderBook.vue` 与 `OrderBookEntry` 类型

12. **CandleChart 的 autoRefreshMs prop 变化时旧 timer 不清除**

13. **交易后 quoteResult 未清除（TradingView.vue:222）**

14. **loadUserData 串行请求（TradingView.vue:58-63）**
    - `fetchSummary()` 和 `fetchHoldings()` 应该用 `Promise.all` 并行

15. **n-empty 未导入（TradingView.vue:399）**
    - 使用小写 kebab-case 标签但未从 naive-ui 导入

16. **快捷数量按钮硬编码 [1,5,10,25,50,100]（TradePanel.vue:50）**
    - maxShares 为 3 时大部分不可用，应动态生成

---

## 三、用户资产页面 (Portfolio / Transactions) 审阅

### 严重问题

1. **loading 状态在 fetchAllUserData 中混乱（user.ts:34-92）**
   - 三个 fetch 函数各自设 loading=true/false
   - `Promise.all` 并发时 loading 状态会在不同时间被反复切换
   - 应在 fetchAllUserData 中统一管理

2. **N+1 查询：Portfolio 表格 render 函数重复遍历（Portfolio.vue:77-110）**
   - 每行每列都 `marketStore.markets.find()`，同一行查了 4 次
   - 应建立 `marketId -> market` 的 Map 缓存

3. **totalHoldingsValue 数据不一致风险（user.ts:16）**
   - 取自后端 `summary.holdings_value`，但前端 holdings 列表可能已过期
   - 两处数据来源不同，可能不一致

### 高优先级

4. **持仓表格缺少关键列**
   - 没有「买入均价」和「盈亏」，对交易决策几乎无参考价值

5. **交易记录缺少关键信息**
   - 没有关联的市场名称、选项名称

6. **移动端适配不足**
   - NDataTable 7 列总宽 ~960px，无 `scroll-x` 配置
   - Transactions.vue 完全没有 @media 响应式

### 中优先级

7. **错误处理混乱**
   - store 和页面组件双重管理 loading/error 状态

8. **holdingsByMarket 转换重复**
   - store 里算 Map，页面里再转数组，应在 store 中统一

---

## 四、首页与市场列表 (Home / MarketList / Leaderboard) 审阅

### 严重问题

1. **分页未重置：筛选条件变化时 currentPage 不归 1（MarketList.vue）**
   - 搜索后仍停留在旧页码，可能显示空结果

2. **热门市场排序无意义（Home.vue:26）**
   - `activeMarkets.slice(0, 4)` 只取前4个，没有按交易量/活跃度排序

3. **统计数据重复（Home.vue:28-33）**
   - 「活跃市场」和「交易中」数据重复
   - 缺少有价值的数据（总交易额、用户数）

### 高优先级

4. **MarketCard 缺少决策信息**
   - 没有最后成交时间、成交笔数、用户持仓

5. **排序功能完全缺失（MarketList.vue）**
   - 无法按流动性、创建时间、活跃度排序

6. **Leaderboard 无排名变化指示**

### 中优先级

7. **平台特色区域不可交互（Home.vue:110-143）**
   - 纯文字展示，没有链接到对应功能

8. **搜索无防抖（MarketList.vue:36-48）**

---

## 五、布局与管理后台 (Layout / Admin) 审阅

### 严重问题

1. **全局错误边界完全缺失（App.vue）**
   - 组件运行时错误会导致白屏

2. **MarketManage 结算操作无二次确认（MarketManage.vue:144-158）**
   - 结算不可撤销却无确认弹窗

3. **路由过渡动画缺失（App.vue:96）**
   - 有过渡样式定义（133-141行）但未应用

### 高优先级

4. **DefaultLayout 侧边栏正则匹配效率低（DefaultLayout.vue:24-27）**
   - 运行时创建正则，应改用 `route.name` 判断

5. **移动端侧边栏无关闭机制（DefaultLayout.vue:92-102）**
   - fixed 定位后遮挡内容，无法关闭

6. **AppFooter 链接无功能（AppFooter.vue:10-14）**
   - `<span>` 标签带 pointer 光标，但不可点击

### 中优先级

7. **面包屑只有 2 级（DefaultLayout.vue:60-69）**
   - 嵌套路由如 /admin/market-manage 只显示「首页 > 市场管理」

8. **通知系统重复**
   - MarketManage 用 `message.error()`，App.vue 用 `useNotificationStore()`
   - 两套通知系统共存

9. **创建市场选项用数组索引做 key（MarketManage.vue:272-280）**
   - 删除后焦点可能错乱

---

## 六、已修复问题（2026-04-13 本次会话）

| 问题 | 修复文件 | 说明 |
|------|----------|------|
| CRITICAL: 开放重定向 | Callback.vue | 添加 sanitizeRedirect() |
| HIGH: 环境变量未校验 | casdoor.ts | 启动时检查 VITE_CASDOOR_* |
| HIGH: OAuth state 未校验 | casdoor.ts + Callback.vue | 生成 nonce + 回调比对 |
| MEDIUM: 401 重复跳转 | api/index.ts + auth.ts | isLoggingOut 锁 |
| MEDIUM: 报价无防抖 | TradingView.vue | 400ms debounce |
| MEDIUM: maxShares 兜底 100 | TradingView.vue | 改为 0 |
| MEDIUM: SSE 事件无校验 | stream.ts | isValidMarketEvent 类型守卫 |
| LOW: JSON parse 静默失败 | auth.ts | 添加 console.warn |
| LOW: 死代码 api-client.ts | 已删除 | — |

### 第二轮修复（核心页面审阅后）

| 问题 | 修复文件 | 说明 |
|------|----------|------|
| 11 个死代码文件 | 已删除 | 5 icons + QuotePanel + DepthChart + ChartToolbar + 3 composables |
| PriceChart 双重 ref | PriceChart.vue | 移除外层 ref，ECharts 只初始化到内层元素 |
| 3 个相同的 SSE 处理函数 | TradingView.vue | 合并为 handleRealtimeEvent |
| SSE 切换市场未断开旧连接 | TradingView.vue | watch(marketId) 中先 disconnect |
| 交易执行无反馈 | TradingView.vue | 添加 message.success/error |
| 交易后 quoteResult 未清除 | TradingView.vue | 重置为 null |
| loadUserData 串行请求 | TradingView.vue | 改为 Promise.all 并行 |
| 交易中 SSE 刷新导致数据不一致 | TradingView.vue | tradeLoading 时跳过自动刷新 |
| 图表移动端过高 480px | TradingView.vue | 改为 300/400/560 三档 |
| n-empty 未导入 | TradingView.vue | 改为 NEmpty + 导入 |
| 分页未重置 | MarketList.vue | watch 筛选条件 → currentPage = 1 |
| Portfolio N+1 查询 | Portfolio.vue | 建立 marketById/outcomeById Map 缓存 |
| 表格移动端溢出 | Portfolio.vue | NDataTable 添加 scroll-x=960 |
| 路由过渡动画缺失 | App.vue | RouterView 包裹 Transition |
| 侧边栏正则低效 | DefaultLayout.vue | 改用 route.name 判断 |
| Footer 死链接 | AppFooter.vue | 移除不可点击的假链接 |
| 结算无二次确认 | MarketManage.vue | submitSettle + directSettle 添加 dialog.warning |

### 第三轮修复（剩余问题清扫）

| 问题 | 修复文件 | 说明 |
|------|----------|------|
| TradePanel quoteResult/userHolding 用 any | TradePanel.vue | 改为 QuoteResponse/Holding 具体类型 |
| TradingView quoteResult 用 any | TradingView.vue | 改为 ref\<QuoteResponse \| null\> |
| 报价字段语义混乱 (cost=net) | TradingView.vue + TradePanel.vue | 去掉重映射，TradePanel 展示 gross/fee/net 三行 |
| 交易按钮无防双击 | TradePanel.vue | isSubmitting 锁 + 500ms 延迟解锁 |
| 快捷份额按钮硬编码 | TradePanel.vue | 根据 maxShares 动态生成有意义的档位 |
| OutcomeCard 价格变动假设基准 0.5 | OutcomeCard.vue | 改为显示 LMSR 隐含概率百分比 |
| ~~OrderBook 不跟随 SSE 刷新~~ | ~~OrderBook.vue + TradingView.vue~~ | ⚠️ 作废 (2026-04-19)：LMSR 架构无订单簿，组件已删除 |
| user.ts fetchAllUserData loading 混乱 | user.ts | 添加 manageLoading 参数，并发调用时传 false |
| Home 统计数据重复 | Home.vue | 去掉重复的「活跃市场」，改为「已暂停」 |
| 特色区域不可交互 | Home.vue | feature-card 添加 click 跳转 + hover 样式 |
| MarketList 排序功能缺失 | MarketList.vue | 添加流动性/选项数排序下拉 + watch 重置页码 |
| 全局错误边界缺失 | App.vue | onErrorCaptured + 工业风错误页面 |
| 移动端侧边栏无关闭 | DefaultLayout.vue | 遮罩层点击关闭 + 路由切换自动收起 |
| 面包屑只有 2 级 | DefaultLayout.vue | 从 route.matched 动态生成多级面包屑 |
| 创建市场选项用索引做 key | MarketManage.vue | 改为带自增 id 的对象数组 |

### 仍需后端配合的问题（前端无法单独修复）

| 问题 | 说明 |
|------|------|
| 持仓表格缺少买入均价/盈亏 | 后端 API 未提供 average_cost/pnl 字段 |
| 交易记录缺少市场名/选项名 | 后端 Transaction 无关联的 market_title/outcome_label |
| MarketCard 缺少成交笔数/最后成交时间 | 后端 MarketListItem 无 trade_count/last_trade_at |
| Leaderboard 无排名变化指示 | 后端无历史排名对比数据 |
| 热门市场按热度排序 | 后端无交易量/活跃度排序字段 |
