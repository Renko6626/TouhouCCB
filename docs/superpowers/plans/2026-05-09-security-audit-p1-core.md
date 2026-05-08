# 安全审计阶段 1：业务核心 + 认证授权 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 TouhouCCB 安全审计第 1 阶段（业务核心 + 认证授权），产出一份阶段报告 `docs/security-audit-2026-05-09-p1-core.md`，按 P0–P3 标注每条发现。**只读审计，不动产品代码**。

**Architecture:** 每个 task 聚焦一个审计领域（一组相关文件 + 一组要回答的问题），先读源码 → 对照检查清单/危险模式 → 把发现追加到阶段报告 → commit。整个阶段在 `ralph/2026-05-09-secaudit-p1-core` 分支推进，不 push。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy / Decimal；Casdoor SSO；前端不参与本阶段；静态工具：`bandit`、`semgrep`。

**前置阅读（每个 task 都会用到的项目护栏）：**
- `CLAUDE.md` — 红线（不动 `.env*`、不跑 `init_db.py`、不 `--no-verify`、不 push、不在 main 提交）
- `docs/superpowers/specs/2026-05-08-security-audit-design.md` §6 护栏 — 审计期间不修复产品代码、静态工具结果先 triage 再写入报告
- `docs/development.md` — 栈约束（如需查阅）

**本阶段产物：**
- `docs/security-audit-2026-05-09-p1-core.md` — 阶段报告（贯穿所有 task 增量追加）
- `docs/ralph-log.md` — 每个独立 commit 一条简记
- 多个独立 commit，每个 task 一个 commit

---

## Task 0：建立分支 + 报告骨架

**Files:**
- Create: `docs/security-audit-2026-05-09-p1-core.md`
- Modify: `docs/ralph-log.md`（追加一条）

- [ ] **Step 1：确认在 main 分支且无未提交变更**

```bash
git status
git branch --show-current
```

Expected：`main`、工作区干净。如不干净，**停下问用户**。

- [ ] **Step 2：开 ralph 分支**

```bash
git checkout -b ralph/2026-05-09-secaudit-p1-core
```

- [ ] **Step 3：创建阶段报告骨架**

写入 `docs/security-audit-2026-05-09-p1-core.md`：

```markdown
# 安全审计阶段 1 报告：业务核心 + 认证授权

**审计日期**：2026-05-09 起
**分支**：ralph/2026-05-09-secaudit-p1-core
**审计员**：Claude（代码 + 静态工具，只读）
**评级体系**：P0–P3（详见 docs/superpowers/specs/2026-05-08-security-audit-design.md §3）
**状态**：进行中

## 执行摘要

（最后填写）

## 发现明细

### LMSR 数值安全
（Task 1 填写）

### 资金一致性 / 事务原子性
（Task 2 填写）

### 持仓估值与精度
（Task 3 填写）

### 贷款 / 复利 / 还款
（Task 4 填写）

### 兑换码资金流
（Task 5 填写）

### SSO / Casdoor / Token
（Task 6 填写）

### 首位 admin 自动晋升竞态
（Task 7 填写）

### admin gate 覆盖矩阵
（Task 8 填写）

### IDOR / 横向越权矩阵
（Task 9 填写）

### models/base.py 无迁移机制风险
（Task 10 填写）

### 静态工具 triage 结果
（Task 11 填写）

## 不在范围（已识别但本阶段不审）

- （边审边记录）

## 阶段统计

（最后填写：P0/P1/P2/P3 各几条）
```

- [ ] **Step 4：追加 ralph-log**

在 `docs/ralph-log.md` 末尾追加：

```markdown
## 2026-05-09 HH:MM — 安全审计阶段 1 启动
**目标**：业务核心 + 认证授权审计（spec §4.1）
**动机**：上线前最后一道安全线，参考 docs/superpowers/specs/2026-05-08-security-audit-design.md
**范围**：仅限 backend，仅读不改
**改动**：
- `docs/security-audit-2026-05-09-p1-core.md`：建报告骨架
**风险 & 回滚**：仅文档，回滚 = 删文件
**验证**：N/A（仅文档）
**下一轮**：Task 1 LMSR 数值安全
```

