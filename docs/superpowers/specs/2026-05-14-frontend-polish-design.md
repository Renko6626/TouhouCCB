# 前端文案 + 样式打磨 + TOS 同意机制 — 设计文档

> 日期：2026-05-14
> 范围：thccb-frontend 全站文案/样式/微交互 + 用户首次进入前的免责声明知情同意（含后端 schema 加列）
> 不动：定价/交易业务逻辑、术语（「买入/卖出份额」「预测市场」保持不动）、UI 库

---

## 1. 背景与原则

现有前端已落地工业风（黑白、无圆角、粗边框、6px 硬阴影）和东方主题骨架（项目名 / hero / 排行榜称号），但**点缀稀疏、规范执行不一致、品牌定位偏"交易平台"**。同时，目前用户进入站点前**没有强制的知情同意环节**，rulebook.tex 文末的免责声明仅作为 PDF 文档存在。

本次打磨四条主线：

1. **品牌定位降风险**：全站 tagline 从「交易平台」改为「学习平台」，明示这是 LMSR 预测市场的学习/模拟环境
2. **TOS 知情同意**（新增）：首次进入交易前弹出阻塞式免责声明 Modal，用户主动勾选同意；后端记 `users.tos_accepted_at` 留存证据链
3. **style.md 执行收口**：补 `tabular-nums`、消灭 `dashed` / `border-radius > 0` / 硬编码 `#d14` 三类违规
4. **东方氛围 4 处点缀**：仅在 404 / hero / 空状态 / 错误兜底 4 个非主流程位置加入轻量东方元素

**护栏**：
- 不出现「博彩 / 赌博 / 押注 / 庄家 / 盘口 / 投注」等敏感词（已确认全站零命中，需在后续 review 保持零）
- 不引入人物名（灵梦/魔理沙等）到主交互文案，避免对非粉丝不友好
- 涨绿跌红是金融惯例例外，其他通用 UI 守黑白灰
- schema 加列走 alembic autogenerate 流程，不裸改（CLAUDE.md 红线）

---

## 2. 品牌 tagline 全站统一

**所有「预测市场交易平台」改为「预测市场学习平台」**，并把"模拟"前置以强化教育属性。

| 文件 | 当前文案 | 改后文案 |
|---|---|---|
| `Home.vue:140-143`（未登录 hero desc） | `基于 LMSR 算法的模拟预测市场交易平台。<br>交易您对幻想乡事件的判断，让市场发现真实概率。` | `基于 LMSR 的预测市场学习平台。<br>用模拟资金交易对幻想乡事件的判断，理解市场如何发现真实概率。` |
| `AuthLayout.vue:20` brand-tagline | `预测市场交易平台` | `预测市场学习平台` |
| `AppFooter.vue:10-11` footer-tagline | `预测市场交易平台` | `预测市场学习平台` |
| `Home.vue:138` hero-eyebrow | `预测市场 · 东方 Project` | `预测市场学习 · 东方 Project` |

理由：
- 「学习平台」+「模拟资金」清晰传达非真金性质
- "幻想乡事件"保留——这是东方氛围的核心入口

---

## 3. TOS 知情同意机制（新增功能）

### 3.1 动机
仅在页脚挂静态规则手册链接的合规价值有限。必须由用户在进入主交易流之前**主动勾选同意**，并在服务端留存时间戳，才能构成可援引的知情同意凭证。rulebook.tex L741-780 已写好完整的免责声明，本节把它压缩到 600 字以内做成 Modal。

### 3.2 用户流程

```
登录成功（Casdoor 回调） → /auth/me 拿到 user 对象
                              ↓
            判断 user.tos_accepted_at == null ?
                ↓ 是                      ↓ 否
        前端阻塞性弹出 TosModal       正常进入应用
                ↓
        用户勾选「我已阅读并同意」
                ↓
        点「同意并继续」按钮
                ↓
        POST /api/v1/users/me/accept-tos
                ↓
        后端写 tos_accepted_at = now()
                ↓
        前端刷新 user store → 关闭 Modal → 放行
```

