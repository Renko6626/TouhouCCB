# 本地开发 Onboarding 指南

东方 Project 主题 LMSR 预测市场，技术栈：FastAPI + PostgreSQL + Vue 3 + Casdoor SSO。

---

## 前置依赖

| 工具 | 版本 | 用途 |
|------|------|------|
| Python | 3.13 | 后端运行时 |
| Node.js | 22（或 ≥ 20.19.0） | 前端构建 |
| Git | 任意 | 版本管理 |

本地开发默认使用 **SQLite**，无需安装 PostgreSQL 或 Docker。
如需使用 Docker Compose 跑完整生产镜像，参见 `docs/deploy.md`。

---

## 克隆与环境配置

```bash
git clone <repo-url>
cd TouhouCCB

# 复制环境变量模板
cp .env.example .env
```

**本地最小配置**（用 SQLite，不配 Casdoor）只需在 `.env` 里确认以下几项：

```dotenv
APP_ENV=development

# 使用 SQLite，无需额外服务
DB_BACKEND=sqlite
SQLITE_PATH=data/thccb.db

# JWT 密钥（开发环境留空时会自动生成随机值，不会报错）
SECRET_KEY=

# Casdoor 留空 —— 开发环境不配置时只打 warning，应用照常启动
# OAuth 登录不可用，但其余 API 端点可以正常测试
CASDOOR_ENDPOINT=
CASDOOR_CLIENT_ID=
CASDOOR_CLIENT_SECRET=

# 前端开发服务器地址（Vite 默认）
FRONTEND_URL=http://localhost:5173
CORS_ORIGINS=http://localhost:5173
```

> `.env` 文件不要提交到 Git（已在 .gitignore 中排除）。

---

## 后端启动

```bash
cd backend

# 1. 创建 & 激活虚拟环境
python3.13 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 初始化数据库（仅第一次，或需要重置时）
#    ⚠️ 红线：此脚本会清空数据库，生产环境绝不可执行
python init_db.py

# 4. 启动开发服务器（端口 8004）
uvicorn app.main:app --host 127.0.0.1 --port 8004 --reload
```

后端 API 服务运行在 **`http://localhost:8004`**。
Swagger 文档：`http://localhost:8004/docs`。

也可以用 `run.py` 启动（不带 --reload）：

```bash
python run.py
```

---

## 前端启动

前端代码在 `thccb-frontend/` 子目录下。

```bash
cd thccb-frontend

# 1. 安装依赖
npm install

# 2. 启动开发服务器（端口 5173）
npm run dev
```

前端运行在 **`http://localhost:5173`**。
Vite 已配置代理：所有 `/api/*` 请求自动转发到 `http://localhost:8004`，无需手动配置。

> 前端 `src/api/` 里的 `VITE_API_BASE_URL` 不需要在本地显式设置，代理已经处理。

---

## 数据库

### SQLite（本地开发默认）

设置 `DB_BACKEND=sqlite`，应用启动时会自动在 `backend/data/thccb.db` 创建 SQLite 文件，无需其他操作。

### 数据库初始化 & 迁移

**全新环境**：用 `init_db.py` 建表并插入示例市场（见上方"后端启动"步骤 3）。

**已有库、只需应用 schema 变更**（新增列等）：

```bash
cd backend
alembic upgrade head
```

详细迁移工作流（生成迁移文件、review、回滚等）参见 `docs/migrations.md`。

> 向 schema 加列/改字段时，必须走 `alembic revision --autogenerate` 流程，不要裸改模型后直接跑应用。

---

## Casdoor SSO 本地说明

Casdoor 是本项目的唯一登录入口（OAuth 2.0 OIDC）。

**不配置时**：`APP_ENV=development` 下后端会打印一条 warning 并正常启动，
Casdoor OAuth 登录不可用，但其他 API（市场列表、行情等）可正常调用。本地要登录调试，
用下面的 **dev mock 登录**，无需搭 Casdoor。

### dev mock 登录（仅本地）

非生产环境提供一个免凭据登录入口，方便不配 Casdoor 也能登录调试：

- **前端**：开发模式（`npm run dev`）下，登录页会出现「DEV 登录」表单，填个用户名点登录即可
  （首个登录的用户自动成为超管，余额 = `initial_balance`）。