- [ ] **Step 5：commit**

```bash
git add docs/security-audit-2026-05-09-p1-core.md docs/ralph-log.md
git commit -m "docs(secaudit): 阶段 1 报告骨架 + ralph-log

阶段 1（业务核心+认证授权）启动，仅文档，不动产品代码。"
```

---

## Task 1：LMSR 数值安全审计

**Files:**
- Read: `backend/app/services/lmsr.py`（41 行，全文细读）
- Read: `backend/tests/test_*.py` 中涉及 lmsr 的（grep 即可）
- Modify: `docs/security-audit-2026-05-09-p1-core.md`（填写 LMSR 章节）

**为什么重要**：CLAUDE.md 红线：`lmsr.py` 是定价核心，改错全站估值错乱。Decimal 精度后端 6/8 位。本 task 只读不改。

- [ ] **Step 1：通读 `backend/app/services/lmsr.py`**

```bash
# 用 Read 工具读完整文件
```

记录关键 API（函数签名、入参类型、返回类型）。

- [ ] **Step 2：grep LMSR 调用点**

```bash
grep -rn "from app.services.lmsr\|from .lmsr\|import lmsr" backend/app/
grep -rn "lmsr\." backend/app/api/ backend/app/services/
```

记录所有调用点（file:line）。

- [ ] **Step 3：对照危险模式清单**

逐项检查并在草稿里勾选（Y/N + 证据 file:line）：

- [ ] 价格/概率被零除：是否存在 `b == 0`、`sum(exp) == 0`、`liquidity == 0` 的入口路径？是否有前置守卫？
- [ ] 负份额输入：`buy(-x)` / `sell(-x)` 是否被显式拒绝（应用层 vs 路由层）？
- [ ] 极大份额：`Decimal("1e30")` 类输入是否会让 `exp(...)` 溢出（Python Decimal 不会溢出但会非常慢，构成 DoS——记入阶段 3，本阶段只标注）？
- [ ] 量化精度：函数内部 `Decimal` 运算是否在每个步骤都用了一致的 `quantize` 精度？返回值的精度是否与 CLAUDE.md "资金/份额 6 位、价格 8 位" 一致？
- [ ] `ROUND_*` 模式：是否一致用 `ROUND_HALF_EVEN` / `ROUND_DOWN` 等？混用会导致零和不平衡
- [ ] 浮点污染：是否有 `float()` 强转或 `math.exp` / `math.log`（应该用 `Decimal` 或精度可控的库）
- [ ] 状态读写竞态：LMSR 状态是否在事务内一致读写，还是先读后写（TOCTOU）？这部分跨到 Task 2 但本任务标注线索

- [ ] **Step 4：把发现写进报告 LMSR 章节**

每条发现按 spec §5.1 模板（位置/类别/复现/影响/修复建议/等级）写入。
**没发现也写**："2026-05-09 审计：未发现以上模式相关问题，证据见 file:line。"

- [ ] **Step 5：commit**

```bash
git add docs/security-audit-2026-05-09-p1-core.md
git commit -m "docs(secaudit): 阶段 1 / Task 1 LMSR 数值安全审计完成"
```

---

## Task 2：资金一致性 / 事务原子性审计

**Files:**
- Read: `backend/app/api/v1/market.py`（1058 行，重点是 buy/sell/quote 路径）
- Read: `backend/app/services/lmsr.py`（已读，回看调用点）
- Read: `backend/app/models/base.py`（看 User.cash / Position.shares 字段类型）
- Read: `backend/app/core/database.py`（看 session/事务管理方式）
- Modify: `docs/security-audit-2026-05-09-p1-core.md`

**为什么重要**：market.py 是买卖/报价/资金/滑点的入口，并发下事务不严会出负余额、负份额、价格抢跑套利。

- [ ] **Step 1：读 `core/database.py`，搞清 session 模式**

是 `SessionLocal()` 手动管理还是 FastAPI `Depends(get_db)`？是否有显式 `commit()` / `rollback()`？是否每请求一个 session？

