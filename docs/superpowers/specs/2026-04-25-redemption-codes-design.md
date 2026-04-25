# 兑换码模块（Redemption Codes）设计文档

**日期**：2026-04-25
**作者**：brainstorming session（Ralph + 用户）
**状态**：Draft，待用户 review

---

## 1. 背景与目标

TouhouCCB 当前的"资金"是一个纯内部账本（LMSR 预测市场驱动，6 位 `Decimal`），与外部世界没有任何兑换通道。用户希望让 TouhouCCB 资金能"消费"到朋友们的其他网站/系统上，进行单向流出兑换。

**约束条件**：
- 各方系统**分离部署**，底层无法直接打通
- 朋友们**不愿意 / 没空写很多代码**
- 未来可能接入**多个**朋友的系统（N ≥ 1）
- 朋友那边的"东西"是浮动定价的，定价规则由朋友各自决定

**经过 brainstorming 收敛后的结论**：
- 方向性：**单向流出**（TouhouCCB → 朋友站点）
- 机制：**离线兑换码**（用户复制粘贴，两边账户系统零耦合）
- 库存模型：**朋友预生成码批次 → 管理员后台 CSV 导入 → TouhouCCB 当码贩卖机**
- 朋友侧工作量：**0 行代码**（只需在他自己原本的兑换码核销逻辑里把码列入白名单）

---

## 2. 整体架构

TouhouCCB 多一个「兑换中心」模块，本质是**码贩卖机 + 用户钱包关联记录**。

```
朋友的站点                TouhouCCB                            用户
  │                          │                                  │
  │ ① 朋友自己生成 N 个码     │                                  │
  │   （他原本就有的兑换码）   │                                  │
  │                          │                                  │
  │ ② 给管理员 CSV 文件       │                                  │
  ├─────────────────────────►│                                  │
  │                          │ ③ 管理员后台导入到批次             │
  │                          │                                  │
  │                          │ ④ 用户在兑换中心浏览批次 ◄────────┤
  │                          │                                  │
  │                          │ ⑤ 用户购买 → 扣资金 + 弹一个码 ─►│
  │                          │                                  │
  │ ⑥ 用户复制码到朋友站点 ◄─────────────────────────────────────┤
  │                          │                                  │
  │ ⑦ 朋友站点用自己原本逻辑   │                                  │
  │   核销该码                 │                                  │
```

整个流程中，朋友的站点和 TouhouCCB **没有任何 API 交互**，朋友甚至不需要知道 TouhouCCB 的存在。TouhouCCB 也无从知晓码是否在朋友侧已被核销——这是**有意为之**的设计。

---

## 3. 数据模型

新增三张表，沿用 `app/models/base.py` 的现有 `Decimal` 精度约定。

### `RedemptionPartner` — 合作方

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int PK | |
| `name` | str | 合作方显示名 |
| `description` | text | 合作方介绍 |
| `website_url` | str | 朋友站点 URL（用户跳转使用） |
| `logo_url` | str nullable | 合作方 Logo |
| `is_active` | bool | 禁用 → 该合作方下所有批次从用户列表隐藏 |
| `created_at` | datetime | |

### `RedemptionBatch` — 批次（= 一种商品 SKU）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int PK | |
| `partner_id` | FK → Partner | |
| `name` | str | 批次/商品名 |
| `description` | text | 兑换说明（markdown） |
| `unit_price` | `Decimal(18, 6)` | 单个码的售价（TouhouCCB 资金，6 位精度） |
| `status` | enum: `draft` / `active` / `archived` | 状态机见下文 |
| `created_at` | datetime | |
| `created_by_admin_id` | FK → User | 审计用 |

**状态机**：
- `draft`：刚创建，用户列表不可见，可继续录入码
- `active`：上架销售，用户可购买
- `archived`：下架，用户列表不显示，但已购用户仍能查看自己的码
- 不允许在 `active` 状态下修改 `unit_price`（保持已购用户的体感一致）；要改价 = 新建批次

### `RedemptionCode` — 单个码（库存原子单元）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int PK | |
| `batch_id` | FK → Batch | |
| `code_string` | str **unique（全局）** | 实际兑换码字符串 |
| `status` | enum: `available` / `sold` | |
| `bought_by_user_id` | FK → User nullable | 已售时填 |
| `bought_at` | datetime nullable | |
| `marked_used_by_user_at` | datetime nullable | 用户自己点"已使用"的时间，**纯本地 UI 状态**，不影响码本身 |