**关键规则**：
- 新老用户都走同一逻辑：老用户首次部署后下次登录会强制补勾一次（`tos_accepted_at` 默认 null）
- Modal **不可关闭**：无 ESC、无遮罩点击关闭、无右上角 X；底部只有「同意并继续」一个按钮，灰态直到 checkbox 勾选
- 路由白名单：`/auth/login`、`/auth/callback`、`/auth/register` 不触发（避免登录页就弹）；其他需登录页全部触发
- 同意后**永久有效**，下次登录不再弹
- 不在后端硬卡接口（避免与 SSE/auth 等接口耦合），由前端门禁；后端只负责写入和回显

### 3.3 后端改动

**Schema 加列**（走 alembic，**红线动作 — 需用户授权**）：

```python
# backend/app/models/base.py User 模型
tos_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
```

迁移：
```bash
alembic revision --autogenerate -m "add users.tos_accepted_at"
# review 生成的迁移文件，确认只是 ADD COLUMN nullable
alembic upgrade head  # 本地先跑
```

**API**：`POST /api/v1/users/me/accept-tos`
- 路径：`backend/app/api/v1/users.py`（或 auth.py，取近的）
- 鉴权：需登录
- 行为：`UPDATE users SET tos_accepted_at = now() WHERE id = current_user.id AND tos_accepted_at IS NULL`
- 幂等：已同意者不报错，返回当前 `tos_accepted_at`
- 响应：`{"tos_accepted_at": "2026-05-14T..."}`
- 限速：复用 `/auth` 5r/s（防止异常重复 POST）

**Pydantic schemas 调整**：
- `UserOut` / `UserMe` schema 增加 `tos_accepted_at: datetime | None` 字段
- 现有 `/auth/me`（或等价端点）回传新字段

**测试**：
- `test_accept_tos_first_time_writes_timestamp`：null → 调用 → 非 null
- `test_accept_tos_idempotent`：连续调两次，第二次不报错且时间戳不变
- `test_accept_tos_requires_auth`：未登录 401

### 3.4 前端改动

**新组件**：`thccb-frontend/src/components/legal/TosModal.vue`
- 工业风：4px 黑实框 + 8px 硬阴影、内层 1px 细线装饰、无圆角
- 标题区黑底白字「东方炒炒币 — 用户须知」
- 正文区滚动容器（max-height 50vh），含 §3.5 法律文案
- 底部固定区：checkbox + 「同意并继续」按钮（disabled 直到勾选）
- 不响应 ESC、不响应遮罩点击关闭

**集成位置**：`DefaultLayout.vue`
- 顶层挂载一个 `<TosModal v-if="authStore.needsTos" />`
- `authStore` 加 computed `needsTos`：`isAuthenticated && user && user.tos_accepted_at == null`
- 在路由 `meta.requiresAuth` 为 true 的页面下生效；登录/回调页因走 AuthLayout 不走 DefaultLayout，天然白名单

**Store 改动**：`stores/auth.ts`
- `User` 类型加 `tos_accepted_at: string | null`
- 新方法 `acceptTos()`：调 API → 更新 `user.tos_accepted_at`
- `/auth/me` 拉取时自动包含该字段（依赖后端响应已含）

**新 API client**：`api/users.ts`
- `acceptTos(): Promise<{ tos_accepted_at: string }>`

### 3.5 弹窗法律文案（压缩自 rulebook.tex L747-780，约 580 字）

