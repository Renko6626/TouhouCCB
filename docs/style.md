# 东方炒炒币 — 设计系统

## 设计语言

**工业风高对比度黑白（Industrial Brutalist）**

核心原则：
- 极简黑白：只用 `#000000` / `#ffffff` 及灰度中间值，无彩色
- 无圆角：`border-radius: 0` 全局强制
- 重边框：元素用粗边框（2–4px）定义边界
- 硬阴影：投影用 `N px N px 0 #000` 偏移阴影，无模糊
- 强层次：通过边框粗细、背景黑白反转体现视觉层级

---

## 颜色系统

| Token | 值 | 用途 |
|---|---|---|
| `--color-black` | `#000000` | 主色、边框、文字 |
| `--color-white` | `#ffffff` | 背景、反色文字 |
| `--color-gray-1` | `#333333` | 次要文字 |
| `--color-gray-2` | `#555555` | 辅助说明文字 |
| `--color-gray-3` | `#888888` | 占位符、禁用 |
| `--color-gray-4` | `#cccccc` | 轻边框、分隔线 |
| `--color-bg-soft` | `#f5f5f5` | 工具栏、悬停底色 |
| `--color-bg-mute` | `#e0e0e0` | 进度条轨道、浅背景 |

---

## 字体

```css
font-family: 'IBM Plex Sans', 'Segoe UI', 'Noto Sans SC', -apple-system, sans-serif;
```

| 用途 | 规格 |
|---|---|
| 展示大标题 | 32–52px, `font-weight: 900`, `letter-spacing: -0.02em` |
| 卡片标题 / 区块标题 | 14–18px, `font-weight: 700`, `text-transform: uppercase` |
| 正文 | 13–15px, `font-weight: 400`, `line-height: 1.6` |
| 辅助 / 标签 | 10–12px, `font-weight: 600`, `text-transform: uppercase`, `letter-spacing: 0.06em` |
| 数据数字 | `font-variant-numeric: tabular-nums` |

---

## 核心 CSS 类

### `.industrial-frame`
**用途**：轻量卡片/容器，无投影

```css
border: 4px solid #000000;
background: #ffffff;
border-radius: 0;
/* 伪元素：内层细线 + 45° 角装饰 */
```

### `.industrial-panel`
**用途**：有投影的卡片/面板（更突出）

```css
border: 4px solid #000000;
background: #ffffff;
border-radius: 0;
box-shadow: 6px 6px 0 #000000;
/* 同样有内层细线 + 角装饰 */
```

### UnoCSS 快捷类

| 类名 | 效果 |
|---|---|
| `card` | `p-6 border-2 border-black bg-white shadow-[6px_6px_0_0_#000]` |
| `btn` | 白底黑框按钮，hover 反色 |
| `input` | 白底黑框输入框 |
| `badge` | 小标签，黑框无圆角 |

---

## 组件规范

### 按钮

两种形态：

| 形态 | 样式 |
|---|---|
| 主要（Primary） | 黑底 + 白字 + `box-shadow: 4px 4px 0 #444` |
| 次要（Default） | 白底 + 黑字 + 黑框 + `box-shadow: 2px 2px 0 #000` |

悬停效果：`transform: translate(-1px, -1px)` + 阴影加大

```css
/* 主要按钮 */
background: #000000; color: #ffffff;
border: 2px solid #000000;
box-shadow: 4px 4px 0 #444444;
/* 禁用圆角 */
border-radius: 0;
```

### 卡片（MarketCard）

结构：
```
┌─────────────────────────────┐  ← border: 2px solid #000, shadow: 4px 4px 0 #000
│ 标题                [状态]  │  ← border-bottom: 1px #e0e0e0
│ 描述文字（2行截断）          │
│ ─ 选项A ──────── 45.2%      │  ← 概率条（黑色填充）
│ ─ 选项B ──────── 54.8%      │
│─────────────────────────────│  ← border-top: 1px #e0e0e0
│ 流动性: ¥100  选项: 2        │ [详情] [交易]
└─────────────────────────────┘
```

状态标签颜色：
- 交易中：黑底白字
- 已暂停：浅灰底灰字
- 已结算：白底灰字 + 灰边框

### 状态标签

```css
/* 不使用 Naive UI NTag type，改用自定义 class */
.status-trading  { background: #000000; color: #ffffff; border: 1.5px solid #000 }
.status-halt     { background: #f0f0f0; color: #444444; border: 1.5px solid #000 }
.status-settled  { background: #ffffff; color: #888888; border: 1.5px solid #888 }
```

