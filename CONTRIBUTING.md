# 贡献指南

欢迎参与 TouhouCCB！这是一个以东方 Project 为主题的 LMSR 预测市场游戏，由单人业余维护，生产站持续在跑。贡献前请花几分钟读完本文，避免踩坑。

---

## 开发环境

环境搭建、依赖安装、本地启动方式见 [`docs/development.md`](docs/development.md)。UI 设计规范（工业风黑白、无圆角、粗边框、涨绿跌红）见 [`docs/style.md`](docs/style.md)。

---

## 代码规范

### 栈约束

- **不引入新框架或新 UI 库**。后端是 FastAPI + SQLAlchemy + PostgreSQL，前端是 Vue 3 + UnoCSS，具体允许的依赖见 `docs/development.md`。
- **不升级依赖主版本**（例如把 SQLAlchemy 1.x 升到 2.x），版本升级需单独讨论。
- **TypeScript 类型不得用 `any` 绕过**，也不得修改 `tsconfig` 放宽检查。
- **不顺手重构 PR 之外的代码**，不删除未经验证引用的代码。每个 PR 只做一件事。

### 精度规则（重要）

后端对金额字段有严格精度要求：

| 类型 | 精度 |
|------|------|
| 资金 / 份额 | 6 位 Decimal |
| 价格 | 8 位 Decimal |

前端展示时**不要用 `Number()` 转换**，那会把 Decimal 精度丢掉。需要格式化时使用字符串操作或专用的格式化工具函数。

### Schema 变更必须走 Alembic

`backend/app/models/base.py` 由 Alembic 接管迁移，**加列、改字段类型、改约束都必须**生成迁移文件：

```bash
cd backend
alembic revision --autogenerate -m "描述你的变更"
```

不要直接裸改模型然后期待数据库自动同步。详见 [`docs/migrations.md`](docs/migrations.md)。

---

## ORM 查询守则

这是本项目**最容易踩坑**的地方，请认真阅读。

### 背景

`backend/app/models/base.py` 里以下反向集合关系全部配置了 `lazy="raise_on_sql"`：

- `User.positions` / `User.transactions`
- `Outcome.positions` / `Outcome.transactions`

**直接访问这些属性会抛异常**，除非在同一次查询中用 `selectinload()` 显式预加载过。

**为什么这样配置？** 买卖的 hot path 每秒要执行 `SELECT user FOR UPDATE` 和 `SELECT outcome FOR UPDATE` 各一次。如果用默认的 `lazy="selectin"`，每次取 user 时都会自动追加一条 SELECT 把该用户全部持仓/交易记录（活跃用户上千行）拉出来，取 outcome 时同样会把热门市场所有持仓拖出来——而 hot path 根本用不到这些数据。高并发下这是真实瓶颈。`raise_on_sql` 让问题在开发期就报错暴露，而不是悄悄吃掉性能。

### 正确写法

**取单条或带过滤的集合** — 永远用显式 `select(...).where(...)`：

```python
pos = (await db.execute(
    select(Position)
    .where(Position.user_id == uid, Position.outcome_id == oid)
    .with_for_update()
)).scalars().first()
```

**需要一个父对象的整个集合**（列表接口、批量场景）— 在 query 上挂 `selectinload()`：

```python
stmt = select(Market).options(selectinload(Market.outcomes))
```

**绝对禁止**的写法（会直接报错）：

```python
for p in user.positions:       # 禁止
len(user.transactions)         # 禁止
{"positions": user.positions}  # 禁止（response model 里隐式访问）
```

**新增关系时**，默认设 `lazy="raise_on_sql"`。只有"该模型几乎总是要与集合一起使用"的场景才考虑 `selectin`，且需要在 PR 描述里说明理由。**不要为了图方便把现有关系改回 `selectin`**——这会让 hot path 性能问题悄悄回归。

---

## 提交规范

### 分支命名

```
feat/<topic>        新功能
fix/<topic>         缺陷修复
refactor/<topic>    重构
docs/<topic>        文档
perf/<topic>        性能
style/<topic>       样式
```

### Commit 消息

一个可独立回滚的改动对应一条 commit，格式为类型前缀 + 中文说明：

```
feat: 添加市场搜索功能
fix: 修复持仓估值精度丢失问题
refactor: 拆分 market_service 为独立模块
docs: 补充 ORM 查询示例
```

### 暂存文件

按文件路径逐个 `git add <path>`，不要用 `git add -A` 或 `git add .`，避免意外提交 `.env`、`dist/`、`*.db` 等文件。

---

## 测试与验证

**提交 PR 前，以下步骤必须全部通过。**

### 前端

```bash
cd thccb-frontend
npm run type-check
npm run lint
# 涉及构建或依赖变更时还需：
npm run build
```

### 后端

```bash
cd backend
python -m py_compile $(find app -name '*.py')
python -c "import app.main"
pytest
```

### UI 改动

需要在浏览器里实测主路径和边界态：空状态、加载中、报错、未登录、移动端宽度。本地环境起不来时请在 PR 描述里如实说明，不要假称已测。

---

## 高敏感区

以下文件或目录改动影响面大，PR 描述里请说明改动理由：

| 路径 | 风险 |
|------|------|
| `backend/app/services/lmsr.py` | 全站定价核心；Decimal 精度 6/8 位，改错则全站估值错乱 |
| `backend/app/services/realtime.py` | SSE 实时推送，改动影响所有在线客户端 |
| `backend/app/api/v1/market.py` | 买卖/报价/滑点/资金逻辑 |
| `backend/app/api/v1/auth.py` | 改错可能导致全员无法登录 |
| `backend/app/models/base.py` | Schema 定义，变更必须走 Alembic autogenerate |
| `thccb-frontend/src/stores/` | 全局状态，改动影响范围广 |
| `thccb-frontend/src/api/` | 前后端接口契约 |
| `thccb-frontend/src/router/` | 路由与权限守卫 |
| `thccb-frontend/vite.config.ts` | 构建配置 |
| `thccb-frontend/uno.config.ts` | 设计 token，改动影响全站样式 |

以下文件**请勿在 PR 中修改**（属于部署/密钥/数据范畴）：

- `.env*`（`.env.example` 除外）
- `docker-compose.yml`
- `deploy/`
- `.github/workflows/`
- `backend/init_db.py`
- `backend/app/core/{config,users,oidc,admin}.py`
- `backups/`、`backend/data/`

---

## 业务逻辑提示

- **LMSR 定价**：任意一个选项成交都会影响同一市场所有选项的价格，不要孤立地只测目标选项。
- **持仓估值**：显示的是 LMSR 清算价值（含卖出滑点和手续费），不是瞬时价格 × 数量。
- **认证**：首个 SSO 登录的用户自动成为超管，不要添加"管理员创建接口"。

---

如有疑问，欢迎先开 Issue 讨论，再动手实现。