```
东方炒炒币 — 用户须知

本网站为基于东方 Project 的非官方、非营利同人社团项目，
与 ZUN 及任何商业实体无关联。继续使用即视为您已阅读并同意以下条款：

1. 性质
   本系统为同好趣味活动与 LMSR 预测市场机制学习演示，不是真实
   金融交易平台。所有"买入/卖出/盈亏/利息/清算"等表述均为游戏
   内数值或教学类比。

2. 不涉及真实财物
   本系统不接受任何形式的真实金钱充值、押金、对价，不支付任何
   现金、返利、利息、分红、奖酬。系统中的盈亏、排行榜不代表
   现实损益。

3. 不构成金融服务
   本系统不提供证券、期货、基金、外汇、贷款、支付、虚拟货币
   交易或任何需许可的金融服务，不构成投资建议或收益承诺。

4. 不构成博彩活动
   本系统不组织或便利任何真实金钱投注，不设可兑换现金/财物/
   积分权益的游戏结果。用户在系统中的任何数值均无现实价值。

5. 使用边界
   用户不得将本系统用于现实金钱结算、账号交易、奖品兑换、商业
   推广、融资或任何违反法律的用途。任何人将本系统中的价格、
   模型或策略示例套用于现实金融或高风险行为，后果自负。

6. 未成年用户
   未成年人应在监护人知情同意下参与。如所在地区对模拟交易/
   同人活动有特殊限制，请自行遵循当地法规。

完整版本见 规则手册（链接到 /docs/rules 或 rulebook.pdf）。

☐ 我已阅读并同意以上条款

[ 同意并继续 ]
```

> 注：第 4 条用了"不构成博彩活动"——这是**否定式**陈述（声明本系统不是博彩），不是赞美/邀请用词，法律性能上比回避不提更强。这是合规弹窗里允许且必要的用法（rulebook 原文 L773 也是同样表述）。

### 3.6 不做的事
- 不在每次登录都强制重新同意（一次写入永久有效）
- 不做版本化 TOS（暂不加 `tos_version` 列；如未来条款大改再说）
- 不卡后端接口（避免破坏 SSE 等长连接）
- 弹窗不做国际化（站点本身就是单语）

---

## 4. 东方氛围点缀（共 4 处）

**只在低频接触的边角点缀，不污染主交易流。** 每处不超过 1 行小字。

### 4.1 NotFound 404 副标

`NotFound.vue:11`

```diff
- <p class="nf-message">页面未找到</p>
+ <p class="nf-message">页面未找到</p>
+ <p class="nf-flair">— 似乎迷失在了迷途竹林 —</p>
```

样式：`font-size: 11px; color: #aaa; letter-spacing: 0.08em; margin-top: -16px; margin-bottom: 24px;` —— 比主消息更弱化的灰色辅助文。

### 4.2 首页 hero（已在 §2 中处理，"幻想乡事件"已是天然落点）

### 4.3 Home 空市场状态

`Home.vue:187-192`

```diff
- <div v-else class="empty-markets">
-   <p>暂无活跃市场</p>
+ <div v-else class="empty-markets">
+   <p class="empty-title">博丽神社香火稀疏</p>
+   <p class="empty-sub">当前没有活跃市场</p>
    <NButton v-if="authStore.isAdmin" type="primary" ...>创建市场</NButton>
  </div>
```

样式：title 14px/700/uppercase；sub 12px/#888。

### 4.4 TradingView 加载失败兜底

`TradingView.vue` 加载失败 NAlert 处（参考 sub-agent 报告 ~556-563）：

```diff
- 加载失败，请重试
+ 情报丢失在结界中，请重试
```

仅这一处错误用东方化语气。其他错误（401/403/网络断）保持平实。

---

## 5. 样式打磨（按规范分类）

### 5.1 `tabular-nums` 补齐（所有数字数据列必须等宽）

> 规范来源：style.md L43 `数据数字 → font-variant-numeric: tabular-nums`

