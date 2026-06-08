# PR5 质量收尾 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐 task 执行。步骤用 `- [ ]` 勾选。
> 计划存于 `docs/archive/`（PR3 已删 `docs/superpowers/`，不重建）。

**Goal:** 在不改业务行为的前提下，清理开源前的代码债，提升可维护性。

**Architecture:** PR5 是一批**独立清理**，按风险/价值拆成 5a（本计划：已核实的低风险清理）+ 5b/5c/5d（各需独立计划/brainstorm，见末尾路线图）。每个 task 自包含、可独立回滚。

**Tech Stack:** FastAPI/pytest（后端）、Vue3/vue-tsc/eslint（前端）、uv（quant）。

**前置**：新分支 `feat/2026-06-08-pr5-cleanup`（栈在 deploy-docs 之上或合栈后基于 main，由执行时定）。每 task 后端跑 `pytest -x`、前端跑 `npm run type-check`，全绿才 commit。

---

## 核实结论（计划据此，已 grep 验证 2026-06-08）

- ✅ `schemas/base.py` 的 `MarketRead`/`OutcomeRead` 全仓无引用 → 死代码，可删
- ✅ 前端 `stores/title.ts` 无 import → 死代码，可删
- ⚠️ `stores/notification.ts` **被 `App.vue` 使用** → **不是死代码，保留**（审计此处有误）
- ✅ `backend/scripts/{bench_chart.py,verify_chart_numpy_replay.py}` 无引用 → 可删
- ✅ `quant/` 无 lock 文件（只有 `pyproject.toml`）→ 补 `uv.lock`
- ⚠️ `docs/latex/` 已有 `.gitignore`、无编译产物被 track（`rulebook.pdf` 是有意分发物）→ **该审计项是伪命题，不做**
- ⚠️ 废依赖（fastapi_users/pwdlib/argon2/itsdangerous/wtforms）虽 0 直接 import，但 `sqladmin` 在用、`wtforms`/`itsdangerous` 是其传递依赖 → **删依赖须用 pipdeptree 验证**，移到 5b
- ⚠️ `vite-plugin-vue-devtools` 本就 dev-only，且 `vite.config.ts` 是高敏感文件 → 降级为存疑项，移到路线图

---

## PR5a：低风险清理（本计划详细执行）

### Task 1：删除死代码后端 schema MarketRead/OutcomeRead

**Files:**
- Modify: `backend/app/schemas/base.py`（删 `OutcomeRead`、`MarketRead` 两个 class）

- [ ] **Step 1：再次确认无引用**

Run: `cd backend && grep -rn "MarketRead\|OutcomeRead" app/ | grep -v "schemas/base.py"`
Expected: 无输出（仅定义处，无使用）

- [ ] **Step 2：删除两个 class**

打开 `backend/app/schemas/base.py`，删除 `class OutcomeRead(BaseModel): ...` 与 `class MarketRead(BaseModel): ...` 两段（约 14-30 行区域，以实际为准）。若删后 `base.py` 出现未用 import（如 `List`），一并清理。

- [ ] **Step 3：编译 + import + 回归**

Run: `cd backend && ./venv/bin/python -m py_compile $(find app -name '*.py') && ./venv/bin/python -c "import app.main" && ./venv/bin/python -m pytest -q`
Expected: compile OK / import OK / 全套 passed（基线 367）

- [ ] **Step 4：Commit**

```bash
git add backend/app/schemas/base.py
git commit -m "refactor: 删除未引用的 MarketRead/OutcomeRead schema"
```

### Task 2：删除死代码前端 store title.ts

**Files:**
- Delete: `thccb-frontend/src/stores/title.ts`

- [ ] **Step 1：确认无 import**

Run: `cd thccb-frontend && grep -rn "stores/title\|useTitleStore" src/ | grep -v "stores/title.ts:"`
Expected: 无输出

- [ ] **Step 2：删除文件**

```bash
git rm thccb-frontend/src/stores/title.ts
```

- [ ] **Step 3：type-check**

Run: `cd thccb-frontend && npm run type-check`
Expected: 无报错（vue-tsc 干净）

- [ ] **Step 4：Commit**

```bash
git commit -m "refactor(frontend): 删除未使用的 title store"
```

### Task 3：env.d.ts 补全 VITE_* 类型声明

**Files:**
- Modify: `thccb-frontend/env.d.ts`

- [ ] **Step 1：读现状**

Run: `cat thccb-frontend/env.d.ts`
看是否已有 `ImportMetaEnv` interface 及哪些 VITE_* 已声明。

- [ ] **Step 2：补全声明**

确保 `env.d.ts` 含如下接口（已存在的字段不重复；缺的补上）：

```typescript
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  readonly VITE_APP_TITLE?: string
  readonly VITE_CASDOOR_URL?: string
  readonly VITE_CASDOOR_CLIENT_ID?: string
  readonly VITE_CASDOOR_ORG?: string
  readonly VITE_CASDOOR_APP?: string
  readonly VITE_CLIENT_TOKEN_SECRET?: string
}
interface ImportMeta {
  readonly env: ImportMetaEnv
}
```

- [ ] **Step 3：type-check**

Run: `cd thccb-frontend && npm run type-check`
Expected: 干净（且各处 `import.meta.env.VITE_*` 现在有类型）

- [ ] **Step 4：Commit**

```bash
git add thccb-frontend/env.d.ts
git commit -m "chore(frontend): 补全 VITE_* 环境变量类型声明"
```