### 输入框 / 表单

- 边框：`2px solid #000000`
- 圆角：`0`（全局强制）
- 聚焦：保持黑色边框，无 glow

### 数据表格

```css
.n-data-table { border: 2px solid #000000; }
.n-data-table th, .n-data-table td { border: 1px solid #000000; }
```

表头：黑底白字（`background: #000000; color: #ffffff`）

### 页面章节标题

```html
<div class="section-header">  <!-- border-bottom: 2px solid #000 -->
  <h2 class="section-title">章节标题</h2>  <!-- uppercase, font-weight: 700 -->
  <button class="section-more">查看全部 →</button>
</div>
```

---

## 布局结构

```
┌─ Header（黑底，h-16）──────────────────────────────────┐
│ [T] 东方炒炒币    市场  排行榜  [用户名 ▾]             │
├─────────────┬──────────────────────────────────────────┤
│  Sidebar    │  Content Area（industrial-frame 包裹）    │
│ (240/64px)  │                                           │
│  - 首页     │  ┌─ Breadcrumb ─────────────────────┐    │
│  - 市场     │  │ 首页 > 当前页                     │    │
│  - 排行榜   │  └──────────────────────────────────┘    │
│  - 我的资产 │                                           │
│  ─────────  │  <slot>页面内容</slot>                    │
│  [管理]     │                                           │
│  - 市场管理 │                                           │
├─────────────┴──────────────────────────────────────────┤
│ Footer（黑底，h-16）                                    │
└────────────────────────────────────────────────────────┘
```

- Header 高：`64px`，黑底白字
- Sidebar 展开：`240px`，收起：`64px`
- Footer 高：`64px`，黑底灰字
- 内容最大宽：`1320px`，居中

---

## Naive UI 主题覆盖

关键 override（在 `App.vue` 中配置）：

```typescript
const industrialThemeOverrides = {
  common: {
    primaryColor: '#000000',
    borderRadius: '0px',
    borderColor: '#000000',
  },
  Button: {
    border: '2px solid #000000',
    // Primary: 黑底白字
  },
  Card: {
    boxShadow: '6px 6px 0 #000000',
    border: '2px solid #000000',
  },
  Input: {
    border: '2px solid #000000',
  },
}
```

全局 CSS 强制：
```css
/* 所有 Naive UI 组件圆角归零 */
.n-button, .n-card, .n-input, .n-select, ... { border-radius: 0 !important; }
/* 边框颜色统一黑色 */
.n-data-table, .n-tag, .n-alert { border-color: #000000 !important; }
```

---

## 动效规范

- 过渡时长：`0.1s–0.15s`（短促，避免"柔和"感）
- 悬停卡片：`transform: translate(-1px, -1px)` + 阴影增大
- 页面淡入：`opacity 0.3s ease`
- 禁止：`border-radius` 过渡、颜色渐变过渡（违背工业风）
- 骨架屏：线性 shimmer，颜色 `#f5f5f5 → #ebebeb → #f5f5f5`

---

## 涨跌/盈亏色（唯一的彩色例外）

金融数据中的涨跌、盈亏方向允许使用红绿色，这是行业惯例，不受黑白灰限制：

| Token | 值 | 用途 |
|---|---|---|
| `--color-up` | `#16a34a` | 上涨、盈利、买入 |
| `--color-down` | `#dc2626` | 下跌、亏损、卖出 |
| `--color-up-bg` | `#f0fdf4` | 涨色浅底（K 线阳柱填充等） |
| `--color-down-bg` | `#fef2f2` | 跌色浅底（K 线阴柱填充等） |

**适用场景**：
- K 线图阳/阴柱
- 价格变动方向箭头或文字（+2.3% / -1.5%）
- 交易记录中的买入/卖出标签
- 持仓盈亏金额

**不适用场景**（仍遵循黑白灰）：
- 状态标签（交易中/熔断/已结算）
- 按钮、输入框、卡片等通用 UI 组件
- 概率/价格条（使用黑色填充 + 灰色轨道）

---

## 禁止项

- ❌ 任何 `border-radius > 0`
- ❌ `box-shadow` 带模糊值（如 `0 4px 6px rgba(0,0,0,0.1)`）
- ❌ 渐变背景（`gradient`）
- ❌ 通用 UI 中的彩色（涨跌色例外，见上节）
- ❌ dark mode class（`dark:text-*`、`dark:bg-*`）
- ❌ Naive UI 的 `type="success/warning/error"` Tag（颜色不符，改用自定义 class）