| 文件:位置 | 现状 | 改法 |
|---|---|---|
| `OutcomeCard.vue` `.outcome-value` (L93-95) | 无 | 加 `font-variant-numeric: tabular-nums` |
| `OutcomeCard.vue` `.outcome-value--bold` (L97-100) | 无 | 同上 |
| `Portfolio.vue` `.asset-value` (L316-322) | 已有 ✓ | 无需改 |
| `Transactions.vue` 时间戳/金额列 | 缺失 | 表头列加 `align: 'right'` 时间戳列 cell 加 inline `font-variant-numeric: tabular-nums` |
| `MyRedemptions.vue` 日期/金额 | 缺失 | 同上 |
| `Leaderboard.vue` "净值" 列 render (L39) | 缺失 | render 用 `h('span', { style: { fontVariantNumeric: 'tabular-nums' } }, ...)` |
| `Movers.vue` / `RecentTrades.vue` 价格/份额列 | 部分缺失 | 列 cell 补 inline style |

> 实施建议：在 `assets/styles/` 全局加一个 `.tabular { font-variant-numeric: tabular-nums }` 工具类，避免散点重复。或直接给 `.asset-value` 等 token 化类挂上。

### 5.2 `border-radius: 0` 强制（消灭残留圆角）

> 规范来源：style.md L255 ❌ 任何 `border-radius > 0`

| 位置 | 现状 | 改法 |
|---|---|---|
| `Home.vue:457` `.pnl-skeleton` | `border-radius: 2px` | 删除该行 |
| `Home.vue:465` `.pnl-percent-skeleton` | `border-radius: 2px` | 删除该行 |

全局再 grep 一次 `border-radius:\s*[1-9]` 确保只剩这两处。

### 5.3 `dashed` → `solid`（虚线违反工业风）

| 位置 | 现状 | 改法 |
|---|---|---|
| `Home.vue:596` `.empty-markets` | `border: 2px dashed #cccccc` | `border: 2px solid #cccccc` |
| `BatchDetail.vue` `.code-box`（如确认存在） | `dashed` | `solid 2px #000` |

### 5.4 颜色 token 化（消灭硬编码红色）

> `:root` 中已定义 `--color-down: #dc2626`，禁止再用 `#d14`

| 位置 | 现状 | 改法 |
|---|---|---|
| `AppHeader.vue:233-244` `.debt-badge` | `border: 2px solid #d14; color: #d14;` 等 | 全部换 `var(--color-down)` |
| `Portfolio.vue:336-337` `.asset-value-debt` | `color: #d14;` | `color: var(--color-down);` |
| `Loan.vue` `.debt-number.red` | `color: #d14;` | `color: var(--color-down);` |
| `Home.vue:440` `.pnl-stat-debt-value` | `color: #d14;` | `color: var(--color-down);` |

实施：grep `#d14` 一次性扫净。

### 5.5 标签文本 uppercase 一致性

> 规范来源：style.md L42 `辅助/标签 → text-transform: uppercase; letter-spacing: 0.06em`

| 位置 | 现状 | 改法 |
|---|---|---|
| `OutcomeCard.vue:88-91` `.outcome-label` | 无 uppercase | 加 `text-transform: uppercase; letter-spacing: 0.06em;` |
| `AuthLayout.vue:163-169` `.auth-link` | 无 uppercase | 改为 12px / `text-transform: uppercase; letter-spacing: 0.08em;` 加 underline-offset |

注意：标签字号建议同时降到 11–12px、color #888，避免 uppercase 后视觉太重。

---

## 6. 文案精修（按页面）

### 6.1 首页 Home.vue

| 位置 | 现状 | 改后 | 原因 |
|---|---|---|---|
| L95 (pnl-percent flat) | `暂无持仓，去市场建立你的第一笔仓位` | `暂无持仓，去市场开启第一笔预测` | 删"仓位"（金融化）、"建立"换"开启"更自然 |
| L117 (pnl-note) | `* 仅含未结算持仓的浮动盈亏，不含已结算收益` | 保持 ✓ | OK |
| L215-218 feature-card 描述 | `基于对数市场评分规则，价格随买卖动态调整，始终保证流动性。` | 保持 ✓ | OK |
| L223-227 feature-card 描述 | `SSE 实时流推送，成交瞬间刷新价格图表与持仓数据。` | 保持 ✓ | OK |
| L233-236 feature-card 描述 | `从「无名氏」到「大天狗的座上宾」，用净值竞逐幻想乡排名。` | 保持 ✓ | 这已经是东方点缀，留 |