- [ ] **Step 2：定位 market.py 写路径**

```bash
grep -n "@router\." backend/app/api/v1/market.py
grep -n "def buy\|def sell\|def quote\|async def buy\|async def sell\|async def quote" backend/app/api/v1/market.py
```

记录每个写路径的起止行号。

- [ ] **Step 3：通读 buy / sell 函数**

对每个写路径回答：

- [ ] **事务边界**：从读取用户余额 → 读取 LMSR 状态 → 写入 → 提交，是否在**同一个事务**？
- [ ] **行级锁**：余额扣减前是否 `SELECT ... FOR UPDATE`（SQLAlchemy: `.with_for_update()`）？没有锁则两并发请求都基于旧余额扣减 = 透支
- [ ] **TOCTOU**：是否先 quote 计算价格、再写入扣减——中间 LMSR 状态可能已变？滑点保护是否在写入时再校验？
- [ ] **滑点保护**：是否接受 `max_price` / `slippage_bps` 入参；服务端是否强制最大滑点上限
- [ ] **份额下界**：sell 时校验 `position.shares >= quantity`？是否会出负 shares？
- [ ] **资金下界**：buy 时校验 `user.cash >= cost`？是否会出负 cash？是否有兜底 abort？
- [ ] **回滚路径**：异常时是否真正 rollback？还是 commit 后异常导致部分成交？
- [ ] **重复提交 / 幂等**：同一请求重发会不会成交两次？是否有客户端 nonce / idempotency-key？
- [ ] **手续费 / fee 一致性**：fee 是先计算后扣还是先扣后算？精度量化点？
- [ ] **quote 不应有副作用**：纯只读？还是会写入价格快照（如有，是否事务一致）？

- [ ] **Step 4：把发现写进报告"资金一致性"章节**

特别标注 P0/P1：能造成负余额/负份额/双花的 = P0 candidate。

- [ ] **Step 5：commit**

```bash
git add docs/security-audit-2026-05-09-p1-core.md
git commit -m "docs(secaudit): 阶段 1 / Task 2 资金一致性 / 事务原子性"
```

---

## Task 3：持仓估值与精度审计

**Files:**
- Read: 持仓估值入口（先 grep 定位）
- Read: `backend/app/services/lmsr.py`（清算价值函数，如有）
- Read: 前端持仓估值代码（**只读不改**）：`thccb-frontend/src/` 下凡涉及 holdings 估值的
- Modify: `docs/security-audit-2026-05-09-p1-core.md`

**为什么重要**：CLAUDE.md 强调"清算价值（含卖出滑点+手续费），不是瞬时价×数量"，且最近修正过（commit `4a49d2e`）。要验证修正是否到位、是否有第二个错误的入口。

- [ ] **Step 1：定位所有持仓估值入口**

```bash
grep -rn "def.*portfolio\|def.*holdings.*value\|清算\|liquidat\|portfolio_value\|holdings_value" backend/app/
grep -rn "portfolio\|holdings" thccb-frontend/src/api/ thccb-frontend/src/stores/ thccb-frontend/src/composables/
```

记录每个入口（file:line）。

- [ ] **Step 2：看 commit `4a49d2e` 的修正内容（理解原意）**

```bash
git show 4a49d2e --stat
git show 4a49d2e -- '*.py'
```

记录：哪个函数被修，从什么改成什么。

- [ ] **Step 3：对每个估值入口逐项验证**

- [ ] 是否调用了 LMSR 清算价值函数（含手续费 + 滑点）？
- [ ] 是否还存在简单 `price * quantity` 的旧路径（哪怕一个被遗漏的接口）？
- [ ] 后端 → 前端的序列化精度是否完整保留（`Decimal` → `str` vs `float`）？
- [ ] 前端是否对返回值做了 `Number(...)`（在边界丢精度，CLAUDE.md 明确警告）？
- [ ] 多空仓 / 空仓的估值方向是否正确？
- [ ] 估值在 SSE 推送中是否一致使用同一函数（避免后端两套口径）？

