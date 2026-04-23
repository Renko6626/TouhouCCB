# TouhouCCB — Ralph 模式护栏

**生产站正在跑**：FastAPI + Postgres + Vue 3 + Casdoor SSO，push `main` 会自动部署上线。

---

## 🚫 红线（停下问用户）

**命令**：`rm -rf` / `git push` / `git push -f` / `git reset --hard` / `git commit --amend` 已 push 的 / `git clean -fd` / `docker compose down -v`（`-v` 清 pgdata 卷=全量数据丢失）/ `docker volume rm` / 在有数据库上跑 `init_db.py` / 任何 `DROP` `TRUNCATE` / `--no-verify`

**文件**（未经授权别动）：`.env*`（除 `.env.example`）、`backups/`、`backend/data/`、`docker-compose.yml`、`deploy/`、`.github/workflows/`、`backend/init_db.py`、`backend/app/core/{config,users,oidc,admin}.py`

**行为**：
- 不在 `main` 直接提交 → 新分支 `ralph/<date>-<topic>`
- **不 push**（push = 自动部署）
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
- `backend/app/models/base.py` — **没有迁移机制**，`create_all` 不改已有列，别随便改字段
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

只调研未改代码也要写一条。动手前先翻 `docs/` 有没有相关历史（`README.md` / `api.md` / `development.md` / `style.md` / `*-review-*.md`）。

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
- 遇到需要 push / 改 .env / 改 deploy 或 CI / 动 schema / 升级主版本 / 删数据 → **停下问**
- 方向错了立即停，不要硬撑；拿不准默认行为是停下问，不是先干再说