### 6.2 MarketList.vue

| 位置 | 现状 | 改后 | 原因 |
|---|---|---|---|
| L130 placeholder | `搜索市场名称...` | `搜索市场名称…` | 三点 → 省略号字符 |
| L168 loading | `加载市场中...` | `正在加载…` | 文风更精简 |
| L62 错误 fallback | `加载市场失败` | 保持 ✓ | |
| L192 NEmpty | `没有找到匹配的市场` | `没有匹配的市场` | 删"找到"冗余 |

### 6.3 Leaderboard.vue

| 位置 | 现状 | 改后 | 原因 |
|---|---|---|---|
| L76 page-bar-title | `财富排行榜` | `排行榜` | "财富"冗余，与下方副标"净值排名"重复 |
| L77 page-bar-sub | `平台净值排名` | `按净资产排名` | "平台"冗余 |
| L88 loading | `加载排行榜中...` | `正在加载…` | 精简 |

### 6.4 TradingView.vue / TradePanel.vue / OutcomeCard.vue

| 位置 | 现状 | 改后 | 原因 |
|---|---|---|---|
| `TradePanel.vue` 滑点 label | （现状有 tooltip 但无视觉指示） | 加 `cursor: help; text-decoration: underline dotted;` | 用户不知道 hover 有提示 |
| `TradePanel.vue` disabled 执行按钮 | 无 title | 加 `:title="disabledReason"` 计算属性（如「请填写份额」「资金不足」等具体原因） | 用户不知为何禁用 |
| `TradePanel.vue` (~L250) 数量上限提示 | `最大 N (估)` | `最大 N（预估）` | 全角括号更工业风，"估"换"预估" |
| `OutcomeCard.vue:16-21` heatLabel | `热门 / 中性 / 冷门` | 保持 ✓（sub-agent 提议改单字，不采纳——会损害可读性） | 不动 |
| `TradingView.vue` 加载文案 | `加载市场数据中...` | `正在加载市场…` | 精简 |
| `TradingView.vue` 加载失败弹窗 | 通用错误文案 | （见 §4.4 东方氛围点缀） | |

### 6.5 用户中心

| 文件:位置 | 现状 | 改后 | 原因 |
|---|---|---|---|
| `Portfolio.vue:148` loading | `加载资产数据中...` | `正在加载…` | 精简 |
| `Portfolio.vue:185` debt-card title | `点击跳转借款页` | `点击查看负债详情` | "跳转借款页"过技术 |
| `Portfolio.vue:250` empty | `暂无持仓记录` | `暂无持仓` | 删"记录" |
| `Portfolio.vue:252` CTA | `去市场交易` | `去市场看看` | 与"学习平台"调性一致 |
| `Transactions.vue` 空状态 | `暂无符合条件的交易记录` | `暂无相关交易` | 精简 |
| `MyRedemptions.vue:49` 空状态 | `尚未兑换任何码` | `尚未兑换任何兑换码` | "任何码"歧义 |

### 6.6 借贷 / 兑换

| 文件:位置 | 现状 | 改后 | 原因 |
|---|---|---|---|
| `Loan.vue:83` title | `借款功能已关闭` | `借款功能维护中` | "维护中"更友好；若产品意图是永久关闭则保留原文 |
| `BatchDetail.vue` 免责行（含 ⚠ 字符处） | `⚠ 兑换由...不承担责任。` | 用 `<span class="warning-tag">注意</span>` 替代 ⚠ 字符 | emoji/特殊字符违反工业风纯净度 |
| `RedemptionList.vue:40` 空状态 | `暂无可兑换批次` | 保持 ✓ | 已经精简 |

### 6.7 认证 / 错误页