- [ ] **Step 4：写报告"持仓估值与精度"章节**

- [ ] **Step 5：commit**

```bash
git add docs/security-audit-2026-05-09-p1-core.md
git commit -m "docs(secaudit): 阶段 1 / Task 3 持仓估值与精度"
```

---

## Task 4：贷款 / 复利 / 还款审计

**Files:**
- Read: `backend/app/services/loan_service.py`
- Read: `backend/app/services/loan_sweep.py`
- Read: `backend/app/services/loan_migrate.py`
- Read: `backend/app/api/v1/loan.py`
- Read: `backend/tests/test_loan_*.py`（已有测试，看覆盖度）
- Modify: `docs/security-audit-2026-05-09-p1-core.md`

**为什么重要**：CLAUDE.md 明确"还款复利 bug 修复"是近期工作（commits `60847ad`、`5771b45`）。要复核 fix 是否覆盖所有路径、是否引入新 bug。

- [ ] **Step 1：复盘相关 commit**

```bash
git log --oneline --all -- backend/app/services/loan_service.py backend/app/api/v1/loan.py
git show 60847ad
git show 5771b45
```

理解修复前 bug 的原理（复利推过 cash 跑负），记录 fix 的关键代码段。

- [ ] **Step 2：通读 `loan_service.py` 与 `loan.py`**

记录每个写路径（borrow / repay / accrue / sweep）。

- [ ] **Step 3：逐项审计**

- [ ] **repay 双封顶**：是否真的同时按"真实负债（含 post-accrual）" + "当前现金"两侧封顶？跨时区 / 跨日累加是否一致？
- [ ] **accrue 边界**：利息计算的 `Decimal` 量化是否一致；负利率 / 零本金 / 极小本金的边界
- [ ] **sweep 顺序**：批量 sweep 是否对每个 user 独立事务，还是单一大事务（失败回滚范围）？
- [ ] **sweep 抢跑**：sweep 与用户主动 repay 并发会不会双扣？
- [ ] **借款额度**：是否校验最大额度、最大笔数、信用评估？
- [ ] **强平 / 风控**：cash + 持仓估值 < 负债 时的处理路径
- [ ] **不变量校验**：CLAUDE.md commit `5771b45` 提到"debt/cash 不变量防御兜底"——具体是哪个不变量、断言点在哪、能否被绕过

- [ ] **Step 4：审 `loan_sweep.py`（CLAUDE.md 没标红线但属高敏感）**

定时/手动触发？是否走 admin gate？sweep 失败的告警机制？

- [ ] **Step 5：写报告"贷款"章节，Step 6：commit**

```bash
git add docs/security-audit-2026-05-09-p1-core.md
git commit -m "docs(secaudit): 阶段 1 / Task 4 贷款 / 复利 / 还款"
```

---

## Task 5：兑换码资金流审计

**Files:**
- Read: `backend/app/services/redemption.py`
- Read: `backend/app/api/v1/redemption.py`
- Read: `backend/app/api/v1/admin_redemption.py`
- Read: `backend/tests/test_redemption_*.py`
- Read: `backend/app/models/base.py`（`RedemptionTransaction` 表，commit `bed3553`）
- Modify: `docs/security-audit-2026-05-09-p1-core.md`

**为什么重要**：兑换码 = 资金注入入口，最近完成"技术债收尾"（commit `697730d`），要验技术债收尾是否真的封闭了所有薄弱点。

- [ ] **Step 1：审码生成**

- [ ] 码字符集与长度（熵）；是否可被穷举猜出
- [ ] 生成是否使用 `secrets` 模块（不是 `random`）
- [ ] 唯一性：DB 层 `UNIQUE` 约束 + 应用层重试

- [ ] **Step 2：审兑换路径**

- [ ] 单码单次：DB 锁 + 状态机？两并发兑换同一码会不会双花
- [ ] 资金流水审计表 `RedemptionTransaction`：是否每次兑换都强制写入？事务原子性
- [ ] 兑换金额来源：是否能被前端篡改（应该完全由后端从批次取）
- [ ] 兑换时账户余额上限/异常检查

