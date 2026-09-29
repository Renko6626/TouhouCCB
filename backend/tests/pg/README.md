# tests/pg — PostgreSQL 专用测试基座（WP1）

默认 `pytest.ini` 的 `addopts` 带 `-m "not pg"`，所以整套单测不会跑这里；
PG 测试只在显式指定 `-m pg` 且给了测试库 URL 时执行。

## 跑法

```bash
cd backend
TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55439/credit_test \
    python -m pytest -q -m pg tests/pg/
```

本次实施使用的实例（父 agent 提供，见 `../.superpowers/sdd/2026-09-30-unified-credit-risk/environment.md`）：

```
postgresql+asyncpg://sunyunbo@127.0.0.1:55439/credit_test
```

- loopback + trust 认证，无密码/密钥；`credit_test` 是一次性测试库，允许 drop_all。
- `credit_baseline` 归父 agent 所有，**不要**对它跑测试。
- 未设置 `TEST_PG_DATABASE_URL` 时 `pg_engine` fixture 直接 skip（4 skipped），
  不会让 CI 变红，也不会误连生产。

## 安全护栏

`tests/pg/conftest.py` 里的 `_checked_url()` 强制库名必须含 `test`，否则抛
`UsageError` 拒绝执行 `drop_all` —— 防止手抖把 `TEST_PG_DATABASE_URL` 指向真实库。

## 可复现的起库配方（本次未由 WP1 执行，仅记录）

不改 `docker-compose.yml`；用一个独立 data dir + 非默认端口：

```bash
export PGDATA=/tmp/credit-pg
initdb -D "$PGDATA" -U sunyunbo --auth=trust
pg_ctl -D "$PGDATA" -o "-p 55439 -k /tmp -h 127.0.0.1" -l /tmp/credit-pg.log start
createdb -h 127.0.0.1 -p 55439 -U sunyunbo credit_test
```

## 覆盖内容（WP1）

- 方言断言 + `SELECT ... FOR UPDATE` 冒烟
- 新列 server_default（老行无需回填）
- `uq_liquidation_run_active_user` 部分唯一索引在 PG 上真正拦截第二个 active run
- 新 revision 在 PG 上 downgrade → upgrade 后与 `SQLModel.metadata` 零差异