| 文件:位置 | 现状 | 改后 | 原因 |
|---|---|---|---|
| `Callback.vue:46` | `正在完成登录...` | `正在登录…` | 精简 |
| `Callback.vue:28`（如示该文案） | `缺少授权码，请重新登录。` | `登录失败，请重试` | 不暴露 OAuth 术语 |
| `AuthLayout.vue:45` 返回首页 | `← 返回首页` | 保持 ✓（与 §5.5 uppercase 一起处理） | |
| `NotFound.vue` | （见 §4.1） | | |

---

## 7. 微交互（非样式非文案的体验改进）

| 位置 | 改动 | 原因 |
|---|---|---|
| `TradePanel.vue` 选项下拉框 | 加 placeholder `请选择预测结果` 或顶部 hint label | 新用户不知道这是必填项 |
| `TradePanel.vue` 滑点 label | `cursor: help` + dotted underline（见 §5.5） | 视觉提示有 tooltip |
| `TradePanel.vue` disabled 提交按钮 | 动态 `title` 显示禁用原因（见 §6.4） | 用户清楚障碍在哪 |
| `MarketCard.vue:67`（标题） | 已有 `:title="market.title"` ✓ | OK |
| `Movers.vue` loading 态 | 加 skeleton 占位（沿用 Home.vue 的 `.skeleton-card` 风格） | 当前空白突兀 |
| `RecentTrades.vue` loading 态 | 同上 | |

---

## 8. 实施切分（commit 粒度）

**分支名**：`feat/2026-05-14-frontend-polish-and-tos`

按"一个可独立回滚的改动 = 一条 commit"切分。**注意**：commit 1（schema 加列）触发 CLAUDE.md 红线"动 schema 加列"，**开始前需用户授权动手**。

### 后端先行（TOS 同意机制）

1. `feat(db): users 表加 tos_accepted_at 列（alembic）` — alembic autogenerate + 本地 upgrade head 验证
2. `feat(api): POST /users/me/accept-tos + UserOut 加 tos_accepted_at 字段` — 含 3 个 pytest
3. （部署到 staging/生产时需先跑 `alembic upgrade head`；deploy.sh 已包含该步骤，无需额外改动）

### 前端品牌 + TOS 集成

4. `feat: tagline 全站统一为「预测市场学习平台」` (§2，3 处)
5. `feat: TosModal 组件 + DefaultLayout 集成 + auth store acceptTos` (§3.4)

### 前端样式收口

6. `style: 补齐 tabular-nums + 消灭 border-radius 残留` (§5.1 + §5.2)
7. `style: dashed → solid + 颜色 token 化` (§5.3 + §5.4)
8. `style: 标签 uppercase 一致性` (§5.5)

### 前端东方点缀 + 文案精修

9. `feat: 东方氛围 4 处点缀（404 / hero 已在 §2 / 空市场 / 错误兜底）` (§4)
10. `style: 文案精修（首页 + 市场列表 + 排行榜）` (§6.1-6.3)
11. `style: 文案精修（交易页 + 用户中心 + 借贷兑换）` (§6.4-6.6)
12. `style: 文案精修 + 微交互（认证 + 微交互）` (§6.7 + §7)

每条 commit 走 `🔍 声称完成前必跑` 三件套（前端：type-check + lint + build；后端：py_compile + import + pytest）。最后合 main 前再 UI 实测核心流程 + TOS 流程。

---

## 9. 验证清单（声称完成前必跑）

**后端自动化**：
- [ ] `python -m py_compile $(find app -name '*.py')` 通过
- [ ] `python -c "import app.main"` 通过
- [ ] `pytest -x` 通过（含新增 3 个 test_accept_tos）
- [ ] `alembic upgrade head` 在干净 DB 上跑通
- [ ] `alembic downgrade -1 && alembic upgrade head` 验证迁移可逆