- [ ] **Step 3：审 admin 批次管理**

- [ ] 创建批次：是否校验 admin；批次额度上限
- [ ] 库存低位告警（commit `11f5b1e`）：是否仅 UI 层、能否被关闭？
- [ ] 批次禁用 / 撤回：是否会作废未使用码、对已兑换的回滚处理

- [ ] **Step 4：审枚举攻击**

未鉴权的"查询码状态"接口存在吗？返回是否会泄漏"已使用 vs 不存在"差异（侧信道）？

- [ ] **Step 5：写报告 + commit**

```bash
git add docs/security-audit-2026-05-09-p1-core.md
git commit -m "docs(secaudit): 阶段 1 / Task 5 兑换码资金流"
```

---

## Task 6：SSO / Casdoor / Token 审计

**Files:**
- Read: `backend/app/api/v1/auth.py`（174 行）
- Read: `backend/app/core/oidc.py`（**只读**，CLAUDE.md 红线之一，**不动逻辑**）
- Read: `backend/app/core/users.py`（**只读**）
- Read: `backend/app/core/config.py`（**只读**，看 OIDC 配置项）
- Read: `backend/tests/test_auth.py`
- Modify: `docs/security-audit-2026-05-09-p1-core.md`

**为什么重要**：CLAUDE.md 说"改错全员无法登录"。这个 task **只审不改**，发现问题进 issue 清单留给单独修复轮次。

- [ ] **Step 1：通读 `auth.py` 与 `oidc.py`**

记录所有路由、token 校验路径、回调路径。

- [ ] **Step 2：OAuth/OIDC 必查项**

- [ ] **state 参数**：登录发起时是否生成 state、是否绑定 session、回调时是否校验
- [ ] **nonce**：ID token 是否带 nonce、是否回查
- [ ] **回调 URL 白名单**：在 Casdoor 端配置 + 服务端 `redirect_uri` 是否硬编码（防开放重定向）
- [ ] **token 签名校验**：是否校验签名（JWT `verify=True`）、issuer、audience、过期 `exp`、`iat` / `nbf`
- [ ] **JWKS 获取**：是否走 HTTPS、是否缓存（避免 DoS 上游 + 性能）、缓存过期策略
- [ ] **算法白名单**：是否拒绝 `alg=none`、是否限制 `RS256` 等
- [ ] **session 管理**：本地 session/cookie 怎么签发？过期、刷新、撤销路径
- [ ] **登出**：本地登出是否同步上游、token 撤销
- [ ] **错误信息**：登录失败的回显是否泄漏"用户存在 vs 密码错"（应该统一）

- [ ] **Step 3：审 `next` 参数 / 登录回跳**

任何 `?next=` / `?redirect=` 类参数都必须走白名单（仅同源、仅已知路径），否则开放重定向。

- [ ] **Step 4：审 token 存储**

服务端 token 入库吗？加密了吗？前端存哪（这个跨到阶段 2，本任务标注线索）

- [ ] **Step 5：写报告 + commit**

```bash
git add docs/security-audit-2026-05-09-p1-core.md
git commit -m "docs(secaudit): 阶段 1 / Task 6 SSO / Casdoor / Token"
```

---

## Task 7：首位 admin 自动晋升竞态审计

**Files:**
- Read: `backend/app/core/admin.py`（**只读**，CLAUDE.md 红线）
- Read: `backend/app/core/users.py`（**只读**）
- Read: `backend/app/core/oidc.py`（**只读**，看登录后 user 创建路径）
- Modify: `docs/security-audit-2026-05-09-p1-core.md`

**为什么重要**：CLAUDE.md 明确"第一个 SSO 登录的自动超管"。理论上"第一个"=单数，但在并发下可能是"前几个"。

- [ ] **Step 1：定位晋升逻辑**

```bash
grep -rn "is_admin\|is_superuser\|admin\s*=\s*True\|first.*admin\|count.*=.*0" backend/app/core/
```

- [ ] **Step 2：审晋升判定**

