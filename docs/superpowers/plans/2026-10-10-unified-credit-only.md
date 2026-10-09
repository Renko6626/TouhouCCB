# Unified Credit Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement the owned tasks. Steps use checkbox syntax for tracking.

**Goal:** 删除旧贷款模式及旧接口字段，统一信贷成为唯一内核。
**Architecture:** 复用现有 credit 风险与执行器，移除 mode-off 分支；业务运营开关独立保留。一次性迁移废弃配置，账户数据不变。
**Tech Stack:** Existing FastAPI/SQLModel/SQLAlchemy/Alembic, Vue/TypeScript.
**Spec:** `docs/superpowers/specs/2026-10-10-unified-credit-only-design.md`

## Global Constraints

- 已确认彻底删除旧执行路线和旧 API 字段，不留恒定 mode flag 或兼容分支。
- 真实借还款、外币欠币/锁金、账户级风控、审计、经济版本、ownership、只读与恢复保护保留。
- 不清库、不重置任何账户，不自动打开任何业务闸门；新库 `loan_enabled=false`。
- 保留现有合法统一参数；新库默认 `2 / 0.2`。非法显式参数或旧参数映射报错。
- UI 验证无自动交互；测试遵守 AGENTS，保留有效金融断言。
- Implementers GPT-6.1 Sol low; reviewers GPT-6.1 Sol medium. Workers do not push/merge/deploy or spawn agents.

## Review Focus

- 旧总闸 false 的已重建库必须升级为唯一统一内核，同时其他关闭开关与冻结保持。
- 所有经济入口移除 mode 分支后仍要求所有权；只读和锁丢失拒绝写入。
- 外币回补成本未知时保留 blocked/None，不能按零债务处理。
- 统一强平 scheduler、死锁识别、事件与 run/action 幂等保留，旧执行器彻底删除。
- 后端响应字段和前端类型、页面、store 同步；套餐不修改运行期日利率。

### Task 1: 唯一后端执行路线

**Ownership:** `backend/app/**` except `services/loan_migrate.py` and `api/v1/site_config.py`; backend tests except `test_credit_config.py`, `test_site_config_api.py`, migration/rebuild tests and global conftest. Global conftest edits require coordination with root.
**Interfaces:** Remove CreditFlags mode field and enable-request fallback; valid defaults support test construction, persisted flags loading requires valid thresholds. LoanQuotaResponse replaces leverage_k with credit_leverage. UserSummary/policy remove obsolete mode and old margin fields per spec. Task 2 consumes these fields, Task 3 supplies seed/load config.

- [x] Remove mode gates and use existing unified execution in loan/user/admin/stats, LMSR/FX, redemption/danmuku/PvE; retain business gates and ownership.
- [x] Remove legacy liquidation service and writer command/handler registrations, preserve shared event/deadlock/scheduler functions.
- [x] Make startup require valid credit config and ownership for writing, always start writer for owner, disable raw economic CRUD.
- [x] Update/reuse affected tests; remove exclusively obsolete-mode tests while keeping independent financial invariants.
- [x] Run owned meaningful backend checks on a unique disposable DB and report commands/results. Root handles integration and full suite.

### Task 2: 唯一前端语义

**Ownership:** `thccb-frontend/**` only.
**Interfaces:** Loan quota `credit_leverage` string; summary unified risk fields; liquidation policy fields from spec. No unified_credit_enabled/leverage_k/legacy/old margin fallbacks.

- [x] Update API types, store and all dependent views/cards to consume only unified fields.
- [x] Replace old-config presets with direct nominal leverage/maintenance pairs (2/.2,3/.2,4/.15,6/.1,10/.04), keep valid save order and maintenance notices; no rate changes in preset.
- [x] Remove old metadata, flags and old-mode-only tests; preserve actual store/presentation assertions.
- [x] Run existing frontend unit tests and `npm run build` (includes typecheck), report results.

### Task 3: 配置升级与工具

**Ownership:** `backend/app/services/loan_migrate.py`, `backend/app/api/v1/site_config.py`, `backend/alembic/**` new migration only, `backend/scripts/**`; tests `test_credit_config.py`, `test_site_config_api.py`, migration/rebuild-related tests (not core API test files). Current runbook docs only as needed. Coordinate filenames if shared tests encountered.
**Interfaces:** Task 1 flags no mode field, parse requires valid thresholds. `seed_credit_risk_configs` may be renamed if its consumers updated. Fresh defaults explicit 2/.2; preserve existing valid parameters and existing gates. Configuration whitelist uses only retained keys. Rebuild GATES excludes removed mode; loan_enabled default false.

- [x] Add a new data migration that preserves accounts and valid unified parameters, safely maps missing unified params from legacy values, validates, then removes obsolete keys; no historical migration edits.
- [x] Seed current configuration directly; prevent startup seeding deleted keys. Shared parameter/gate defaults follow spec.
- [x] Update config API validation and scripts/rebuild verify; remove obsolete shadow/legacy-only tools or adapt retained tools to current config.
- [x] Update real migration/startup/rebuild behavior tests; preserve identity/entitlement/data constraints, verify invalid config fails and gate values unchanged.
- [x] Run related tests on unique disposable DB; report compatibility concerns and exact output.

### Integration and review

- [x] Root inspects reports/diffs, resolves cross-task type/import/test issues without altering approved route.
- [ ] Run retained full backend suite once, frontend tests/build, relevant migration checks. Fix concrete failures, do not remove valid assertions.
- [x] Independent medium review of final diff and task constraints; address blockers and scoped re-review.
- [ ] Commit reviewable work and push branch; report commands, results, unresolved limitations. User authorizes root to merge and deploy via existing CI/CD after review; workers do not access production.