### Task 4：清理 stream.ts 生产 console 噪音

**Files:**
- Modify: `thccb-frontend/src/api/stream.ts`

保留 `console.error` / `console.warn`（真异常需可见）；把纯信息日志（line 28/48/132/139/155 的 `console.log`、line 155 `console.debug`）改为仅 dev 输出。

- [ ] **Step 1：替换为 dev 守卫**

把这些信息级日志包到 `if (import.meta.env.DEV)`，例如：
```typescript
// 原：console.log(`Opening market stream: ${marketId}`)
if (import.meta.env.DEV) console.log(`Opening market stream: ${marketId}`)
```
对 line 28/48/132/139 的 `console.log` 与 155 的 `console.debug` 各做同样处理。`console.error`/`console.warn` 不动。

- [ ] **Step 2：type-check**

Run: `cd thccb-frontend && npm run type-check`
Expected: 干净

- [ ] **Step 3：Commit**

```bash
git add thccb-frontend/src/api/stream.ts
git commit -m "chore(frontend): SSE 信息日志仅 dev 输出，去生产噪音"
```

### Task 5：删除无引用的一次性 dev 脚本

**Files:**
- Delete: `backend/scripts/bench_chart.py`、`backend/scripts/verify_chart_numpy_replay.py`

（`backfill_*` 与 `migrate_loan_v1.py` 是可能仍有用的 ops 工具，**保留**。）

- [ ] **Step 1：确认无引用**

Run: `cd backend && grep -rn "bench_chart\|verify_chart_numpy" app/ --include=*.py`
Expected: 无输出

- [ ] **Step 2：删除**

```bash
git rm backend/scripts/bench_chart.py backend/scripts/verify_chart_numpy_replay.py
```

- [ ] **Step 3：import 检查 + 回归**

Run: `cd backend && ./venv/bin/python -c "import app.main" && ./venv/bin/python -m pytest -q`
Expected: import OK / 全套 passed

- [ ] **Step 4：Commit**

```bash
git commit -m "chore: 删除无引用的 bench/verify 一次性脚本"
```

### Task 6：quant 补 lock 文件

**Files:**
- Create: `quant/uv.lock`

- [ ] **Step 1：生成 lock**

Run: `cd quant && uv lock`
Expected: 生成 `uv.lock`

- [ ] **Step 2：确认 .gitignore 未忽略它**

Run: `cd <repo-root> && git check-ignore quant/uv.lock`
Expected: 无输出（未被忽略；若被忽略需在根 .gitignore 加 `!quant/uv.lock`）

- [ ] **Step 3：Commit**

```bash
git add quant/uv.lock
git commit -m "chore(quant): 提交 uv.lock 锁定依赖"
```

### Task 7：前端依赖漏洞修复

**Files:**
- Modify: `thccb-frontend/package.json`、`thccb-frontend/package-lock.json`

- [ ] **Step 1：查看漏洞**

Run: `cd thccb-frontend && npm audit`

- [ ] **Step 2：自动修复（不破坏 major）**

Run: `cd thccb-frontend && npm audit fix`
（**不要**用 `--force`；会引入 major 升级，违反"不升主版本"约束。剩余需 major 才能修的漏洞留记录，不强修。）

- [ ] **Step 3：构建 + type-check 验证**

Run: `cd thccb-frontend && npm run type-check && npm run build`
Expected: 均通过（确认 fix 没破坏构建）

- [ ] **Step 4：Commit**

```bash
git add thccb-frontend/package.json thccb-frontend/package-lock.json
git commit -m "chore(frontend): npm audit fix 修复非破坏性依赖漏洞"
```

---

## 路线图：5b / 5c / 5d（各需独立计划，不在本计划详述）

按"先安全后风险、先小后大"排序，每项产出独立可测：

- **5b 测试补强 + 依赖清理**（中等，需 brainstorm 测试设计）：
  - 后端补 `lmsr` 定价单测、`resolve_market` 结算测试、SSE 端点集成测试
  - `requirements.txt` 用 `pipdeptree` 验证后删 fastapi-users 链（注意 sqladmin 传递依赖 wtforms/itsdangerous **不能删**）、pin 范围版本、拆 `requirements-dev.txt`
  - `Transaction.type` 等裸 str → `Literal[...]`
- **5c 前端类型债 + 测试**（大、繁琐）：消除 66 个 `@typescript-eslint/no-explicit-any`、引入 vitest（货币精度等关键路径）、CI 接入 lint+type-check 门禁
- **5d `market.py` 拆分**（高风险，**必须单独 brainstorm**）：1397 行上帝路由器拆 `quote_cache`/`market_stats`/`trade` 等；动资金 hot path，需充分测试护航，不可与其他清理混做
- **存疑/待定**：`vite-plugin-vue-devtools` 是否真在 prod 生效（先验证，多半 dev-only 非问题，且 `vite.config.ts` 高敏感）；`SecretStr` 加固（动 auth，单独 brainstorm）

---

## Self-Review

- 覆盖：5a 各 task 均对应已核实的清理项；删除类用"grep 无引用 + 回归全绿"做验证（删除无经典 red-green，用回归护栏）。
- 无 placeholder：每步给了命令/代码/预期。
- 一致性：保留项（notification.ts / backfill 脚本 / docs/latex / rulebook.pdf）已在核实结论标注，不被误删。