- [ ] 判定条件是 `User.count() == 0` 还是 `User.count() == 0 within transaction`？
- [ ] 是否走 `SELECT FOR UPDATE` 或唯一约束保证只有第一个？
- [ ] 在事务内 INSERT 之前还是之后判定？
- [ ] 两并发首登能否都拿到 admin（验证：走逻辑流，再看 DB 约束）

- [ ] **Step 3：审 admin 字段的其他变更入口**

除"自动晋升"外是否还有"手动晋升"接口（CLAUDE.md 说不应该加，但要验证现状）？如有，是 P0。

- [ ] **Step 4：写报告 + commit**

```bash
git add docs/security-audit-2026-05-09-p1-core.md
git commit -m "docs(secaudit): 阶段 1 / Task 7 首位 admin 自动晋升竞态"
```

---

## Task 8：admin gate 覆盖矩阵

**Files:**
- Read: 所有 `backend/app/api/v1/*.py`
- Read: `backend/app/core/admin.py`（看 `require_admin` 实现）
- Modify: `docs/security-audit-2026-05-09-p1-core.md`

**为什么重要**：admin 路由若漏 gate = 普通用户能调 admin 接口 = P0。

- [ ] **Step 1：列出 require_admin 的实现**

```bash
grep -rn "def require_admin\|require_admin\b" backend/app/
```

记录函数签名、是否抛 401/403、是否检查 `user.is_admin`。

- [ ] **Step 2：列出所有 admin 路由**

```bash
grep -rn "@router\.\(get\|post\|put\|delete\|patch\)" backend/app/api/v1/admin_redemption.py
grep -rn "/admin\b\|admin_" backend/app/api/v1/
```

- [ ] **Step 3：构建路由 ↔ 鉴权矩阵**

在报告里画一张表：

```markdown
| 文件:行 | HTTP 方法 + 路径 | 鉴权依赖 | 备注 |
|---|---|---|---|
| admin_redemption.py:45 | POST /admin/redemption/batch | Depends(require_admin) | OK |
| ... | ... | ... | ... |
```

任何 `/admin*` 路径**未走 require_admin** = P0；任何 admin_* 文件下的路由未走 require_admin = P0。

- [ ] **Step 4：审 require_admin 是否能被绕过**

- [ ] 是否仅检查 `user.is_admin` 而未先校验认证（即未登录就直接拒）
- [ ] 是否被某些路由用 `Depends(get_current_user)` 替代（容易漏判 admin）

- [ ] **Step 5：写报告 + commit**

```bash
git add docs/security-audit-2026-05-09-p1-core.md
git commit -m "docs(secaudit): 阶段 1 / Task 8 admin gate 覆盖矩阵"
```

---

## Task 9：IDOR / 横向越权矩阵

**Files:**
- Read: 所有 `backend/app/api/v1/*.py`
- Modify: `docs/security-audit-2026-05-09-p1-core.md`

**为什么重要**：FastAPI 路由经常按 ID 取资源，若不校验 ownership = 横向越权（A 用户读/改 B 用户的资源）。

- [ ] **Step 1：grep 所有按 ID 路径**

```bash
grep -rn "{.*_id.*:\|{id\|/\(.*\)/{" backend/app/api/v1/
grep -rn "Path(\|path_params" backend/app/api/v1/
```

- [ ] **Step 2：grep ORM 按 ID 取**

```bash
grep -rn "\.get(\s*[a-zA-Z_]*_id\|\.filter_by(id=\|\.filter(.*\.id\s*==" backend/app/api/v1/
```

- [ ] **Step 3：构建资源 ↔ ownership 矩阵**

```markdown
| 文件:行 | 路由 | 资源类型 | ownership 校验 | 等级 |
|---|---|---|---|---|
| user.py:88 | GET /users/{id} | User | 仅 self 或 admin？ | OK / P0 / P1 |
| loan.py:120 | POST /loan/{id}/repay | Loan | loan.user_id == current_user.id？ | ... |
```

未做 ownership 校验且非 admin 接口的 = P0/P1（按敏感度）。

