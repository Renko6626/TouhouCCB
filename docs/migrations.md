# 数据库迁移工作流（Alembic）

本项目使用 [Alembic](https://alembic.sqlalchemy.org/) 管理数据库 schema 变更。
Phase 1 安全审计 P2-M10-1 修复。

## 一次性配置（每个环境）

已有数据库（dev / staging / prod）需要标定到 alembic baseline：

```bash
cd backend
alembic stamp head
```

这告诉 alembic「我的 DB 已经在 baseline 这个状态了」，不会重建已存在的表。
**必须先做这一步**，否则后续 upgrade 会报「表已存在」。

新环境（全新空 DB）不需要 stamp，可以直接 `alembic upgrade head`，
baseline 是空的，所以是 no-op；后续真正的 schema 变更才会执行。
全新 dev DB 目前仍然走 `init_db.py`（create_all），不经过 alembic。

## 添加 schema 变更

1. 在 `app/models/base.py` 或 `app/models/redemption.py` 改字段。
2. 自动生成迁移：
   ```bash
   cd backend
   alembic revision --autogenerate -m "描述这次变更"
   ```
3. **人工 review** `alembic/versions/<新文件>.py`：
   - 删多余的 drop / 改动（autogen 有时太激进，尤其面对 server_default）
   - 加 backfill 逻辑（如果新字段是 NOT NULL 且需要默认数据）
   - 改 server_default、index、constraint 等
   - SQLite 对 ALTER 支持差，必要时用 `with op.batch_alter_table(...)`
4. 本地试跑：
   ```bash
   alembic upgrade head
   ```
   验证字段确实加上了，应用代码也跑得通。
5. commit 迁移文件 + 模型改动一起进 PR（一个原子改动）。

## 部署侧

部署脚本（CI / docker entrypoint）在启动应用前跑：
```bash
alembic upgrade head
```
幂等：DB 已经在 head 时不会做任何事。

## 回滚

```bash
alembic downgrade -1                  # 回退一步
alembic downgrade <revision>          # 回退到指定版本
alembic history                       # 看所有 revision
alembic current                       # 看当前 DB 在哪个 revision
```

注意：**生产数据回滚通常不安全**（删列会丢数据）。
回滚作为应急用，平常应该是「加一个修正 migration」往前走。

## 驱动注意

alembic 自身是同步驱动，但 app 跑的是 async 驱动。`alembic/env.py` 会自动把：

- `postgresql+asyncpg` → `postgresql+psycopg2`
- `sqlite+aiosqlite` → `sqlite`
- `mysql+asyncmy` → `mysql+pymysql`

如果你换了 driver 前缀，记得同步改 env.py。
sync 驱动需要单独装（生产 Postgres 环境需要 `psycopg2` / `psycopg2-binary`）。

## 与 init_db.py 的关系

`backend/init_db.py` 仍然存在，用于「从零起一个空 DB」（dev 重建场景）。
它走 `create_all`，绕过 alembic。

生产环境**不要**用 `init_db.py`，用 `alembic upgrade head` 接管。

长期目标：把 init_db.py 也改成走 alembic（一步到位 upgrade head），
但需要确认所有环境都 stamp 完了再改。