**关键约束**：
- `code_string` 全局 UNIQUE：防止同一字符串被导入两个不同批次造成混乱
- 已售码的 `code_string` 在**管理员后台不显示**，只展示"已售给 user_id=X 于 Y 时间"，避免管理员侧泄露

---

## 4. 用户流程

### 4.1 兑换中心列表页（`/redemption`）

- 按合作方分组展示所有 `status=active` 且**剩余库存 > 0** 的批次卡片
- 每张卡片显示：批次名、单价、剩余数量、合作方信息（名称/Logo）
- 点击卡片进入详情页

### 4.2 批次详情页（`/redemption/batches/:id`）

- 完整 markdown 描述（兑换说明）
- 合作方信息 + 跳转链接
- 价格 + 当前余量
- "购买"按钮 → 二次确认弹窗：
  - 显示扣款金额、当前余额、购买后余额
  - **明确提示**："码一旦显示视同交付，不可退款"
  - 用户确认 → 调后端购买 API

### 4.3 购买原子事务（关键）

后端单事务（参考 `services/lmsr.py` 现有事务模式）：

1. 行锁用户余额：`SELECT balance FROM users WHERE id = ? FOR UPDATE`
2. 校验：`balance >= batch.unit_price`，否则 422
3. 校验批次状态：`status = 'active'`，否则 409
4. 抓码：`SELECT id, code_string FROM redemption_codes WHERE batch_id = ? AND status = 'available' FOR UPDATE SKIP LOCKED LIMIT 1`
5. 若拿不到 → 409 "已售罄"
6. 扣款：`UPDATE users SET balance = balance - ? WHERE id = ?`
7. 标记码：`UPDATE redemption_codes SET status='sold', bought_by_user_id=?, bought_at=now() WHERE id=?`
8. 写资金流水（沿用现有资金流水表）：类型 `redemption_purchase`
9. 提交 → 返回 `{ code_id, code_string, batch_info, ... }`

**为何用 `FOR UPDATE SKIP LOCKED`**：处理并发抢最后一码时，多个事务不会互相阻塞，拿不到的直接走"已售罄"分支。

### 4.4 我的兑换记录（`/my/redemptions`）

- 列表：购买时间 / 合作方 / 批次名 / 价格 / 状态（未使用 / 已使用）
- 点开看详情：完整码字符串 + 兑换说明 + 跳转朋友站点链接
- "标记为已使用" 按钮：可逆切换 `marked_used_by_user_at`，纯前端体感折叠功能

### 4.5 限速

参考现有 `/market` 规则：
- `POST /redemption/purchase`：10r/s
- 列表与详情查询：默认限速

---

## 5. 管理员流程

沿用现有 `core/admin.py` 超管鉴权（参考红线：第一个 SSO 登录的自动超管，不另设管理员创建接口）。

### 5.1 合作方管理（`/admin/redemption/partners`）

- CRUD：增 / 改 / 启用禁用
- 不提供硬删，只允许禁用

### 5.2 批次管理（`/admin/redemption/batches`）

- 列表显示：批次名 / 合作方 / 单价 / 总码数 / 已售 / 剩余 / 状态
- 创建批次：选合作方 + 批次名 + 单价 + 描述（markdown）+ 状态（默认 `draft`）
- 编辑批次：`active` 状态下不能改价；可在 `draft ↔ active ↔ archived` 之间切换
- 不提供硬删，只允许 archive

### 5.3 CSV 导入码（`/admin/redemption/batches/:id/import`）

**输入方式**（二选一）：
- 上传 CSV 文件
- 粘贴文本框

**CSV 格式**：
- 单列 `code_string`，每行一个码
- 支持表头 `code` 自动识别（有则跳过首行）
- 空行 / 前后空白自动 trim

**导入流程**：
1. **预检（dry-run）**：解析后展示
   - ✅ 新增 N 条
   - ⚠️ 与已有码重复 M 条（跳过）
   - ❌ 非法 K 条（空字符串、超过 64 字符等）
2. 管理员确认 → 真正写入
3. 写入用 `INSERT ... ON CONFLICT (code_string) DO NOTHING` 保证幂等（重复导入安全）
4. 完成后显示报告：实际写入 / 跳过

---

## 6. 边界与安全