- [ ] **Step 4：写报告 + commit**

```bash
git add docs/security-audit-2026-05-09-p1-core.md
git commit -m "docs(secaudit): 阶段 1 / Task 9 IDOR / 横向越权矩阵"
```

---

## Task 10：models/base.py 无迁移机制风险审计

**Files:**
- Read: `backend/app/models/base.py`（**只读**，CLAUDE.md 红线）
- Read: `backend/init_db.py`（**只读**，**不跑**）
- Modify: `docs/security-audit-2026-05-09-p1-core.md`

**为什么重要**：CLAUDE.md 明确"没有迁移机制，create_all 不改已有列"。意味着字段类型/默认值/约束变更后，老库不会同步——可能导致：1) 默认值不一致（NULL vs 给定值）；2) 旧数据违反新约束被忽略；3) 索引/外键缺失。

- [ ] **Step 1：通读 models/base.py，列出所有 model 与字段**

- [ ] **Step 2：定位最近变更的字段**

```bash
git log -p --all -- backend/app/models/base.py | head -300
```

记录最近 30 天内修改过的字段。

- [ ] **Step 3：对每个修改过的字段评估生产风险**

- [ ] 改了类型（如 `Integer` → `Numeric`）：生产 DB 仍是旧类型，应用读取可能精度丢失或失败
- [ ] 改了默认值：旧数据 NULL，新逻辑可能崩
- [ ] 加了 NOT NULL：旧 NULL 数据在 SELECT 时会被新代码假设非空
- [ ] 加了 UNIQUE / 索引：生产 DB 没有，性能/正确性差异
- [ ] 加了外键：生产 DB 没有，可能存在悬空引用

- [ ] **Step 4：评估"无迁移"机制本身的风险**

无迁移 = 上线时数据库变更靠 SQL 手工。是否有人/流程能保证手工 SQL 一定执行？这本身是 P1 级别风险，但属于流程风险，记入报告。

- [ ] **Step 5：写报告 + commit**

```bash
git add docs/security-audit-2026-05-09-p1-core.md
git commit -m "docs(secaudit): 阶段 1 / Task 10 models 无迁移风险"
```

---

## Task 11：静态工具运行 + triage

**Files:**
- Read: bandit / semgrep 输出（临时文件，**不 commit**）
- Modify: `docs/security-audit-2026-05-09-p1-core.md`

**为什么重要**：覆盖人眼可能漏掉的常见模式。但工具产生噪声，必须 triage。

- [ ] **Step 1：检查工具是否已装**

```bash
which bandit || pip show bandit 2>/dev/null || echo "MISSING bandit"
which semgrep || pip show semgrep 2>/dev/null || echo "MISSING semgrep"
```

如缺，**用本地 venv 装**，不污染 `requirements.txt`：

```bash
python3 -m venv /tmp/secaudit-venv
/tmp/secaudit-venv/bin/pip install bandit semgrep
```

后续命令都走 `/tmp/secaudit-venv/bin/<tool>`。

- [ ] **Step 2：跑 bandit**

```bash
cd /data/sunyunbo/www/TouhouCCB
/tmp/secaudit-venv/bin/bandit -r backend/app -ll -f json -o /tmp/bandit.json
/tmp/secaudit-venv/bin/bandit -r backend/app -ll
```

- [ ] **Step 3：跑 semgrep**

```bash
/tmp/secaudit-venv/bin/semgrep --config=p/python --config=p/owasp-top-ten --config=p/security-audit backend/ --json -o /tmp/semgrep.json
/tmp/secaudit-venv/bin/semgrep --config=p/python --config=p/owasp-top-ten --config=p/security-audit backend/
```

- [ ] **Step 4：triage**

逐条工具告警：
- 真问题 → 进报告"静态工具 triage 结果"章节，按 P0–P3 标注
- 误报 → 报告里列在"已审 = 误报"小节，简要说明为什么是误报
- **不把 `/tmp/bandit.json` 等原始文件 commit**

- [ ] **Step 5：写报告 + commit**