- **后端接口**：`POST /api/v1/auth/dev-login`，body `{"username": "dev"}`，返回与 OAuth 回调
  同结构的 token：

  ```bash
  curl -X POST http://localhost:8004/api/v1/auth/dev-login \
    -H 'Content-Type: application/json' -d '{"username":"dev"}'
  ```

> ⚠️ 该接口在 `APP_ENV=production` 下一律返回 **404**（防 auth bypass 泄漏到生产）。
> 切勿移除 `backend/app/api/v1/auth.py::dev_login` 里的 `is_production` guard。

**需要完整登录流程时**：需要自建或复用一个 Casdoor 实例，在 `.env` 填入：

```dotenv
CASDOOR_ENDPOINT=https://your-casdoor-instance.example.com
CASDOOR_CLIENT_ID=your-client-id
CASDOOR_CLIENT_SECRET=your-client-secret
```

三个字段必须同时填写，否则后端启动时会报 `ValueError: Casdoor 配置不完整`。

完整 Casdoor 自建流程参见 `docs/deploy.md`（待完善）。

---

## 跑测试

测试在 `backend/` 目录下执行，使用独立测试数据库，不会影响开发库。

```bash
cd backend
source venv/bin/activate

pytest -x
```

配置要点（`pytest.ini`）：
- `asyncio_mode = strict`，所有 async 测试须显式标注
- 单测超时 30s，超时自动终止
- 全套约 80s 跑完

运行特定模块：

```bash
pytest tests/test_market.py -x -v
```

排除慢测试：

```bash
pytest -m "not slow"
```

---

## 提交前验证

**没跑验证 = 没完成。** 在 claim 完成或提交 commit 前，必须全过：

### 后端

```bash
cd backend
source venv/bin/activate

# 语法检查
python -m py_compile $(find app -name '*.py')

# 导入检查（触发 lifespan 之前的所有初始化错误）
python -c "import app.main"

# 测试
pytest -x
```

### 前端

```bash
cd thccb-frontend

# TypeScript 类型检查
npm run type-check

# Lint
npm run lint

# 涉及构建产物或依赖变更时，额外跑构建
npm run build
```

### UI 改动

浏览器实测主路径 + 边界态（空态、加载中、错误、未登录、移动端尺寸）。
环境起不来时，在 commit message 或 PR 里注明「未实测 UI」，不得谎称通过。

---

## 技术栈约束（硬约束）

### 前端

- 必须使用：**Vue 3 + TypeScript + Naive UI + UnoCSS + Pinia + Axios**
- 图表：lightweight-charts（K 线）/ ECharts（走势图）
- 不引入与当前栈冲突的新框架或 UI 库
- 所有接口交互经过 `src/api/` 封装
- 页面状态放 `stores/` 或 `composables/`，不散落在页面组件里
- 新增类型落在 `src/types/`，禁止大量 `any`
- 不修改 `tsconfig` 放宽类型检查

### 后端

- Python 3.13，FastAPI，SQLModel / SQLAlchemy 2.0，Alembic
- 资金/份额精度 6 位 Decimal，价格 8 位；前端不用 `Number()` 丢精度
- 反向集合关系一律 `lazy="raise_on_sql"`，查集合必须显式 `selectinload`（详见 `CONTRIBUTING.md`）
- 不升级依赖主版本

---

## 代码组织原则

- `src/api/*.ts`：处理请求细节和参数转换
- `stores/` / `composables/`：处理业务状态与副作用
- 页面组件只做展示与交互编排（保持"薄"）
- 抽离可复用业务组件到 `components/`
- 对后端不稳定字段做兜底处理（默认值、空态）

---

## 开发流程

1. 明确任务目标、涉及页面、涉及 API、验收标准
2. 只读和任务相关的文件
3. 实施改动，优先小步修改（先通路，后优化）
4. 执行上方"提交前验证"全套命令
5. 更新 `docs/README.md` 中的完成状态（如有变化）

---

## 权威信息源（按优先级）

1. `docs/api.md` — 后端 API 规范
2. `docs/README.md` — 项目状态与功能清单
3. `docs/migrations.md` — 数据库迁移工作流
4. `docs/deploy.md` — 完整部署与 Casdoor 自建流程
5. 当前代码实现（与文档不一致时以代码为准）