**前端自动化**：
- [ ] `npm run type-check` 通过
- [ ] `npm run lint` 通过
- [ ] `npm run build` 通过
- [ ] `grep -riE "(博彩|赌博|押注|赌局|庄家|盘口|投注)" thccb-frontend/src --include="*.vue" --include="*.ts"` 仍然零命中（**注意**：TosModal 文案中"博彩"会出现于"不构成博彩活动"否定式陈述，这是允许的；需在 grep 后人工 review，确认仅在 TosModal 命中）
- [ ] `grep -rE "border-radius:\s*[1-9]" thccb-frontend/src --include="*.vue" --include="*.css"` 仅命中可解释的特例（如 0）
- [ ] `grep -rE "#d14\b" thccb-frontend/src --include="*.vue"` 零命中（除潜在 svg 等不相关位）
- [ ] `grep -rE "border:.*dashed" thccb-frontend/src --include="*.vue"` 零命中

**浏览器实测**（CLAUDE.md 硬要求）：
- [ ] **TOS Modal**：老用户首次登录强制弹出、未勾选时按钮 disabled、勾选后可同意、同意后刷新页面不再弹、ESC/遮罩点击不能关闭
- [ ] 首页未登录态、登录态（含 PnL hero）
- [ ] 市场列表筛选/搜索/排序、空状态、错误态
- [ ] 排行榜加载
- [ ] 交易页买入/卖出报价、错误兜底
- [ ] 我的资产、交易记录、兑换记录、借贷
- [ ] 404、错误边界、登录回调
- [ ] 移动端宽度（375px / 480px / 768px），含 TosModal 在窄屏可读

---

## 10. 不在本次范围

- ❌ 重命名"买入/卖出"等核心动作术语
- ❌ 重写组件结构、抽取新公共组件（除 TosModal 这个新组件）
- ❌ 修改路由 / store / API 层（除 TOS 相关新增的 `/users/me/accept-tos` 和 store 方法）
- ❌ 引入新 UI 库或图标库
- ❌ 升级依赖
- ❌ 后端字段补充（持仓均价/盈亏在 frontend-review 中已标记为后端依赖）
- ❌ 黑暗模式 / 国际化 / a11y 大改
- ❌ TOS 版本化（不加 `tos_version` 列）

---

## 11. 风险与回滚

| 改动 | 风险 | 回滚 |
|---|---|---|
| TOS schema 加列 | 列默认 null，老用户原有数据不受影响；prod 上 alembic 迁移失败会卡部署 | `alembic downgrade -1` 移除列；CLAUDE.md 红线，迁移前必须本地+staging 验证 |
| TOS API endpoint | 新增端点，无对现有接口侵入 | 直接 revert commit |
| TosModal 阻塞 | 若 TosModal 渲染失败（如 typo），所有已登录用户无法进入应用 | 单条 commit revert；上线后 30 分钟内监控 sentry/console error |
| 品牌 tagline 改动 | 3 处文件，纯字符串替换 | 一行 revert |
| 样式 token 化 | 可能影响极少数无视觉测试覆盖的暗角 | 靠 §9 验证清单兜底 |
| 东方点缀文案 | 纯文字，零功能影响 | 回滚成本极低 |

**TosModal 灰度建议**：上线后第一小时盯一下 sentry，若发现报错率异常，立即 revert commit 5（TosModal 集成），用户即恢复无门禁登录——但合规上短期回到改前状态。

---

**审阅请求**：

本 spec 现涵盖：~40 处前端打磨 + 1 个新后端 schema 列 + 1 个新 API + 1 个新前端 Modal 组件，工作量约 1 个完整工作日。请重点过：

- **§3.5 弹窗法律文案**（这是合规相关的最重要文字，过一遍措辞）
- **§3.2 用户流程**（是否同意"老用户首登补勾"的策略）
- **§4 东方点缀文案**（4 处）
- **§8 commit 切分**（特别是 commit 1 schema 加列需要你点头才能动）

任何一处想改告诉我；都 OK 我 commit spec 到 main，然后开分支 `feat/2026-05-14-frontend-polish-and-tos` 从 commit 1 开始落地（schema 那一步会再停一次问你确认）。