```bash
git add docs/security-audit-2026-05-09-p1-core.md
git commit -m "docs(secaudit): 阶段 1 / Task 11 静态工具 triage"
```

---

## Task 12：阶段 1 总结 + 阶段报告收口

**Files:**
- Modify: `docs/security-audit-2026-05-09-p1-core.md`（填写执行摘要 + 统计）
- Modify: `docs/ralph-log.md`（追加阶段总结）

- [ ] **Step 1：填写执行摘要**

在报告顶部"执行摘要"段落写：

```markdown
本阶段共审查 N 个领域，发现 P0 X 条 / P1 Y 条 / P2 Z 条 / P3 W 条。
最关键发现：[一句话最严重的]。
建议立即修复：[P0 列表]。
```

- [ ] **Step 2：填写阶段统计**

按等级 + 子系统聚合的小表。

- [ ] **Step 3：列出"已识别但本阶段不审"**

把扫到但属于阶段 2/3 的线索归类（前端 XSS / 限速 / SSE 等），方便后续轮次直接接手。

- [ ] **Step 4：报告状态从"进行中"改为"已完成"**

- [ ] **Step 5：追加 ralph-log**

```markdown
## 2026-05-XX HH:MM — 安全审计阶段 1 完成
**目标 / 动机**：见 spec §4.1
**范围**：完成 11 个审计领域，仅文档
**改动**：
- `docs/security-audit-2026-05-09-p1-core.md`：阶段 1 完整报告
**风险 & 回滚**：仅文档
**验证**：报告自查口径一致 / 工具产物未 commit
**下一轮**：等用户 review；如有 P0 起补丁轮次；否则进阶段 2（传统 web 漏洞）
```

- [ ] **Step 6：最终 commit**

```bash
git add docs/security-audit-2026-05-09-p1-core.md docs/ralph-log.md
git commit -m "docs(secaudit): 阶段 1 完成（业务核心+认证授权）

最终统计：见报告执行摘要。
不修复，issue 清单等阶段 2/3 完成后合并到 summary。"
```

- [ ] **Step 7：汇报给用户（不 push、不合并）**

按 CLAUDE.md "每轮结束一句话"格式：改了什么 / 在哪个分支 / 验证结果 / 未决风险。
**询问用户**：
- 阶段报告 OK 吗？
- 是否需要为某条 P0/P1 立刻起补丁轮次？
- 是否进入阶段 2？

**不主动 push、不主动 merge 到 main、不主动起补丁。**

---

## Self-Review 检查（Plan 写完后我自己跑）

- [x] **Spec 覆盖**：spec §4.1 的 11 个 checklist 项，每条对应到至少一个 Task（Task 1=LMSR, Task 2=资金一致性, Task 3=持仓估值, Task 4=贷款, Task 5=兑换码, Task 6=SSO, Task 7=首位 admin, Task 8=admin gate, Task 9=IDOR, Task 10=models/base.py, Task 11=静态扫）✅
- [x] **占位符扫描**：无 TBD / TODO / "fill in details"（Task 1-12 每个 Step 都给了具体命令或具体问题清单）✅
- [x] **类型一致性**：所有引用的文件路径与 Bash 已验证的真实路径一致；分支名 `ralph/2026-05-09-secaudit-p1-core` 全文一致；报告路径 `docs/security-audit-2026-05-09-p1-core.md` 全文一致 ✅
- [x] **护栏一致性**：Task 6/7/10 明确标注"只读不改"；Task 11 明确"不污染 requirements.txt、原始 JSON 不 commit"；Task 12 明确"不主动 push、不主动 merge" ✅

## 关于 TDD 适用性的说明

writing-plans 默认是 TDD-first。**本计划是审计计划，没有产品代码改动**，因此每个 Task 的"步骤"是"读文件 → 对照危险模式 → 写报告 → commit"，不写测试代码。这是有意为之，与 spec §6 "审计期间不修复"一致。如发现需要修复的项，**等阶段全部完成后由用户决定是否单独起补丁轮次**，那个轮次会回到正常 TDD 流程。