| 场景 | 处理 |
|---|---|
| 用户余额不足 | 前端按钮置灰 + 后端二次校验返回 422 |
| 批次被别人买空 | 后端事务返回 409，前端提示并刷新库存 |
| 同一码字符串被两个批次导入 | 全局 unique 约束拒绝；预检阶段提前提示 |
| 用户重复点购买 | 前端 disable + 后端 10r/s 限速 |
| 管理员误删批次 / 合作方 | 不提供硬删，只允许 archive / disable |
| 已售码的可见性 | 仅对 `bought_by_user_id` 本人可见；管理员后台不显示已售码 `code_string` |
| Logo / 描述 XSS | 后端存原文，前端用项目现有 markdown 渲染器（已 sanitize） |
| 朋友站点核销了一个码但 TouhouCCB 不知 | **设计上接受**——TouhouCCB 不试图追踪码的核销状态，"已使用"是纯前端体感 |
| 朋友站点跑路 / 拒绝核销 | 列入合作方风险；管理员可在后台 archive 该合作方所有批次，但已售用户的损失需自行与朋友交涉。建议在 UI 显著位置提示该风险 |

---

## 7. 不退款政策

**强约束**：购买后**不允许退款**。

理由：码字符串一旦在购买成功页或"我的兑换记录"中向用户揭示，TouhouCCB 已视同交付。无论用户是否实际去朋友站点核销，TouhouCCB 都无从验证，因此不能退款。

UI 层面：购买二次确认弹窗显著文案，避免用户误操作。

---

## 8. 前端实现约束（守 `docs/development.md` + `docs/style.md`）

- Vue 3 + `<script setup>` + Pinia store（新建 `src/stores/redemption.ts`）
- API 层：`src/api/redemption.ts`，类型 `src/types/redemption.ts`
- 路由懒加载新增：
  - `/redemption`（列表）
  - `/redemption/batches/:id`（详情）
  - `/my/redemptions`（我的记录）
  - `/admin/redemption/partners`、`/admin/redemption/batches`、`/admin/redemption/batches/:id/import`
- UnoCSS + 工业风设计 token：黑白、无圆角、粗边框，本模块无涨跌色
- 侧栏（参考最近 commit `0445b4a`）增加「兑换中心」入口

---

## 9. 后端实现约束

- 新增模型：`app/models/redemption.py`（不动 `base.py`，参考红线"`base.py` 没迁移机制"）
- 新增 API：
  - `app/api/v1/redemption.py`（用户端：列表 / 详情 / 购买 / 我的记录 / 标记已用）
  - `app/api/v1/admin_redemption.py`（管理员端）
- 新增服务层：`app/services/redemption.py` 封装购买事务
- 限速沿用现有装饰器，参考 `/market` 配置
- 资金流水类型新增：`redemption_purchase`

**生产部署的库迁移**：
- 首次部署：依赖 `create_all` 自动建新表（仅新增表，不影响已有列）
- 将来加字段：单独写启动期迁移脚本（参考 `b3bf0be` 的 LoanV1 启动期迁移模式）

---

## 10. 测试要求

**后端 `pytest`**：
- 购买事务并发竞态（多事务抢最后一码，仅一个成功）
- 余额不足、批次售罄、批次非 active 的拒绝路径
- CSV 导入幂等性（重复导入同一文件不会重复入库）
- CSV 导入预检（非法行 / 重复行的统计正确）
- 权限边界（非管理员访问后台 API 拒绝）
- 已售码的 `code_string` 仅对买家本人可见

**前端**：
- `npm run type-check` + `npm run lint` + `npm run build`
- 浏览器实测：购买扣款、码可见性、标记已用切换、空库存态、未登录跳转、移动端布局

---

## 11. 未来扩展点（不在本期范围）

- 朋友侧主动核销回调（如朋友愿意写一点点代码，可加 webhook 让 TouhouCCB 同步"已使用"状态）
- 批量购买（一次买 N 个码）
- 批次的开售时间 / 截止时间调度
- 不同合作方的兑换汇率联动
- 用户对合作方的评分 / 投诉

本期严格只做最小可用闭环，后续按需扩展。

---

## 12. 风险

| 风险 | 缓解 |
|---|---|
| 朋友给的码批次质量不可控（重复 / 失效 / 拒兑） | 预检 + 合作方启用禁用机制；UI 显著提示风险归属 |
| 管理员误操作（错把高价码低价上架） | `active` 状态禁改价；CSV 导入有预检确认步骤 |
| 用户截图泄露码 | 设计上接受——这是离线码的固有风险，不在 TouhouCCB 责任范围 |
| `code_string` 全局 unique 约束在导入大批次时性能 | 加索引 + `ON CONFLICT DO NOTHING`，单次导入实测 ≤ 几千条规模可接受 |
