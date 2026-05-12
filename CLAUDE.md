# TouhouCCB — Ralph 模式护栏

**生产站正在跑**：FastAPI + Postgres + Vue 3 + Casdoor SSO，push `main` 会自动部署上线。

**单人开发**：唯一 contributor 是项目所有者，没有 PR review 流程。所以这些护栏不是为团队协作，而是防自己手抖：保护资金、数据、生产稳定。Trivial 改动不需要新分支、不需要冗长 ceremony。

---

## 🚫 红线（停下问用户）

**命令**：`rm -rf` / `git push` / `git push -f` / `git reset --hard` / `git commit --amend` 已 push 的 / `git clean -fd` / `docker compose down -v`（`-v` 清 pgdata 卷=全量数据丢失）/ `docker volume rm` / 在有数据库上跑 `init_db.py` / 任何 `DROP` `TRUNCATE` / `--no-verify`

**文件**（未经授权别动）：`.env*`（除 `.env.example`）、`backups/`、`backend/data/`、`docker-compose.yml`、`deploy/`、`.github/workflows/`、`backend/init_db.py`、`backend/app/core/{config,users,oidc,admin}.py`

**行为**：
- **小修补**（≤2 文件 / 单一原因 / 不动业务逻辑 / 不动红线和高敏感文件 / 不改 schema）：可以直接在 `main` commit + push，前提是 `🔍 声称完成前必跑` 那套验证全过
- **非小修补**（多文件改动 / 业务逻辑 / 高敏感文件 / 加列改字段 / 大重构 / 跨前后端）：新分支 `ralph/<date>-<topic>`；验完再合 main 再 push
- **任何 push 默认会触发自动部署上线**——可以做，但要清楚自己在干什么；有疑虑就先合到分支让用户看一眼
- 不引入新框架/新 UI 库（栈约束见 `docs/development.md`）
- 不用 `any` 绕类型、不改 `tsconfig` 放宽检查
- 不顺手重构无关代码、不删看似没用但没验证过引用的代码
- 不升级依赖主版本

---

## ⚠️ 高敏感（动前在 ralph 日志说明为什么）

- `backend/app/services/lmsr.py` — 定价核心，`Decimal` 精度 6/8 位，改错全站估值错乱
- `backend/app/services/realtime.py` — SSE 广播
- `backend/app/api/v1/market.py` — 买卖/报价、资金与滑点
- `backend/app/api/v1/auth.py` — 改错全员无法登录
- `backend/app/models/base.py` — 高敏感字段定义；现在有 alembic 接管迁移（详见 `docs/migrations.md`），加列/改字段时**必须**走 `alembic revision --autogenerate` 流程，不要再裸改
- 前端 `stores/`、`src/api/`、`router/`、`vite.config.ts`、`uno.config.ts`

---

## ✅ 安全区

`pages/` `components/` `composables/` `utils/` `types/` `backend/tests/` `docs/` — 仍守 `docs/development.md` 栈约束与 `docs/style.md` 设计系统（工业风黑白、无圆角、粗边框、涨绿跌红）。

---

## 📝 每轮写 `docs/ralph-log.md`（追加）

```markdown
## YYYY-MM-DD HH:MM — 一句话标题
**目标** / **动机**（证据）/ **范围**（仅限 xxx）
**改动**：- `path`：做了 X，因为 Y
**风险 & 回滚**
**验证**：type-check ✅ / lint ✅ / 手测路径
**下一轮**
```

动了产品代码 / schema / 部署配置 / 业务策略 → **必写一条**。
单纯探查源码、改 typo、调注释、加 import 之类的 trivial 改动可省略，但每轮结束的一句话总结仍要给。
动手前先翻 `docs/` 有没有相关历史（`README.md` / `api.md` / `development.md` / `style.md` / `*-review-*.md` / `migrations.md` / `security-audit-*.md`）。

---

## 🔍 声称完成前必跑

前端：`npm run type-check` + `npm run lint`（涉及构建/依赖时加 `npm run build`）
后端：`python -m py_compile $(find app -name '*.py')` + `python -c "import app.main"` + `pytest -x`
UI 改动：**浏览器实测**主路径+边界态（空/加载/错/未登录/移动端）；环境起不来就在日志写「未实测 UI」，不得谎称通过。

**没证据 = 没完成。**

---

## 🧠 业务域避坑

- **LMSR**：任一选项成交影响全市场价格；图表按全市场逐笔重放，不只看目标选项
- **持仓估值**：LMSR 清算价值（含卖出滑点+手续费），**不是**瞬时价×数量（最近修正 `4a49d2e`）
- **认证**：第一个 SSO 登录的自动超管，别加「管理员创建接口」
- **精度**：后端资金/份额 6 位、价格 8 位 Decimal，前端别用 `Number()` 把精度丢了
- **限速**：`/auth` 5r/s、`/market/{buy,sell,quote}` 10r/s、`/admin` 2r/s
- **SSE**：优雅关闭 `stop_grace_period: 8s`

---

## Git 与沟通

- commit 粒度：一个可独立回滚的改动 = 一条 commit；消息风格参考 `git log`（`feat:/fix:/refactor:/style:/docs:` + 中文）
- 按文件 `git add <path>`，不用 `-A` / `.`（避免误入 `.env` `dist/` `backups/` `*.db`）
- 每轮结束一句话：改了什么 / 在哪个分支 / 验证结果 / 未决风险，细节进日志
- 遇到改 .env / 改 deploy 或 CI / 动 schema（含加列）/ 升级主版本 / 删数据 / **风险改动直接 push 到 main** → **停下问**
  （小修补 push 不再必停；动业务逻辑/高敏感文件/跨前后端的改动直接 push 上 prod 仍然要停下问）
- 方向错了立即停，不要硬撑；拿不准默认行为是停下问，不是先干再说
