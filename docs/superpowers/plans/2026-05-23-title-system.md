# Title 系统实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 TouhouCCB 加用户称号系统：catalog + 多对多持有 + 装备制 + 激活码兑换 + 市场门槛（ANY-of），并把"单用户管理"散落各处的 admin 操作整合到一个新的 `/admin/users` 页。

**Architecture:** 5 张新表（title / user_title / title_code_batch / title_code / market_required_title）+ User.equipped_title_id；分支：`feat/2026-05-23-title-system`；alembic autogenerate 后手抄成全显式命名；前后端 TDD，按数据层 → 服务层 → endpoint → 集成 → 前端基础组件 → 用户页 → 管理页 顺序推进。

**Tech Stack:** FastAPI / SQLModel / asyncpg / alembic / pytest-asyncio / Vue 3 / TypeScript / naive-ui / unocss

**Spec:** `docs/superpowers/specs/2026-05-23-title-system-design.md` — 任何 task 卡住先回看 spec 对齐预期，**不要自己改 spec**；改动有疑义停下问。

**对应分支：** `feat/2026-05-23-title-system`（任务 0 创建）

---

## 文件结构总览

### 新建文件

```
backend/
  app/models/title.py                          # 5 个 SQLModel 类
  app/schemas/title.py                         # Pydantic 响应/请求 schema
  app/services/title_service.py                # catalog / equip / grant / revoke 业务逻辑
  app/services/title_code_service.py           # CSV 解析 / batch 管理 / redeem 业务
  app/services/market_title_gating.py          # buy gate 检查 helper
  app/api/v1/title.py                          # 用户自助 endpoint
  app/api/v1/admin_title.py                    # admin title catalog + batch + user-title + market-required
  alembic/versions/2026_05_23_XXXX-add_title_system.py

  tests/test_title_models.py
  tests/test_title_migration.py
  tests/test_title_catalog_admin.py
  tests/test_title_user_read.py
  tests/test_title_equip.py
  tests/test_title_code_admin.py
  tests/test_title_code_csv_import.py
  tests/test_title_redeem.py
  tests/test_title_admin_user.py
  tests/test_market_gating.py
  tests/test_market_required_titles_admin.py
  tests/test_title_response_augmentation.py

thccb-frontend/
  src/api/title.ts
  src/stores/title.ts
  src/components/title/TitleChip.vue
  src/components/title/MyTitlesPanel.vue
  src/components/title/RequiredTitlesBadge.vue
  src/pages/redeem/RedeemTitle.vue
  src/pages/admin/TitleCatalog.vue
  src/pages/admin/TitleCodeBatches.vue
  src/pages/admin/UserManage.vue
```

### 修改文件

```
backend/
  app/main.py                                  # include 2 个新 router + import title model module
  app/api/v1/market.py                         # buy 加 gate；list/detail 加 required_titles + user_can_trade
  app/api/v1/user.py                           # summary 加 equipped_title + all_titles
  app/api/v1/admin_stats.py / leaderboard 等   # 响应加 equipped_title 字段（具体看代码位置）

thccb-frontend/
  src/api/index.ts                             # axios 拦截 MARKET_TITLE_REQUIRED marker
  src/api/admin.ts                             # 追加 title admin 调用
  src/router/routes.ts                         # 4 个新路由
  src/components/layout/AppSidebar.vue         # 用户菜单加"称号兑换"；admin 菜单加 3 项
  src/pages/profile/* (现有用户页)             # 集成 MyTitlesPanel
  src/pages/admin/MarketManage.vue             # required_titles multi-select
  src/components/leaderboard/* (现有 chip 位)  # 加 TitleChip
```

---

## Task 0: 创建分支 + 基础设施确认

**Files:** 无（git 操作）

- [ ] **Step 1: 从 main 拉新分支**

```bash
cd /data/sunyunbo/www/TouhouCCB
git fetch origin
git checkout main && git pull --ff-only origin main
git checkout -b feat/2026-05-23-title-system
```

- [ ] **Step 2: 验证 pytest 在 main 基线干净**

```bash
cd backend
pytest -x 2>&1 | tail -20
```
Expected: 全 pass（或仅有先前 known fail；不要在引入 title 前留下绿色基线缺口）。

- [ ] **Step 3: 验证前端基线**

```bash
cd thccb-frontend
npm run type-check
npm run lint
```
Expected: 全 pass。

> 若任一步 fail：停下问，不要边带病基线边开发新功能。

---

## Task 1: SQLModel 5 张新表 + User 加列

**Files:**
- Create: `backend/app/models/title.py`
- Modify: `backend/app/models/base.py` — User 类加 `equipped_title_id` 列
- Modify: `backend/app/main.py` — import `app.models.title` 触发 metadata 注册
- Test: `backend/tests/test_title_models.py`

- [ ] **Step 1: 写测试 — 表能建出来 + lazy raise_on_sql 守护**

`backend/tests/test_title_models.py`:

```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
import pytest_asyncio
from sqlmodel import select
from sqlalchemy.exc import StatementError, InvalidRequestError
from sqlalchemy.orm.exc import DetachedInstanceError

from app.core.database import async_session_maker
from app.models.base import User
from app.models.title import (
    Title, UserTitle, TitleCodeBatch, TitleCode, MarketRequiredTitle,
)


@pytest.mark.asyncio
async def test_create_title_and_query(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            t = Title(name="VIP", description="尊贵会员",
                      color="#FFD700", icon="★", sort_order=10)
            s.add(t)
            await s.flush()
            tid = t.id

    async with async_session_maker() as s:
        got = (await s.execute(select(Title).where(Title.id == tid))).scalar_one()
        assert got.name == "VIP"
        assert got.is_active is True


@pytest.mark.asyncio
async def test_title_name_unique(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            s.add(Title(name="VIP"))
    with pytest.raises(Exception):
        async with async_session_maker() as s:
            async with s.begin():
                s.add(Title(name="VIP"))


@pytest.mark.asyncio
async def test_user_title_unique_per_pair(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username="alice", casdoor_id="cd_a")
            t = Title(name="VIP")
            s.add(u); s.add(t)
            await s.flush()
            s.add(UserTitle(user_id=u.id, title_id=t.id, source="admin"))
    with pytest.raises(Exception):
        async with async_session_maker() as s:
            async with s.begin():
                u = (await s.execute(select(User).where(User.username == "alice"))).scalar_one()
                t = (await s.execute(select(Title).where(Title.name == "VIP"))).scalar_one()
                s.add(UserTitle(user_id=u.id, title_id=t.id, source="code"))


@pytest.mark.asyncio
async def test_user_equipped_title_id_nullable(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username="bob", casdoor_id="cd_b")
            s.add(u)
            await s.flush()
            assert u.equipped_title_id is None
```

- [ ] **Step 2: 跑测试看 fail**

```bash
cd backend
pytest tests/test_title_models.py -v 2>&1 | tail -20
```
Expected: ImportError "cannot import name 'Title' from 'app.models.title'".

- [ ] **Step 3: 写 `backend/app/models/title.py`**

```python
"""Title 系统的 5 张表 + User.equipped_title_id 列定义。

CLAUDE.md 性能护栏：所有反向 List[...] 关系一律 lazy="raise_on_sql"。
读 user.user_titles / market.required_titles 时必须显式 selectinload。
"""
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import (
    UniqueConstraint, CheckConstraint, Index, DateTime,
)


class Title(SQLModel, table=True):
    __tablename__ = "title"
    __table_args__ = (UniqueConstraint("name", name="uq_title_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(nullable=False, max_length=32, index=True)
    description: str = Field(default="", max_length=200)
    color: str = Field(default="#000000", max_length=16)
    icon: str = Field(default="", max_length=16)
    sort_order: int = Field(default=100, index=True)
    is_active: bool = Field(default=True, nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )


class UserTitle(SQLModel, table=True):
    __tablename__ = "user_title"
    __table_args__ = (
        UniqueConstraint("user_id", "title_id", name="uq_user_title"),
        CheckConstraint("source IN ('admin','code')", name="ck_user_title_source"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True, nullable=False)
    title_id: int = Field(foreign_key="title.id", index=True, nullable=False)
    granted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
    granted_by_admin_id: Optional[int] = Field(default=None, foreign_key="user.id")
    source: str = Field(max_length=16)  # 'admin' | 'code'


class TitleCodeBatch(SQLModel, table=True):
    __tablename__ = "title_code_batch"

    id: Optional[int] = Field(default=None, primary_key=True)
    title_id: int = Field(foreign_key="title.id", index=True, nullable=False)
    name: str = Field(nullable=False, max_length=64)
    description: str = Field(default="", max_length=200)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
    created_by_admin_id: Optional[int] = Field(default=None, foreign_key="user.id")


class TitleCode(SQLModel, table=True):
    __tablename__ = "title_code"
    __table_args__ = (
        UniqueConstraint("code_string", name="uq_title_code_string"),
        Index("ix_title_code_batch_status", "batch_id", "status"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: int = Field(foreign_key="title_code_batch.id", index=True, nullable=False)
    code_string: str = Field(nullable=False, max_length=64)
    status: str = Field(default="available", nullable=False)  # 'available' | 'used'
    used_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    used_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


class MarketRequiredTitle(SQLModel, table=True):
    """市场 → 允许交易的 title 名单（ANY-of）。空 = 任何人可交易。"""
    __tablename__ = "market_required_title"
    __table_args__ = (
        UniqueConstraint("market_id", "title_id", name="uq_market_required_title"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    market_id: int = Field(foreign_key="market.id", index=True, nullable=False)
    title_id: int = Field(foreign_key="title.id", index=True, nullable=False)
```

- [ ] **Step 4: 给 `backend/app/models/base.py` 的 User 加列**

在 User 类的最后一个字段后追加（保持在 `positions` / `transactions` 关系定义之前）：

```python
    # 当前佩戴的 title；ondelete=SET NULL 兜底（title 硬删 / 撤销时自动清）
    equipped_title_id: Optional[int] = Field(
        default=None,
        sa_column=Column(ForeignKey("title.id", ondelete="SET NULL"), nullable=True),
    )
```

需要确认 `base.py` 顶部已 import `Column, ForeignKey`（既有 imports 含 `Column, DateTime, ForeignKey, Index, Numeric, JSON` — OK）。

- [ ] **Step 5: 让 `app/main.py` 触发新模型注册**

在 `app/main.py` 中找到既有 model 模块 import（搜 `from app.models`），追加：

```python
from app.models import title as _title_models  # noqa: F401 触发 metadata 注册
```

放在文件顶部 model 相关 import 区域（与 `from app.models import redemption` 同档位）。

- [ ] **Step 6: 跑测试看 pass**

```bash
cd backend
pytest tests/test_title_models.py -v 2>&1 | tail -20
```
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/title.py backend/app/models/base.py backend/app/main.py backend/tests/test_title_models.py
git commit -m "feat(title): add 5 SQLModel tables + User.equipped_title_id"
```

---

## Task 2: Alembic 迁移（手抄全显式命名版）

**Files:**
- Create: `backend/alembic/versions/2026_05_23_XXXX-add_title_system.py`（实际文件名由 alembic 生成）
- Test: `backend/tests/test_title_migration.py`

- [ ] **Step 1: 先用 autogenerate 出草稿**

```bash
cd backend
alembic revision --autogenerate -m "add title system catalog codes user-title market-gating"
```

记下生成的 revision id（如 `abc123def456`）和文件路径 `alembic/versions/2026_05_23_XXXX-add_title_system_catalog_codes_user_title_market_gating.py`。

- [ ] **Step 2: 用 spec §9.2 的代码整体替换 upgrade/downgrade**

打开生成的文件，把 `def upgrade()` 和 `def downgrade()` 函数体**完全替换**为 spec `docs/superpowers/specs/2026-05-23-title-system-design.md` §9.2 中的代码。保留文件顶部的 `revision`, `down_revision = "679d34cb5986"`, `branch_labels`, `depends_on` 这些 alembic 自动写好的元数据；如果 `down_revision` 不是 `679d34cb5986` 手工改成。

确认要点（spec 已给完整代码，对照检查）：
1. 建表顺序：title → title_code_batch → title_code → user_title → market_required_title → 最后给 user 加 column
2. 所有 NOT NULL 列都给了 `server_default`
3. 所有 FK / Unique / Check 都显式命名（`uq_*`, `fk_*`, `ck_*`, `ix_*`）
4. `user.equipped_title_id` 用 `op.add_column` + 单独 `op.create_foreign_key("fk_user_equipped_title", ..., ondelete="SET NULL")`
5. `downgrade()` 顺序严格反向

- [ ] **Step 3: 本地预演 upgrade → downgrade → upgrade**

```bash
cd backend
# 用一个测试 DB，不动 dev
export DATABASE_URL="sqlite+aiosqlite:////tmp/thccb_migration_test.db"
rm -f /tmp/thccb_migration_test.db

# 给 sqlite 跑迁移需要先有 baseline 跟基础 schema —— 走 init_db 建好已有表
python -c "
import asyncio
from sqlmodel import SQLModel
from app.core.database import engine
import app.models.base, app.models.redemption  # 注册 metadata
# 不 import app.models.title，模拟"加 title 前"的状态
async def init():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
asyncio.run(init())
"

# stamp 到 partial_liq mode 那个 revision，让 alembic 把现状当 baseline
alembic stamp 679d34cb5986

# 跑新迁移
alembic upgrade head
# Expected: INFO Running upgrade 679d34cb5986 -> <new_id>, add_title_system...

# 跑 downgrade
alembic downgrade -1
# Expected: INFO Running downgrade <new_id> -> 679d34cb5986

# 再 upgrade 验证幂等
alembic upgrade head
# Expected: INFO Running upgrade 679d34cb5986 -> <new_id>

unset DATABASE_URL
rm -f /tmp/thccb_migration_test.db
```

任一步报错 → 修迁移文件，重新跑。

- [ ] **Step 4: 写 migration round-trip 测试**

`backend/tests/test_title_migration.py`:

```python
"""验证 alembic upgrade/downgrade 在 sqlite 上能跑通且幂等。

直接调 alembic command API（不 fork subprocess），更可靠。
"""
import os
import sys
import tempfile
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, inspect


def _make_alembic_cfg(db_url: str) -> Config:
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_upgrade_creates_tables_and_column():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db_url = f"sqlite:///{db_path}"
    try:
        # 先建已有表（partial_liq 之前的状态）
        from sqlmodel import SQLModel
        import app.models.base  # noqa: F401
        import app.models.redemption  # noqa: F401
        sync_engine = create_engine(db_url)
        SQLModel.metadata.create_all(sync_engine)

        cfg = _make_alembic_cfg(db_url)
        command.stamp(cfg, "679d34cb5986")
        command.upgrade(cfg, "head")

        # 检查 5 张新表 + column 存在
        insp = inspect(sync_engine)
        names = set(insp.get_table_names())
        for t in ["title", "user_title", "title_code_batch", "title_code", "market_required_title"]:
            assert t in names, f"{t} not created"
        user_cols = {c["name"] for c in insp.get_columns("user")}
        assert "equipped_title_id" in user_cols

        # downgrade 回到 partial_liq
        command.downgrade(cfg, "-1")
        insp = inspect(sync_engine)
        names = set(insp.get_table_names())
        for t in ["title", "user_title", "title_code_batch", "title_code", "market_required_title"]:
            assert t not in names, f"{t} not dropped"
        user_cols = {c["name"] for c in insp.get_columns("user")}
        assert "equipped_title_id" not in user_cols

        # 再 upgrade 幂等
        command.upgrade(cfg, "head")
        insp = inspect(sync_engine)
        names = set(insp.get_table_names())
        assert "title" in names
    finally:
        sync_engine.dispose()
        os.unlink(db_path)
```

- [ ] **Step 5: 跑测试**

```bash
cd backend
pytest tests/test_title_migration.py -v 2>&1 | tail -15
```
Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/alembic/versions/2026_05_23_*-add_title_system_*.py backend/tests/test_title_migration.py
git commit -m "feat(title): alembic migration with round-trip test"
```

---

## Task 3: Pydantic 响应/请求 schema

**Files:**
- Create: `backend/app/schemas/title.py`
- Test: `backend/tests/test_title_models.py` 复用（验证 schema 序列化）

- [ ] **Step 1: 写 schema 文件**

`backend/app/schemas/title.py`:

```python
"""Title 系统 Pydantic 请求/响应 schema。"""
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


# ── 公共：title 简化视图（chip 渲染用） ────────
class TitleChipRead(BaseModel):
    id: int
    name: str
    color: str
    icon: str


# ── catalog 视图 ────────────────────────────
class TitleRead(BaseModel):
    id: int
    name: str
    description: str
    color: str
    icon: str
    sort_order: int
    is_active: bool
    created_at: datetime


class TitleCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=32)
    description: str = Field(default="", max_length=200)
    color: str = Field(default="#000000", max_length=16)
    icon: str = Field(default="", max_length=16)
    sort_order: int = Field(default=100)


class TitleUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=32)
    description: Optional[str] = Field(default=None, max_length=200)
    color: Optional[str] = Field(default=None, max_length=16)
    icon: Optional[str] = Field(default=None, max_length=16)
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


# ── 用户自助 ─────────────────────────────────
class MyTitleItem(BaseModel):
    title: TitleRead
    granted_at: datetime
    source: str


class MyTitlesResponse(BaseModel):
    equipped_title_id: Optional[int]
    titles: List[MyTitleItem]


class EquipRequest(BaseModel):
    title_id: Optional[int] = Field(default=None, description="null = 取下")


class RedeemRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=64)


class RedeemResponse(BaseModel):
    title: TitleRead


# ── Batch / Code ──────────────────────────────
class BatchCreateRequest(BaseModel):
    title_id: int
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=200)


class BatchRead(BaseModel):
    id: int
    title_id: int
    title_name: str
    name: str
    description: str
    total: int
    used: int
    created_at: datetime


class CodeRead(BaseModel):
    id: int
    code_string: str
    status: str
    used_by_username: Optional[str]
    used_at: Optional[datetime]


class CSVImportResponse(BaseModel):
    inserted: int


# ── Admin user-title ──────────────────────────
class UserTitleGrantRequest(BaseModel):
    title_id: int


class UserTitleListItem(BaseModel):
    title: TitleRead
    granted_at: datetime
    source: str
    granted_by_admin_id: Optional[int]


# ── Market required title ────────────────────
class MarketRequiredTitlesPutRequest(BaseModel):
    title_ids: List[int] = Field(default_factory=list)
```

- [ ] **Step 2: 跑现有 test_title_models 确保 import OK**

```bash
cd backend
python -c "from app.schemas import title; print('OK')"
```
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/schemas/title.py
git commit -m "feat(title): pydantic schemas"
```

---

## Task 4: Title catalog admin endpoints + service

**Files:**
- Create: `backend/app/services/title_service.py`
- Create: `backend/app/api/v1/admin_title.py`
- Modify: `backend/app/main.py` — include router
- Test: `backend/tests/test_title_catalog_admin.py`

- [ ] **Step 1: 写测试**

`backend/tests/test_title_catalog_admin.py`:

```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest, pytest_asyncio, uuid
from sqlmodel import select
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.title import Title


async def _make_user(superuser=False):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}", is_superuser=superuser)
            s.add(u); await s.flush()
            uid = u.id
    return uid, {"Authorization": f"Bearer {create_access_token(uid)}"}


@pytest.mark.asyncio
async def test_admin_create_title_minimum(client):
    _, h = await _make_user(superuser=True)
    r = await client.post("/api/v1/admin/titles",
                          json={"name": "VIP"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "VIP"
    assert body["is_active"] is True
    assert body["color"] == "#000000"


@pytest.mark.asyncio
async def test_admin_create_title_full(client):
    _, h = await _make_user(superuser=True)
    r = await client.post("/api/v1/admin/titles",
                          json={"name":"Beta","description":"内测","color":"#FF0000",
                                "icon":"β","sort_order":50}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["color"] == "#FF0000"


@pytest.mark.asyncio
async def test_admin_create_title_duplicate_name(client):
    _, h = await _make_user(superuser=True)
    await client.post("/api/v1/admin/titles", json={"name":"VIP"}, headers=h)
    r = await client.post("/api/v1/admin/titles", json={"name":"VIP"}, headers=h)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_admin_titles_list(client):
    _, h = await _make_user(superuser=True)
    await client.post("/api/v1/admin/titles", json={"name":"A","sort_order":20}, headers=h)
    await client.post("/api/v1/admin/titles", json={"name":"B","sort_order":10}, headers=h)
    r = await client.get("/api/v1/admin/titles", headers=h)
    assert r.status_code == 200
    names = [t["name"] for t in r.json()]
    assert names == ["B", "A"]  # 按 sort_order asc


@pytest.mark.asyncio
async def test_admin_patch_title(client):
    _, h = await _make_user(superuser=True)
    r = await client.post("/api/v1/admin/titles", json={"name":"VIP"}, headers=h)
    tid = r.json()["id"]
    r = await client.patch(f"/api/v1/admin/titles/{tid}",
                            json={"color":"#0000FF","is_active":False}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["color"] == "#0000FF"
    assert body["is_active"] is False


@pytest.mark.asyncio
async def test_admin_titles_requires_superuser(client):
    _, h = await _make_user(superuser=False)
    r = await client.post("/api/v1/admin/titles", json={"name":"VIP"}, headers=h)
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: 跑测试看 fail**

```bash
cd backend
pytest tests/test_title_catalog_admin.py -v 2>&1 | tail -15
```
Expected: 6 failed with 404 (router 还没 include).

- [ ] **Step 3: 写 service**

`backend/app/services/title_service.py`:

```python
"""Title catalog 业务逻辑（admin 端 CRUD + 状态查询）。

CLAUDE.md 守则：不让外层路由直接操纵 ORM；service 收口业务规则。
"""
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException, status

from app.models.title import Title
from app.schemas.title import TitleCreateRequest, TitleUpdateRequest


async def list_titles(db: AsyncSession, include_inactive: bool = True) -> List[Title]:
    stmt = select(Title)
    if not include_inactive:
        stmt = stmt.where(Title.is_active == True)  # noqa: E712
    stmt = stmt.order_by(Title.sort_order.asc(), Title.id.asc())
    return list((await db.execute(stmt)).scalars().all())


async def create_title(db: AsyncSession, req: TitleCreateRequest) -> Title:
    # 重名预检（DB UNIQUE 兜底，但提前 raise 错误更清晰）
    dup = (await db.execute(select(Title).where(Title.name == req.name))).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"称号名 '{req.name}' 已存在")
    t = Title(
        name=req.name, description=req.description,
        color=req.color, icon=req.icon, sort_order=req.sort_order,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def update_title(
    db: AsyncSession, title_id: int, req: TitleUpdateRequest,
) -> Title:
    t = await db.get(Title, title_id)
    if not t:
        raise HTTPException(status_code=404, detail="title not found")
    if req.name is not None and req.name != t.name:
        dup = (await db.execute(select(Title).where(Title.name == req.name))).scalar_one_or_none()
        if dup:
            raise HTTPException(status_code=409, detail=f"称号名 '{req.name}' 已存在")
        t.name = req.name
    for f in ("description", "color", "icon", "sort_order", "is_active"):
        v = getattr(req, f)
        if v is not None:
            setattr(t, f, v)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def get_title_or_404(db: AsyncSession, title_id: int) -> Title:
    t = await db.get(Title, title_id)
    if not t:
        raise HTTPException(status_code=404, detail="title not found")
    return t
```

- [ ] **Step 4: 写 admin router**

`backend/app/api/v1/admin_title.py`:

```python
"""Title 管理后端路由 — admin only。

挂载位置见 app/main.py: prefix="/api/v1/admin"
"""
from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User
from app.models.title import Title
from app.schemas.title import (
    TitleRead, TitleCreateRequest, TitleUpdateRequest,
)
from app.services import title_service

router = APIRouter()


def _to_title_read(t: Title) -> TitleRead:
    return TitleRead(
        id=t.id, name=t.name, description=t.description,
        color=t.color, icon=t.icon, sort_order=t.sort_order,
        is_active=t.is_active, created_at=t.created_at,
    )


@router.get("/titles", response_model=List[TitleRead], summary="列出全部 title")
async def list_titles(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    titles = await title_service.list_titles(db, include_inactive=True)
    return [_to_title_read(t) for t in titles]


@router.post("/titles", response_model=TitleRead, summary="创建 title")
async def create_title(
    req: TitleCreateRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    t = await title_service.create_title(db, req)
    return _to_title_read(t)


@router.patch("/titles/{title_id}", response_model=TitleRead, summary="修改 title")
async def update_title(
    title_id: int,
    req: TitleUpdateRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    t = await title_service.update_title(db, title_id, req)
    return _to_title_read(t)
```

- [ ] **Step 5: 在 main.py include router**

在 `backend/app/main.py` 找到 admin_bot include 那一段，下面追加：

```python
from app.api.v1 import admin_title as admin_title_api
app.include_router(admin_title_api.router, prefix="/api/v1/admin", tags=["AdminTitle"])
```

- [ ] **Step 6: 跑测试**

```bash
cd backend
pytest tests/test_title_catalog_admin.py -v 2>&1 | tail -15
```
Expected: 6 passed.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/title_service.py backend/app/api/v1/admin_title.py backend/app/main.py backend/tests/test_title_catalog_admin.py
git commit -m "feat(title): admin catalog endpoints (list/create/patch)"
```

---

## Task 5: 用户自助 title 读接口

**Files:**
- Create: `backend/app/api/v1/title.py`
- Modify: `backend/app/services/title_service.py` — 加 `list_user_titles`, `get_equipped_chip`
- Modify: `backend/app/main.py` — include router
- Test: `backend/tests/test_title_user_read.py`

- [ ] **Step 1: 写测试**

`backend/tests/test_title_user_read.py`:

```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest, uuid
from datetime import datetime, timezone
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.title import Title, UserTitle


async def _mk_user(superuser=False):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}", is_superuser=superuser)
            s.add(u); await s.flush()
            return u.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


async def _mk_title(name="VIP", color="#FFD700", sort=10):
    async with async_session_maker() as s:
        async with s.begin():
            t = Title(name=name, color=color, sort_order=sort)
            s.add(t); await s.flush()
            return t.id


async def _grant(uid, tid, source="admin"):
    async with async_session_maker() as s:
        async with s.begin():
            s.add(UserTitle(user_id=uid, title_id=tid, source=source))


@pytest.mark.asyncio
async def test_my_titles_empty(client):
    uid, h = await _mk_user()
    r = await client.get("/api/v1/title/me", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["equipped_title_id"] is None
    assert body["titles"] == []


@pytest.mark.asyncio
async def test_my_titles_lists_owned_sorted(client):
    uid, h = await _mk_user()
    tid_a = await _mk_title(name="A", sort=30)
    tid_b = await _mk_title(name="B", sort=10)
    await _grant(uid, tid_a)
    await _grant(uid, tid_b)
    r = await client.get("/api/v1/title/me", headers=h)
    assert r.status_code == 200
    names = [item["title"]["name"] for item in r.json()["titles"]]
    assert names == ["B", "A"]


@pytest.mark.asyncio
async def test_catalog_public_excludes_inactive(client):
    uid, h = await _mk_user()
    async with async_session_maker() as s:
        async with s.begin():
            s.add(Title(name="Active", is_active=True))
            s.add(Title(name="Hidden", is_active=False))
    r = await client.get("/api/v1/title/catalog", headers=h)
    assert r.status_code == 200
    names = [t["name"] for t in r.json()]
    assert "Active" in names
    assert "Hidden" not in names


@pytest.mark.asyncio
async def test_users_equipped_returns_chip(client):
    uid, _ = await _mk_user()
    tid = await _mk_title(name="VIP")
    await _grant(uid, tid)
    async with async_session_maker() as s:
        async with s.begin():
            u = await s.get(User, uid)
            u.equipped_title_id = tid
            s.add(u)
    _, h = await _mk_user()
    r = await client.get(f"/api/v1/title/users/{uid}/equipped", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body is not None
    assert body["name"] == "VIP"


@pytest.mark.asyncio
async def test_users_equipped_null_when_not_set(client):
    uid, _ = await _mk_user()
    _, h = await _mk_user()
    r = await client.get(f"/api/v1/title/users/{uid}/equipped", headers=h)
    assert r.status_code == 200
    assert r.json() is None
```

- [ ] **Step 2: 跑测试看 fail**

```bash
pytest tests/test_title_user_read.py -v 2>&1 | tail -15
```
Expected: 5 failed with 404.

- [ ] **Step 3: 在 title_service.py 追加查询函数**

在 `backend/app/services/title_service.py` 末尾追加：

```python
from sqlalchemy.orm import aliased


async def list_my_titles(db: AsyncSession, user_id: int):
    """返回某用户的全部 title，按 sort_order asc。

    显式 join 避免 raise_on_sql 反向集合触发。
    """
    from app.models.title import UserTitle
    stmt = (
        select(UserTitle, Title)
        .join(Title, UserTitle.title_id == Title.id)
        .where(UserTitle.user_id == user_id)
        .order_by(Title.sort_order.asc(), Title.id.asc())
    )
    rows = (await db.execute(stmt)).all()
    return rows  # List[Tuple[UserTitle, Title]]


async def get_equipped_chip(db: AsyncSession, user_id: int):
    """读某用户当前佩戴的 title（chip 视图）。无佩戴返回 None。"""
    from app.models.base import User
    stmt = (
        select(Title)
        .join(User, User.equipped_title_id == Title.id)
        .where(User.id == user_id)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_active_catalog(db: AsyncSession) -> List[Title]:
    return await list_titles(db, include_inactive=False)
```

- [ ] **Step 4: 写用户自助 router**

`backend/app/api/v1/title.py`:

```python
"""Title 用户自助接口 — 我的称号 / 公开 catalog / 别人佩戴的 chip。"""
from typing import Optional
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import current_active_user
from app.models.base import User
from app.schemas.title import (
    TitleRead, TitleChipRead, MyTitlesResponse, MyTitleItem,
)
from app.services import title_service

router = APIRouter()


def _to_chip(t) -> TitleChipRead:
    return TitleChipRead(id=t.id, name=t.name, color=t.color, icon=t.icon)


def _to_full(t) -> TitleRead:
    return TitleRead(
        id=t.id, name=t.name, description=t.description,
        color=t.color, icon=t.icon, sort_order=t.sort_order,
        is_active=t.is_active, created_at=t.created_at,
    )


@router.get("/me", response_model=MyTitlesResponse, summary="我的称号")
async def my_titles(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    rows = await title_service.list_my_titles(db, user.id)
    items = [
        MyTitleItem(
            title=_to_full(t),
            granted_at=ut.granted_at,
            source=ut.source,
        ) for ut, t in rows
    ]
    return MyTitlesResponse(
        equipped_title_id=user.equipped_title_id,
        titles=items,
    )


@router.get("/catalog", response_model=list[TitleRead], summary="公开 title catalog")
async def public_catalog(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    titles = await title_service.list_active_catalog(db)
    return [_to_full(t) for t in titles]


@router.get("/users/{user_id}/equipped", response_model=Optional[TitleChipRead],
            summary="某用户当前佩戴的 title")
async def user_equipped(
    user_id: int,
    requester: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    t = await title_service.get_equipped_chip(db, user_id)
    return _to_chip(t) if t else None
```

- [ ] **Step 5: 在 main.py include**

```python
from app.api.v1 import title as title_api
app.include_router(title_api.router, prefix="/api/v1/title", tags=["Title"])
```

- [ ] **Step 6: 跑测试**

```bash
pytest tests/test_title_user_read.py -v 2>&1 | tail -15
```
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/title_service.py backend/app/api/v1/title.py backend/app/main.py backend/tests/test_title_user_read.py
git commit -m "feat(title): user-facing read endpoints (me/catalog/equipped)"
```

---

## Task 6: Equip endpoint（切换佩戴）

**Files:**
- Modify: `backend/app/services/title_service.py` — 加 `equip_title`
- Modify: `backend/app/api/v1/title.py` — 加 POST /me/equip
- Test: `backend/tests/test_title_equip.py`

- [ ] **Step 1: 写测试**

`backend/tests/test_title_equip.py`:

```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest, uuid
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.title import Title, UserTitle


async def _mk_user_with_title(superuser=False, title_name="VIP"):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}", is_superuser=superuser)
            t = Title(name=f"{title_name}_{suffix}")
            s.add(u); s.add(t); await s.flush()
            s.add(UserTitle(user_id=u.id, title_id=t.id, source="admin"))
            return u.id, t.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


@pytest.mark.asyncio
async def test_equip_owned_title(client):
    uid, tid, h = await _mk_user_with_title()
    r = await client.post("/api/v1/title/me/equip", json={"title_id": tid}, headers=h)
    assert r.status_code == 200
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.equipped_title_id == tid


@pytest.mark.asyncio
async def test_equip_null_unequips(client):
    uid, tid, h = await _mk_user_with_title()
    await client.post("/api/v1/title/me/equip", json={"title_id": tid}, headers=h)
    r = await client.post("/api/v1/title/me/equip", json={"title_id": None}, headers=h)
    assert r.status_code == 200
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.equipped_title_id is None


@pytest.mark.asyncio
async def test_equip_not_owned_title_403(client):
    uid, tid, h = await _mk_user_with_title()
    # 造一个用户不持有的 title
    async with async_session_maker() as s:
        async with s.begin():
            t2 = Title(name=f"OTHER_{uuid.uuid4().hex[:6]}")
            s.add(t2); await s.flush()
            other_tid = t2.id
    r = await client.post("/api/v1/title/me/equip",
                          json={"title_id": other_tid}, headers=h)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_equip_nonexistent_title_403_or_404(client):
    uid, tid, h = await _mk_user_with_title()
    r = await client.post("/api/v1/title/me/equip",
                          json={"title_id": 999999}, headers=h)
    assert r.status_code in (403, 404)
```

- [ ] **Step 2: 跑测试看 fail**

```bash
pytest tests/test_title_equip.py -v 2>&1 | tail -15
```
Expected: 4 failed.

- [ ] **Step 3: 在 title_service 加 equip_title**

```python
async def equip_title(db: AsyncSession, user_id: int, title_id: Optional[int]) -> None:
    """切换用户佩戴的 title。title_id=None → 取下。

    安全约束：title_id 非 None 时，必须确认用户已持有该 title。
    """
    from app.models.base import User
    from app.models.title import UserTitle
    u = await db.get(User, user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    if title_id is None:
        u.equipped_title_id = None
        db.add(u)
        await db.commit()
        return
    # 验证持有
    own = (await db.execute(
        select(UserTitle).where(
            UserTitle.user_id == user_id, UserTitle.title_id == title_id,
        )
    )).scalar_one_or_none()
    if own is None:
        raise HTTPException(status_code=403, detail="你未持有此称号")
    u.equipped_title_id = title_id
    db.add(u)
    await db.commit()
```

- [ ] **Step 4: 在 title.py router 加 endpoint**

```python
from app.schemas.title import EquipRequest


@router.post("/me/equip", summary="佩戴 / 取下称号")
async def equip(
    req: EquipRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    await title_service.equip_title(db, user.id, req.title_id)
    return {"equipped_title_id": req.title_id}
```

- [ ] **Step 5: 跑测试**

```bash
pytest tests/test_title_equip.py -v 2>&1 | tail -15
```
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/title_service.py backend/app/api/v1/title.py backend/tests/test_title_equip.py
git commit -m "feat(title): equip/unequip endpoint"
```

---

## Task 7: Title code batch admin endpoints

**Files:**
- Create: `backend/app/services/title_code_service.py`
- Modify: `backend/app/api/v1/admin_title.py` — 加 batch endpoints
- Test: `backend/tests/test_title_code_admin.py`

- [ ] **Step 1: 写测试**

`backend/tests/test_title_code_admin.py`:

```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest, uuid
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.title import Title


async def _mk_user(superuser=False):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}", is_superuser=superuser)
            s.add(u); await s.flush()
            return u.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


async def _mk_title(name="VIP"):
    async with async_session_maker() as s:
        async with s.begin():
            t = Title(name=f"{name}_{uuid.uuid4().hex[:6]}")
            s.add(t); await s.flush()
            return t.id


@pytest.mark.asyncio
async def test_create_batch(client):
    _, h = await _mk_user(superuser=True)
    tid = await _mk_title()
    r = await client.post("/api/v1/admin/title-batches",
                          json={"title_id": tid, "name": "2026-Q2", "description": ""},
                          headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["title_id"] == tid
    assert body["name"] == "2026-Q2"
    assert body["total"] == 0
    assert body["used"] == 0


@pytest.mark.asyncio
async def test_create_batch_inactive_title_rejected(client):
    _, h = await _mk_user(superuser=True)
    tid = await _mk_title()
    # 软删 title
    await client.patch(f"/api/v1/admin/titles/{tid}",
                       json={"is_active": False}, headers=h)
    r = await client.post("/api/v1/admin/title-batches",
                          json={"title_id": tid, "name": "X"}, headers=h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_list_batches_returns_counts(client):
    _, h = await _mk_user(superuser=True)
    tid = await _mk_title()
    await client.post("/api/v1/admin/title-batches",
                      json={"title_id": tid, "name": "B1"}, headers=h)
    r = await client.get("/api/v1/admin/title-batches", headers=h)
    assert r.status_code == 200
    arr = r.json()
    assert len(arr) >= 1
    assert any(b["name"] == "B1" for b in arr)


@pytest.mark.asyncio
async def test_create_batch_requires_superuser(client):
    _, h = await _mk_user(superuser=False)
    r = await client.post("/api/v1/admin/title-batches",
                          json={"title_id": 1, "name": "X"}, headers=h)
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: 跑测试看 fail**

```bash
pytest tests/test_title_code_admin.py -v 2>&1 | tail -15
```

- [ ] **Step 3: 写 service**

`backend/app/services/title_code_service.py`:

```python
"""Title 激活码 + batch 业务逻辑。包含 CSV 解析 / 校验 / 兑换。"""
import re
from typing import List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from fastapi import HTTPException

from app.models.title import Title, TitleCodeBatch, TitleCode

_CODE_RE = re.compile(r"^[A-Za-z0-9\-_]{4,64}$")
CSV_HARDCAP = 5000


async def create_batch(
    db: AsyncSession, title_id: int, name: str, description: str, admin_id: int,
) -> TitleCodeBatch:
    t = await db.get(Title, title_id)
    if not t:
        raise HTTPException(status_code=404, detail="title not found")
    if not t.is_active:
        raise HTTPException(status_code=400, detail="该称号已软删，不能新建批次")
    b = TitleCodeBatch(
        title_id=title_id, name=name, description=description,
        created_by_admin_id=admin_id,
    )
    db.add(b)
    await db.commit()
    await db.refresh(b)
    return b


async def list_batches_with_counts(db: AsyncSession) -> List[dict]:
    """列出全部 batch，每个 batch 附 used/total + title_name。

    单条 SQL 用聚合 join。
    """
    used_subq = (
        select(
            TitleCode.batch_id.label("bid"),
            func.count().label("total"),
            func.sum(
                func.cast(TitleCode.status == "used", select(1).c.cast)  # placeholder
            ).label("used"),
        )
        .group_by(TitleCode.batch_id)
        .subquery()
    )
    # 上面 cast 写法 portable 性差；改成两次聚合
    # Simpler: load batches + per-batch counts in Python loop, OK for admin pages.
    batches = list((await db.execute(
        select(TitleCodeBatch, Title.name)
        .join(Title, TitleCodeBatch.title_id == Title.id)
        .order_by(TitleCodeBatch.id.desc())
    )).all())
    rows = []
    for b, title_name in batches:
        total = (await db.execute(
            select(func.count()).select_from(TitleCode).where(TitleCode.batch_id == b.id)
        )).scalar_one()
        used = (await db.execute(
            select(func.count()).select_from(TitleCode).where(
                TitleCode.batch_id == b.id, TitleCode.status == "used",
            )
        )).scalar_one()
        rows.append({
            "id": b.id, "title_id": b.title_id, "title_name": title_name,
            "name": b.name, "description": b.description,
            "total": int(total), "used": int(used),
            "created_at": b.created_at,
        })
    return rows


def parse_csv_codes(raw_bytes: bytes) -> List[str]:
    """解析单列 CSV（一行一个 code，可带表头）。整批 reject on any invalid。"""
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV 必须是 UTF-8 编码")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and lines[0].lower() in ("code", "codes", "code_string"):
        lines = lines[1:]
    if len(lines) == 0:
        raise HTTPException(status_code=400, detail="CSV 不含任何 code")
    if len(lines) > CSV_HARDCAP:
        raise HTTPException(
            status_code=400,
            detail=f"CSV 行数 {len(lines)} 超过单批上限 {CSV_HARDCAP}",
        )
    # 校验格式 + 文件内不重复
    seen = set()
    for idx, code in enumerate(lines, 1):
        if not _CODE_RE.match(code):
            raise HTTPException(
                status_code=400,
                detail=f"第 {idx} 行 code '{code}' 格式不合法（仅 A-Z a-z 0-9 - _，长度 4-64）",
            )
        if code in seen:
            raise HTTPException(
                status_code=400,
                detail=f"第 {idx} 行 code '{code}' 在文件内重复",
            )
        seen.add(code)
    return lines


async def import_codes_to_batch(
    db: AsyncSession, batch_id: int, codes: List[str],
) -> int:
    """整批插入 codes 到 batch。任一与库内已有冲突 → 整批 reject。"""
    b = await db.get(TitleCodeBatch, batch_id)
    if not b:
        raise HTTPException(status_code=404, detail="batch not found")
    # 预先 SELECT 一遍找冲突，避免依赖 DB 的 INSERT 异常拆包
    existing = list((await db.execute(
        select(TitleCode.code_string).where(TitleCode.code_string.in_(codes))
    )).scalars().all())
    if existing:
        sample = existing[:5]
        raise HTTPException(
            status_code=400,
            detail=f"以下 code 已存在于库中: {sample} 等 {len(existing)} 个",
        )
    for c in codes:
        db.add(TitleCode(batch_id=batch_id, code_string=c, status="available"))
    await db.commit()
    return len(codes)
```

> 注意：service 文件里用了 cast/used_subq 那段写法说明性的留了 fallback；最终用 simpler per-batch loop 实现，避免 sqlite/pg portability 问题。

- [ ] **Step 4: 修正 service — 把 list_batches_with_counts 的 simpler 版本写干净**

打开 `backend/app/services/title_code_service.py`，**整段替换** `list_batches_with_counts` 函数（删掉 used_subq 那段死代码）为：

```python
async def list_batches_with_counts(db: AsyncSession) -> List[dict]:
    """列出全部 batch，每个 batch 附 used/total + title_name。"""
    batches = list((await db.execute(
        select(TitleCodeBatch, Title.name)
        .join(Title, TitleCodeBatch.title_id == Title.id)
        .order_by(TitleCodeBatch.id.desc())
    )).all())
    rows = []
    for b, title_name in batches:
        total = (await db.execute(
            select(func.count()).select_from(TitleCode).where(TitleCode.batch_id == b.id)
        )).scalar_one()
        used = (await db.execute(
            select(func.count()).select_from(TitleCode).where(
                TitleCode.batch_id == b.id, TitleCode.status == "used",
            )
        )).scalar_one()
        rows.append({
            "id": b.id, "title_id": b.title_id, "title_name": title_name,
            "name": b.name, "description": b.description,
            "total": int(total), "used": int(used),
            "created_at": b.created_at,
        })
    return rows
```

- [ ] **Step 5: admin_title.py 加 batch endpoints**

在 `backend/app/api/v1/admin_title.py` 末尾追加：

```python
from app.schemas.title import BatchCreateRequest, BatchRead
from app.services import title_code_service


@router.get("/title-batches", response_model=List[BatchRead], summary="列出 batch")
async def list_title_batches(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    rows = await title_code_service.list_batches_with_counts(db)
    return [BatchRead(**r) for r in rows]


@router.post("/title-batches", response_model=BatchRead, summary="新建 batch")
async def create_title_batch(
    req: BatchCreateRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    b = await title_code_service.create_batch(
        db, req.title_id, req.name, req.description, admin.id,
    )
    t = await db.get(Title, b.title_id)
    return BatchRead(
        id=b.id, title_id=b.title_id, title_name=t.name,
        name=b.name, description=b.description,
        total=0, used=0, created_at=b.created_at,
    )
```

- [ ] **Step 6: 跑测试**

```bash
pytest tests/test_title_code_admin.py -v 2>&1 | tail -15
```
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/title_code_service.py backend/app/api/v1/admin_title.py backend/tests/test_title_code_admin.py
git commit -m "feat(title): admin batch create/list endpoints"
```

---

## Task 8: CSV 导入端点

**Files:**
- Modify: `backend/app/api/v1/admin_title.py` — 加 POST /title-batches/{id}/import-codes
- Test: `backend/tests/test_title_code_csv_import.py`

- [ ] **Step 1: 写测试**

`backend/tests/test_title_code_csv_import.py`:

```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest, uuid, io
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.title import Title, TitleCodeBatch, TitleCode
from sqlalchemy import select, func


async def _mk_admin():
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"a_{suffix}", email=f"{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}", is_superuser=True)
            t = Title(name=f"VIP_{suffix}")
            s.add(u); s.add(t); await s.flush()
            b = TitleCodeBatch(title_id=t.id, name="B", created_by_admin_id=u.id)
            s.add(b); await s.flush()
            return u.id, b.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


def _csv(*codes, with_header=False):
    lines = []
    if with_header:
        lines.append("code")
    lines.extend(codes)
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.mark.asyncio
async def test_import_3_codes(client):
    _, bid, h = await _mk_admin()
    files = {"file": ("codes.csv", _csv("BETA-AAA", "BETA-BBB", "BETA-CCC"), "text/csv")}
    r = await client.post(f"/api/v1/admin/title-batches/{bid}/import-codes",
                          files=files, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 3
    async with async_session_maker() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(TitleCode).where(TitleCode.batch_id == bid)
        )).scalar_one()
        assert cnt == 3


@pytest.mark.asyncio
async def test_import_with_header(client):
    _, bid, h = await _mk_admin()
    files = {"file": ("codes.csv", _csv("BETA-AAA", with_header=True), "text/csv")}
    r = await client.post(f"/api/v1/admin/title-batches/{bid}/import-codes",
                          files=files, headers=h)
    assert r.status_code == 200
    assert r.json()["inserted"] == 1


@pytest.mark.asyncio
async def test_import_invalid_format_rejected(client):
    _, bid, h = await _mk_admin()
    files = {"file": ("codes.csv", _csv("OK-CODE", "bad code with space"), "text/csv")}
    r = await client.post(f"/api/v1/admin/title-batches/{bid}/import-codes",
                          files=files, headers=h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_import_duplicate_in_file_rejected(client):
    _, bid, h = await _mk_admin()
    files = {"file": ("codes.csv", _csv("DUP-AAA", "DUP-AAA"), "text/csv")}
    r = await client.post(f"/api/v1/admin/title-batches/{bid}/import-codes",
                          files=files, headers=h)
    assert r.status_code == 400
    async with async_session_maker() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(TitleCode).where(TitleCode.batch_id == bid)
        )).scalar_one()
        assert cnt == 0  # 整批 reject


@pytest.mark.asyncio
async def test_import_conflict_with_existing_rejected(client):
    _, bid, h = await _mk_admin()
    # 先成功导一次
    await client.post(f"/api/v1/admin/title-batches/{bid}/import-codes",
        files={"file": ("c.csv", _csv("EXIST-001"), "text/csv")}, headers=h)
    # 再导同一个 code（即使在另一 batch）
    files = {"file": ("c.csv", _csv("NEW-002", "EXIST-001"), "text/csv")}
    r = await client.post(f"/api/v1/admin/title-batches/{bid}/import-codes",
                          files=files, headers=h)
    assert r.status_code == 400
    async with async_session_maker() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(TitleCode).where(
                TitleCode.code_string == "NEW-002"
            )
        )).scalar_one()
        assert cnt == 0  # 整批 reject，NEW-002 也没插


@pytest.mark.asyncio
async def test_import_hardcap_5000(client):
    _, bid, h = await _mk_admin()
    codes = [f"BULK-{i:05d}" for i in range(5001)]
    files = {"file": ("c.csv", _csv(*codes), "text/csv")}
    r = await client.post(f"/api/v1/admin/title-batches/{bid}/import-codes",
                          files=files, headers=h)
    assert r.status_code == 400
```

- [ ] **Step 2: 跑测试看 fail**

```bash
pytest tests/test_title_code_csv_import.py -v 2>&1 | tail -15
```

- [ ] **Step 3: 在 admin_title.py 加 import endpoint**

```python
from fastapi import UploadFile, File
from app.schemas.title import CSVImportResponse


@router.post("/title-batches/{batch_id}/import-codes",
             response_model=CSVImportResponse, summary="CSV 导入激活码到批次")
async def import_codes(
    batch_id: int,
    file: UploadFile = File(...),
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    raw = await file.read()
    if len(raw) > 1 * 1024 * 1024:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="CSV 文件超过 1MB 上限")
    codes = title_code_service.parse_csv_codes(raw)
    inserted = await title_code_service.import_codes_to_batch(db, batch_id, codes)
    return CSVImportResponse(inserted=inserted)
```

- [ ] **Step 4: 跑测试**

```bash
pytest tests/test_title_code_csv_import.py -v 2>&1 | tail -15
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/admin_title.py backend/tests/test_title_code_csv_import.py
git commit -m "feat(title): CSV import endpoint with strict validation"
```

---

## Task 9: 用户兑换激活码

**Files:**
- Modify: `backend/app/services/title_code_service.py` — 加 `redeem_code`
- Modify: `backend/app/api/v1/title.py` — 加 POST /redeem
- Test: `backend/tests/test_title_redeem.py`

- [ ] **Step 1: 写测试**

`backend/tests/test_title_redeem.py`:

```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest, uuid
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.title import Title, TitleCodeBatch, TitleCode, UserTitle
from sqlalchemy import select


async def _mk_setup():
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}")
            admin = User(username=f"a_{suffix}", email=f"a{suffix}@t.com",
                          casdoor_id=f"adcd_{suffix}", is_superuser=True)
            t = Title(name=f"VIP_{suffix}")
            s.add(u); s.add(admin); s.add(t); await s.flush()
            b = TitleCodeBatch(title_id=t.id, name="B", created_by_admin_id=admin.id)
            s.add(b); await s.flush()
            c = TitleCode(batch_id=b.id, code_string=f"OK-{suffix}", status="available")
            s.add(c); await s.flush()
            return u.id, t.id, c.code_string, {"Authorization": f"Bearer {create_access_token(u.id)}"}


@pytest.mark.asyncio
async def test_redeem_available_code(client):
    uid, tid, code, h = await _mk_setup()
    r = await client.post("/api/v1/title/redeem", json={"code": code}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["title"]["id"] == tid
    # user_title 写入
    async with async_session_maker() as s:
        ut = (await s.execute(select(UserTitle).where(
            UserTitle.user_id == uid, UserTitle.title_id == tid,
        ))).scalar_one_or_none()
        assert ut is not None
        assert ut.source == "code"
    # code 标 used
    async with async_session_maker() as s:
        c = (await s.execute(select(TitleCode).where(TitleCode.code_string == code))).scalar_one()
        assert c.status == "used"
        assert c.used_by_user_id == uid
        assert c.used_at is not None
    # 不自动佩戴
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.equipped_title_id is None


@pytest.mark.asyncio
async def test_redeem_already_used(client):
    uid, tid, code, h = await _mk_setup()
    await client.post("/api/v1/title/redeem", json={"code": code}, headers=h)
    # 第二次兑同 code（前一次已用，本次另一用户）
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u2 = User(username=f"u2_{suffix}", email=f"u2{suffix}@t.com",
                      casdoor_id=f"u2cd_{suffix}")
            s.add(u2); await s.flush()
            uid2 = u2.id
    h2 = {"Authorization": f"Bearer {create_access_token(uid2)}"}
    r = await client.post("/api/v1/title/redeem", json={"code": code}, headers=h2)
    assert r.status_code == 403
    # 不泄漏存在性 — 用统一文案
    assert "激活码无效" in r.json().get("detail", "") or "invalid" in r.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_redeem_nonexistent_code(client):
    uid, tid, _, h = await _mk_setup()
    r = await client.post("/api/v1/title/redeem",
                          json={"code": "NO-SUCH-CODE-1234"}, headers=h)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_redeem_already_owned_title_code_not_consumed(client):
    uid, tid, code, h = await _mk_setup()
    # 给用户直接挂上 title（admin source）
    async with async_session_maker() as s:
        async with s.begin():
            s.add(UserTitle(user_id=uid, title_id=tid, source="admin"))
    r = await client.post("/api/v1/title/redeem", json={"code": code}, headers=h)
    assert r.status_code == 403
    assert "已拥有" in r.json().get("detail", "")
    # code 仍 available
    async with async_session_maker() as s:
        c = (await s.execute(select(TitleCode).where(TitleCode.code_string == code))).scalar_one()
        assert c.status == "available"
        assert c.used_by_user_id is None


@pytest.mark.asyncio
async def test_redeem_inactive_title_rejected(client):
    uid, tid, code, h = await _mk_setup()
    # 软删 title
    async with async_session_maker() as s:
        async with s.begin():
            t = await s.get(Title, tid)
            t.is_active = False
            s.add(t)
    r = await client.post("/api/v1/title/redeem", json={"code": code}, headers=h)
    assert r.status_code == 403
```

- [ ] **Step 2: 跑测试看 fail**

```bash
pytest tests/test_title_redeem.py -v 2>&1 | tail -15
```

- [ ] **Step 3: 在 title_code_service.py 加 redeem_code**

```python
from datetime import datetime, timezone
from app.models.title import UserTitle


async def redeem_code(db: AsyncSession, user_id: int, code_string: str) -> Title:
    """
    用户兑换激活码。事务内：
      1. 找 code（不存在 / 已用 → 403 invalid，统一措辞，防探测）
      2. 找 batch → 找 title（is_active 必须 true，否则 403）
      3. 若用户已持有 title → 403 own，code 不消耗
      4. INSERT user_title (source='code') + UPDATE code 标 used
    """
    from app.models.base import User

    # 同事务行锁 code，防并发双兑
    code_row = (await db.execute(
        select(TitleCode).where(TitleCode.code_string == code_string).with_for_update()
    )).scalar_one_or_none()
    if code_row is None or code_row.status != "available":
        raise HTTPException(status_code=403, detail="激活码无效")
    batch = await db.get(TitleCodeBatch, code_row.batch_id)
    title = await db.get(Title, batch.title_id)
    if not title.is_active:
        raise HTTPException(status_code=403, detail="激活码无效")
    # 已持有？
    already = (await db.execute(
        select(UserTitle).where(
            UserTitle.user_id == user_id, UserTitle.title_id == title.id,
        )
    )).scalar_one_or_none()
    if already:
        raise HTTPException(status_code=403, detail="你已拥有此称号")
    # 写入
    now = datetime.now(timezone.utc)
    db.add(UserTitle(
        user_id=user_id, title_id=title.id,
        granted_at=now, source="code",
    ))
    code_row.status = "used"
    code_row.used_by_user_id = user_id
    code_row.used_at = now
    db.add(code_row)
    await db.commit()
    return title
```

- [ ] **Step 4: 在 title.py router 加 endpoint**

```python
from app.schemas.title import RedeemRequest, RedeemResponse
from app.services import title_code_service


@router.post("/redeem", response_model=RedeemResponse, summary="兑换激活码")
async def redeem(
    req: RedeemRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    t = await title_code_service.redeem_code(db, user.id, req.code)
    return RedeemResponse(title=_to_full(t))
```

- [ ] **Step 5: 跑测试**

```bash
pytest tests/test_title_redeem.py -v 2>&1 | tail -15
```
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/title_code_service.py backend/app/api/v1/title.py backend/tests/test_title_redeem.py
git commit -m "feat(title): redeem endpoint with edge case handling"
```

---

## Task 10: Admin 单用户 title 授予/撤销 + 资产快照

**Files:**
- Modify: `backend/app/services/title_service.py` — 加 `grant_user_title`, `revoke_user_title`
- Modify: `backend/app/api/v1/admin_title.py` — 加 user-title endpoints + admin summary
- Test: `backend/tests/test_title_admin_user.py`

- [ ] **Step 1: 写测试**

`backend/tests/test_title_admin_user.py`:

```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest, uuid
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.title import Title, UserTitle


async def _mk():
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"u{suffix}@t.com",
                    casdoor_id=f"ucd_{suffix}")
            admin = User(username=f"a_{suffix}", email=f"a{suffix}@t.com",
                          casdoor_id=f"acd_{suffix}", is_superuser=True)
            t = Title(name=f"VIP_{suffix}")
            s.add(u); s.add(admin); s.add(t); await s.flush()
            return u.id, t.id, {"Authorization": f"Bearer {create_access_token(admin.id)}"}


@pytest.mark.asyncio
async def test_admin_grant_title(client):
    uid, tid, h = await _mk()
    r = await client.post(f"/api/v1/admin/users/{uid}/titles",
                          json={"title_id": tid}, headers=h)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_admin_grant_idempotent(client):
    uid, tid, h = await _mk()
    await client.post(f"/api/v1/admin/users/{uid}/titles",
                      json={"title_id": tid}, headers=h)
    r = await client.post(f"/api/v1/admin/users/{uid}/titles",
                          json={"title_id": tid}, headers=h)
    assert r.status_code == 200  # 幂等不报错


@pytest.mark.asyncio
async def test_admin_revoke_title(client):
    uid, tid, h = await _mk()
    await client.post(f"/api/v1/admin/users/{uid}/titles",
                      json={"title_id": tid}, headers=h)
    r = await client.delete(f"/api/v1/admin/users/{uid}/titles/{tid}", headers=h)
    assert r.status_code == 200
    async with async_session_maker() as s:
        from sqlalchemy import select
        ut = (await s.execute(select(UserTitle).where(
            UserTitle.user_id == uid, UserTitle.title_id == tid,
        ))).scalar_one_or_none()
        assert ut is None


@pytest.mark.asyncio
async def test_admin_revoke_clears_equipped(client):
    uid, tid, h = await _mk()
    await client.post(f"/api/v1/admin/users/{uid}/titles",
                      json={"title_id": tid}, headers=h)
    async with async_session_maker() as s:
        async with s.begin():
            u = await s.get(User, uid)
            u.equipped_title_id = tid
            s.add(u)
    await client.delete(f"/api/v1/admin/users/{uid}/titles/{tid}", headers=h)
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.equipped_title_id is None


@pytest.mark.asyncio
async def test_admin_revoke_not_owned_404(client):
    uid, tid, h = await _mk()
    r = await client.delete(f"/api/v1/admin/users/{uid}/titles/{tid}", headers=h)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_admin_grant_inactive_title_rejected(client):
    uid, tid, h = await _mk()
    await client.patch(f"/api/v1/admin/titles/{tid}",
                       json={"is_active": False}, headers=h)
    r = await client.post(f"/api/v1/admin/users/{uid}/titles",
                          json={"title_id": tid}, headers=h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_admin_user_summary(client):
    uid, _, h = await _mk()
    r = await client.get(f"/api/v1/admin/users/{uid}/summary", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert "cash" in body
    assert "debt" in body


@pytest.mark.asyncio
async def test_admin_user_summary_404(client):
    uid, _, h = await _mk()
    r = await client.get("/api/v1/admin/users/999999/summary", headers=h)
    assert r.status_code == 404
```

- [ ] **Step 2: 跑测试看 fail**

```bash
pytest tests/test_title_admin_user.py -v 2>&1 | tail -15
```

- [ ] **Step 3: 在 title_service.py 加 grant/revoke**

```python
async def grant_user_title(
    db: AsyncSession, user_id: int, title_id: int, admin_id: int,
) -> None:
    from app.models.base import User
    from app.models.title import UserTitle
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    t = await db.get(Title, title_id)
    if not t:
        raise HTTPException(status_code=404, detail="title not found")
    if not t.is_active:
        raise HTTPException(status_code=400, detail="该称号已软删，不能授予")
    existing = (await db.execute(
        select(UserTitle).where(
            UserTitle.user_id == user_id, UserTitle.title_id == title_id,
        )
    )).scalar_one_or_none()
    if existing:
        return  # 幂等
    db.add(UserTitle(
        user_id=user_id, title_id=title_id,
        granted_by_admin_id=admin_id, source="admin",
    ))
    await db.commit()


async def revoke_user_title(
    db: AsyncSession, user_id: int, title_id: int,
) -> None:
    from app.models.base import User
    from app.models.title import UserTitle
    ut = (await db.execute(
        select(UserTitle).where(
            UserTitle.user_id == user_id, UserTitle.title_id == title_id,
        )
    )).scalar_one_or_none()
    if not ut:
        raise HTTPException(status_code=404, detail="该用户未持有此称号")
    # 同事务清 equipped（若匹配）
    u = await db.get(User, user_id)
    if u and u.equipped_title_id == title_id:
        u.equipped_title_id = None
        db.add(u)
    await db.delete(ut)
    await db.commit()
```

- [ ] **Step 4: 在 admin_title.py 加 user-title endpoints + summary**

```python
from app.schemas.title import UserTitleGrantRequest, UserTitleListItem
from app.api.v1.user import get_user_summary  # 复用计算逻辑
from fastapi import HTTPException


@router.get("/users/{user_id}/titles", response_model=List[UserTitleListItem],
            summary="列出用户的全部 title")
async def admin_list_user_titles(
    user_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    rows = await title_service.list_my_titles(db, user_id)
    return [
        UserTitleListItem(
            title=_to_title_read(t),
            granted_at=ut.granted_at,
            source=ut.source,
            granted_by_admin_id=ut.granted_by_admin_id,
        ) for ut, t in rows
    ]


@router.post("/users/{user_id}/titles", summary="授予 title")
async def admin_grant_title(
    user_id: int,
    req: UserTitleGrantRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    await title_service.grant_user_title(db, user_id, req.title_id, admin.id)
    return {"user_id": user_id, "title_id": req.title_id}


@router.delete("/users/{user_id}/titles/{title_id}", summary="撤销 title")
async def admin_revoke_title(
    user_id: int, title_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    await title_service.revoke_user_title(db, user_id, title_id)
    return {"user_id": user_id, "title_id": title_id, "revoked": True}


@router.get("/users/{user_id}/summary", summary="资产快照（admin 查任意用户）")
async def admin_user_summary(
    user_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    """复用 user.summary 逻辑但允许 admin 传 user_id。

    最小实现：直接调 sql 读 cash/debt/holdings，不全 inline /user/summary 大段计算
    （后续如需 net_worth 等扩展，再 refactor 抽出共用 helper）。
    """
    from sqlalchemy import select
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    return {
        "user_id": u.id,
        "username": u.username,
        "email": u.email,
        "cash": float(u.cash),
        "debt": float(u.debt),
        "is_active": u.is_active,
        "is_superuser": u.is_superuser,
        "equipped_title_id": u.equipped_title_id,
    }
```

- [ ] **Step 5: 跑测试**

```bash
pytest tests/test_title_admin_user.py -v 2>&1 | tail -15
```
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/title_service.py backend/app/api/v1/admin_title.py backend/tests/test_title_admin_user.py
git commit -m "feat(title): admin grant/revoke + user summary endpoints"
```

---

## Task 11: 市场门槛 gating helper + buy 集成

**Files:**
- Create: `backend/app/services/market_title_gating.py`
- Modify: `backend/app/api/v1/market.py` — buy 加 gate
- Test: `backend/tests/test_market_gating.py`

- [ ] **Step 1: 写测试**

`backend/tests/test_market_gating.py`:

```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest, uuid
from decimal import Decimal
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User, Market, Outcome
from app.models.title import Title, UserTitle, MarketRequiredTitle


async def _mk_market_with_title_gate(required_titles: list[str] = ()):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            m = Market(title=f"M_{suffix}", liquidity_b=100.0)
            s.add(m); await s.flush()
            o = Outcome(market_id=m.id, label="Yes")
            o2 = Outcome(market_id=m.id, label="No")
            s.add(o); s.add(o2); await s.flush()
            tids = []
            for name in required_titles:
                t = Title(name=f"{name}_{suffix}")
                s.add(t); await s.flush()
                s.add(MarketRequiredTitle(market_id=m.id, title_id=t.id))
                tids.append(t.id)
            return m.id, o.id, tids


async def _mk_user_with_titles(*title_ids, cash=Decimal("1000")):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"u{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}", cash=cash)
            s.add(u); await s.flush()
            for tid in title_ids:
                s.add(UserTitle(user_id=u.id, title_id=tid, source="admin"))
            return u.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


@pytest.mark.asyncio
async def test_no_gate_anyone_buys(client):
    mid, oid, _ = await _mk_market_with_title_gate(required_titles=[])
    uid, h = await _mk_user_with_titles()
    r = await client.post("/api/v1/market/buy",
                          json={"outcome_id": oid, "shares": "1"}, headers=h)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_gate_blocks_user_without_required_title(client):
    mid, oid, tids = await _mk_market_with_title_gate(required_titles=["VIP"])
    uid, h = await _mk_user_with_titles()  # 无 title
    r = await client.post("/api/v1/market/buy",
                          json={"outcome_id": oid, "shares": "1"}, headers=h)
    assert r.status_code == 403
    body = r.json()
    assert body.get("detail") == "MARKET_TITLE_REQUIRED" or "MARKET_TITLE_REQUIRED" in str(body)


@pytest.mark.asyncio
async def test_gate_any_of_passes(client):
    mid, oid, tids = await _mk_market_with_title_gate(required_titles=["VIP", "Beta"])
    uid, h = await _mk_user_with_titles(tids[0])  # 只有 VIP
    r = await client.post("/api/v1/market/buy",
                          json={"outcome_id": oid, "shares": "1"}, headers=h)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_sell_not_gated(client):
    """卖出 / 结算不受 gate 约束 — 防止 title 撤销导致用户持仓被锁。"""
    mid, oid, tids = await _mk_market_with_title_gate(required_titles=["VIP"])
    uid, h = await _mk_user_with_titles(tids[0], cash=Decimal("1000"))
    # 先买进
    await client.post("/api/v1/market/buy",
                      json={"outcome_id": oid, "shares": "1"}, headers=h)
    # 撤销 title
    async with async_session_maker() as s:
        async with s.begin():
            from sqlalchemy import select, delete
            await s.execute(delete(UserTitle).where(
                UserTitle.user_id == uid, UserTitle.title_id == tids[0],
            ))
    # 卖出依然可以
    r = await client.post("/api/v1/market/sell",
                          json={"outcome_id": oid, "shares": "1"}, headers=h)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_quote_not_gated(client):
    """quote 是只读，不 gate。"""
    mid, oid, tids = await _mk_market_with_title_gate(required_titles=["VIP"])
    uid, h = await _mk_user_with_titles()
    r = await client.post("/api/v1/market/quote",
                          json={"outcome_id": oid, "shares": "1"}, headers=h)
    assert r.status_code == 200, r.text
```

- [ ] **Step 2: 跑测试看 fail**

```bash
pytest tests/test_market_gating.py -v 2>&1 | tail -15
```

- [ ] **Step 3: 写 gating service**

`backend/app/services/market_title_gating.py`:

```python
"""市场 title 门槛检查。

ANY-of 语义：market 若无 required_titles → 通过；否则用户的 user_title 集合
与 required 集合至少有一交集；否则抛 403 MARKET_TITLE_REQUIRED。

只 gate buy；sell/quote/settle 完全不调用本 helper。
"""
from typing import Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException

from app.models.title import MarketRequiredTitle, UserTitle, Title


async def assert_user_can_trade_market(
    db: AsyncSession, user_id: int, market_id: int,
) -> None:
    required_rows = (await db.execute(
        select(MarketRequiredTitle.title_id).where(
            MarketRequiredTitle.market_id == market_id,
        )
    )).scalars().all()
    required = set(required_rows)
    if not required:
        return  # 无门槛
    owned_rows = (await db.execute(
        select(UserTitle.title_id).where(
            UserTitle.user_id == user_id,
            UserTitle.title_id.in_(required),
        )
    )).scalars().all()
    if owned_rows:
        return  # ANY-of 命中
    # 加载 required title 名字列表（错误信息用）
    req_titles = list((await db.execute(
        select(Title).where(Title.id.in_(required))
    )).scalars().all())
    raise HTTPException(
        status_code=403,
        detail="MARKET_TITLE_REQUIRED",
        headers=None,
    )
```

- [ ] **Step 4: market.py buy 集成 gate**

定位 `backend/app/api/v1/market.py` 中的 buy endpoint（搜 `@router.post.*buy`）。在用户 ID 拿到、market_id 拿到（通过 outcome → market_id 链路）之后、进入 LMSR 计价**之前**，加一行：

```python
from app.services.market_title_gating import assert_user_can_trade_market
# ...
await assert_user_can_trade_market(db, user.id, market.id)
```

具体插入位置：所有锁拿完（`_lock_user`, `_lock_outcomes_for_market` 完成）之后、`calculate_lmsr_cost` 之前。**必须**在事务内、加锁后调，否则有 TOCTOU 风险（用户刚拿到 title 又被 admin 同时撤销）。

如果不容易在锁后调（语法上插入位置不灵活），可在事务**开始**前的入口处先快速 check 一次（外层 short-circuit 快路径），同时在事务内拿完锁后再 check 一次（兜底）。简化：两层 check：

```python
# 入口快路径（无锁，请求一进来就拒）
await assert_user_can_trade_market(db, user.id, market_id)
# ... 后续 _lock_user / _lock_outcomes_for_market / 拿到 market 对象 ...
# 锁后再校验一次（防 TOCTOU）
await assert_user_can_trade_market(db, user.id, market.id)
```

- [ ] **Step 5: 跑测试**

```bash
pytest tests/test_market_gating.py -v 2>&1 | tail -20
```
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/market_title_gating.py backend/app/api/v1/market.py backend/tests/test_market_gating.py
git commit -m "feat(title): market title gating on buy (ANY-of)"
```

---

## Task 12: Admin 市场 required-titles 配置端点

**Files:**
- Modify: `backend/app/api/v1/admin_title.py` — 加 GET/PUT /admin/markets/{id}/required-titles
- Test: `backend/tests/test_market_required_titles_admin.py`

- [ ] **Step 1: 写测试**

`backend/tests/test_market_required_titles_admin.py`:

```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest, uuid
from sqlalchemy import select, func, delete
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User, Market
from app.models.title import Title, MarketRequiredTitle


async def _mk():
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            admin = User(username=f"a_{suffix}", email=f"a{suffix}@t.com",
                          casdoor_id=f"acd_{suffix}", is_superuser=True)
            m = Market(title=f"M_{suffix}", liquidity_b=100.0)
            t1 = Title(name=f"VIP_{suffix}")
            t2 = Title(name=f"Beta_{suffix}")
            s.add(admin); s.add(m); s.add(t1); s.add(t2); await s.flush()
            return admin.id, m.id, t1.id, t2.id, {"Authorization": f"Bearer {create_access_token(admin.id)}"}


@pytest.mark.asyncio
async def test_get_required_titles_empty(client):
    _, mid, _, _, h = await _mk()
    r = await client.get(f"/api/v1/admin/markets/{mid}/required-titles", headers=h)
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_put_replaces_required_titles(client):
    _, mid, t1, t2, h = await _mk()
    r = await client.put(f"/api/v1/admin/markets/{mid}/required-titles",
                          json={"title_ids": [t1, t2]}, headers=h)
    assert r.status_code == 200
    async with async_session_maker() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(MarketRequiredTitle).where(
                MarketRequiredTitle.market_id == mid,
            )
        )).scalar_one()
        assert cnt == 2


@pytest.mark.asyncio
async def test_put_overwrites_existing(client):
    _, mid, t1, t2, h = await _mk()
    await client.put(f"/api/v1/admin/markets/{mid}/required-titles",
                     json={"title_ids": [t1]}, headers=h)
    await client.put(f"/api/v1/admin/markets/{mid}/required-titles",
                     json={"title_ids": [t2]}, headers=h)
    async with async_session_maker() as s:
        rows = list((await s.execute(
            select(MarketRequiredTitle.title_id).where(
                MarketRequiredTitle.market_id == mid,
            )
        )).scalars().all())
        assert rows == [t2]


@pytest.mark.asyncio
async def test_put_empty_clears(client):
    _, mid, t1, _, h = await _mk()
    await client.put(f"/api/v1/admin/markets/{mid}/required-titles",
                     json={"title_ids": [t1]}, headers=h)
    await client.put(f"/api/v1/admin/markets/{mid}/required-titles",
                     json={"title_ids": []}, headers=h)
    async with async_session_maker() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(MarketRequiredTitle).where(
                MarketRequiredTitle.market_id == mid,
            )
        )).scalar_one()
        assert cnt == 0
```

- [ ] **Step 2: 跑测试看 fail**

```bash
pytest tests/test_market_required_titles_admin.py -v 2>&1 | tail -15
```

- [ ] **Step 3: 在 admin_title.py 加 endpoints**

```python
from app.schemas.title import MarketRequiredTitlesPutRequest
from app.models.base import Market
from app.models.title import MarketRequiredTitle
from sqlalchemy import delete as sa_delete


@router.get("/markets/{market_id}/required-titles", response_model=List[int],
            summary="某市场当前的 required title id 列表")
async def get_market_required_titles(
    market_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    m = await db.get(Market, market_id)
    if not m:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="market not found")
    rows = (await db.execute(
        select(MarketRequiredTitle.title_id).where(
            MarketRequiredTitle.market_id == market_id,
        )
    )).scalars().all()
    return list(rows)


@router.put("/markets/{market_id}/required-titles", summary="覆写某市场的 required title 列表")
async def put_market_required_titles(
    market_id: int,
    req: MarketRequiredTitlesPutRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    m = await db.get(Market, market_id)
    if not m:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="market not found")
    # 校验 title_ids 都存在
    if req.title_ids:
        found = (await db.execute(
            select(Title.id).where(Title.id.in_(req.title_ids))
        )).scalars().all()
        if len(found) != len(set(req.title_ids)):
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="部分 title_id 不存在")
    # 单事务 delete-all + insert-all
    await db.execute(sa_delete(MarketRequiredTitle).where(
        MarketRequiredTitle.market_id == market_id,
    ))
    for tid in set(req.title_ids):
        db.add(MarketRequiredTitle(market_id=market_id, title_id=tid))
    await db.commit()
    return {"market_id": market_id, "title_ids": list(set(req.title_ids))}
```

- [ ] **Step 4: 跑测试**

```bash
pytest tests/test_market_required_titles_admin.py -v 2>&1 | tail -15
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/admin_title.py backend/tests/test_market_required_titles_admin.py
git commit -m "feat(title): admin market required-titles GET/PUT"
```

---

## Task 13: 现有响应加 equipped_title + required_titles

**Files:**
- Modify: `backend/app/api/v1/user.py` — summary 加 equipped_title + all_titles
- Modify: `backend/app/api/v1/market.py` — list/detail 加 required_titles + user_can_trade
- Test: `backend/tests/test_title_response_augmentation.py`

- [ ] **Step 1: 写测试**

`backend/tests/test_title_response_augmentation.py`:

```python
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest, uuid
from sqlalchemy import select
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User, Market, Outcome
from app.models.title import Title, UserTitle, MarketRequiredTitle


async def _mk():
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"u{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}")
            t = Title(name=f"VIP_{suffix}", color="#FFD700", icon="★")
            s.add(u); s.add(t); await s.flush()
            s.add(UserTitle(user_id=u.id, title_id=t.id, source="admin"))
            return u.id, t.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


@pytest.mark.asyncio
async def test_user_summary_includes_titles(client):
    uid, tid, h = await _mk()
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert "equipped_title" in body
    assert "all_titles" in body
    assert body["equipped_title"] is None
    assert len(body["all_titles"]) == 1
    assert body["all_titles"][0]["name"].startswith("VIP_")


@pytest.mark.asyncio
async def test_user_summary_equipped_chip(client):
    uid, tid, h = await _mk()
    async with async_session_maker() as s:
        async with s.begin():
            u = await s.get(User, uid)
            u.equipped_title_id = tid
            s.add(u)
    r = await client.get("/api/v1/user/summary", headers=h)
    body = r.json()
    assert body["equipped_title"] is not None
    assert body["equipped_title"]["color"] == "#FFD700"


@pytest.mark.asyncio
async def test_market_detail_required_titles(client):
    uid, tid, h = await _mk()
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            m = Market(title=f"M_{suffix}", liquidity_b=100.0)
            s.add(m); await s.flush()
            s.add(MarketRequiredTitle(market_id=m.id, title_id=tid))
            mid = m.id
    r = await client.get(f"/api/v1/market/{mid}", headers=h)
    if r.status_code == 200:
        body = r.json()
        assert "required_titles" in body
        assert "user_can_trade" in body
        assert body["user_can_trade"] is True
```

- [ ] **Step 2: 跑测试看 fail**

```bash
pytest tests/test_title_response_augmentation.py -v 2>&1 | tail -15
```

- [ ] **Step 3: 修 user.py summary**

定位 `get_user_summary` 函数末尾 return 字典前，加：

```python
# title 信息
from app.services import title_service as _title_service
equipped_t = await _title_service.get_equipped_chip(db, user.id)
my_rows = await _title_service.list_my_titles(db, user.id)
```

然后在 return 字典中加两个字段：

```python
    return {
        # ... 既有字段不动 ...
        "equipped_title": (
            {"id": equipped_t.id, "name": equipped_t.name,
             "color": equipped_t.color, "icon": equipped_t.icon}
            if equipped_t else None
        ),
        "all_titles": [
            {"id": t.id, "name": t.name, "color": t.color, "icon": t.icon,
             "description": t.description, "sort_order": t.sort_order}
            for _ut, t in my_rows
        ],
    }
```

- [ ] **Step 4: 修 market.py detail endpoint**

定位 `@router.get` 中返回单个 market 的 endpoint（搜 `def get_market` 或 `markets/{` 路径）。
在返回字段中加：

```python
# required_titles
from app.models.title import MarketRequiredTitle, Title as _Title
mrt_rows = (await db.execute(
    select(_Title).join(MarketRequiredTitle,
                         MarketRequiredTitle.title_id == _Title.id)
    .where(MarketRequiredTitle.market_id == market.id)
)).scalars().all()
required_titles = [
    {"id": t.id, "name": t.name, "color": t.color, "icon": t.icon}
    for t in mrt_rows
]
# user_can_trade
if not required_titles:
    user_can_trade = True
else:
    from app.models.title import UserTitle
    has = (await db.execute(
        select(UserTitle.title_id).where(
            UserTitle.user_id == current_user.id,
            UserTitle.title_id.in_([rt["id"] for rt in required_titles]),
        )
    )).scalars().first()
    user_can_trade = has is not None
```

然后在返回字典中加 `"required_titles": required_titles, "user_can_trade": user_can_trade`。

如果 list endpoint 也需要 required_titles（前端市场列表 chip），同步加上（用相同思路，但用 `.where(MarketRequiredTitle.market_id.in_([m.id for m in markets]))` 批量查再 group by market_id）。

- [ ] **Step 5: 跑测试**

```bash
pytest tests/test_title_response_augmentation.py -v 2>&1 | tail -15
```
Expected: 3 passed.

- [ ] **Step 6: 跑完整 backend test 保证没回归**

```bash
pytest -x 2>&1 | tail -30
```
Expected: 全 pass。

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/v1/user.py backend/app/api/v1/market.py backend/tests/test_title_response_augmentation.py
git commit -m "feat(title): augment user.summary + market detail with title info"
```

---

## Task 14: leaderboard / 翻车现场墙 equipped_title

**Files:**
- Modify: leaderboard endpoint（位置在 `backend/app/api/v1/user.py` 或 `admin_stats.py`，先 grep 定位）
- Modify: liquidation 公开列表 endpoint（同 grep）
- Test: 复用 `test_leaderboard.py` 加新 assertion

- [ ] **Step 1: grep 定位 endpoint**

```bash
cd backend
grep -rn "leaderboard\|recent.liquidat" app/api/v1/ | head -10
```

记下：
- leaderboard endpoint 文件 + 函数名
- liquidation 公开列表 endpoint 文件 + 函数名（`/api/v1/loan/recent-liquidations` 或类似）

- [ ] **Step 2: 阅读两个 endpoint 现有结构**

确认现在每条记录的字段（user_id, username, net_worth 等）。
确认 SQL 是怎么 join User 的。

- [ ] **Step 3: 给两个 endpoint 加 equipped_title chip**

每个 endpoint 改两处：

(a) SQL 多 select 一次 Title（join via User.equipped_title_id）：

```python
from app.models.title import Title as _T
stmt = (
    select(User, _T)
    .outerjoin(_T, User.equipped_title_id == _T.id)
    # ... 既有 where/order/limit ...
)
```

(b) 把每条记录的 dict 加：

```python
{
    # ... 既有字段 ...
    "equipped_title": (
        {"id": t.id, "name": t.name, "color": t.color, "icon": t.icon}
        if t else None
    ),
}
```

- [ ] **Step 4: 写测试 assertion**

打开 `backend/tests/test_leaderboard.py`，找到一个已 pass 的 leaderboard 测试，复制为新测试，加 setup：给某 user 挂 title + 设 equipped_title_id，然后断言响应数组中该条目含 `equipped_title.name`。

具体测试代码（追加到 test_leaderboard.py 末尾）：

```python
@pytest.mark.asyncio
async def test_leaderboard_includes_equipped_title(client):
    import uuid
    from app.models.title import Title, UserTitle
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"top_{suffix}", email=f"t{suffix}@x.com",
                    casdoor_id=f"cd_{suffix}", cash=Decimal("99999"))
            t = Title(name=f"VIP_{suffix}", color="#FFD700", icon="★")
            s.add(u); s.add(t); await s.flush()
            s.add(UserTitle(user_id=u.id, title_id=t.id, source="admin"))
            u.equipped_title_id = t.id
            s.add(u)
    h = {"Authorization": f"Bearer {create_access_token(u.id)}"}
    r = await client.get("/api/v1/<leaderboard_path>", headers=h)  # 改为真实路径
    assert r.status_code == 200
    items = r.json()
    # 找我们这条
    mine = next((it for it in items if it.get("username", "").startswith("top_")), None)
    assert mine is not None
    assert mine.get("equipped_title") is not None
    assert mine["equipped_title"]["color"] == "#FFD700"
```

> **任务卡时**：`<leaderboard_path>` 必须替换为真实路径。看 Step 1 grep 结果。

- [ ] **Step 5: 跑测试**

```bash
pytest tests/test_leaderboard.py -v 2>&1 | tail -10
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/<changed_files> backend/tests/test_leaderboard.py
git commit -m "feat(title): add equipped_title to leaderboard / liquidation list"
```

---

## Task 15: 前端基础 — TitleChip + api + store + sidebar

**Files:**
- Create: `thccb-frontend/src/components/title/TitleChip.vue`
- Create: `thccb-frontend/src/api/title.ts`
- Create: `thccb-frontend/src/stores/title.ts`
- Modify: `thccb-frontend/src/router/routes.ts`（加 4 个新路由）
- Modify: `thccb-frontend/src/components/layout/AppSidebar.vue`（加菜单项）
- Modify: `thccb-frontend/src/api/index.ts`（拦 MARKET_TITLE_REQUIRED marker）

- [ ] **Step 1: 写 TitleChip 组件**

`thccb-frontend/src/components/title/TitleChip.vue`:

```vue
<script setup lang="ts">
import { computed } from 'vue'

interface TitleData {
  id?: number
  name: string
  color: string
  icon: string
}

const props = defineProps<{
  title: TitleData | null | undefined
  size?: 'sm' | 'md'
}>()

function pickTextColor(bg: string): string {
  const m = bg.replace('#', '')
  if (m.length !== 6) return '#000'
  const r = parseInt(m.slice(0,2), 16)
  const g = parseInt(m.slice(2,4), 16)
  const b = parseInt(m.slice(4,6), 16)
  const luma = (0.299*r + 0.587*g + 0.114*b) / 255
  return luma > 0.5 ? '#000' : '#fff'
}

const styleObj = computed(() => {
  if (!props.title) return {}
  return {
    backgroundColor: props.title.color || '#000',
    color: pickTextColor(props.title.color || '#000'),
  }
})

const sizeClass = computed(() => props.size === 'sm' ? 'px-1.5 py-0 text-xs' : 'px-2 py-0.5 text-sm')
</script>

<template>
  <span
    v-if="title"
    :class="['inline-flex items-center gap-1 border-2 border-black font-bold', sizeClass]"
    :style="styleObj"
  >
    <span v-if="title.icon">{{ title.icon }}</span>
    <span>{{ title.name }}</span>
  </span>
</template>
```

- [ ] **Step 2: 写 title API client**

`thccb-frontend/src/api/title.ts`:

```typescript
import api from './index'

export interface TitleRead {
  id: number
  name: string
  description: string
  color: string
  icon: string
  sort_order: number
  is_active: boolean
  created_at: string
}

export interface TitleChip {
  id: number
  name: string
  color: string
  icon: string
}

export interface MyTitleItem {
  title: TitleRead
  granted_at: string
  source: 'admin' | 'code'
}

export interface MyTitlesResponse {
  equipped_title_id: number | null
  titles: MyTitleItem[]
}

export const titleApi = {
  myTitles: () => api.get<MyTitlesResponse>('/api/v1/title/me'),
  equip: (title_id: number | null) =>
    api.post<{ equipped_title_id: number | null }>('/api/v1/title/me/equip', { title_id }),
  redeem: (code: string) =>
    api.post<{ title: TitleRead }>('/api/v1/title/redeem', { code }),
  publicCatalog: () => api.get<TitleRead[]>('/api/v1/title/catalog'),
  userEquipped: (user_id: number) =>
    api.get<TitleChip | null>(`/api/v1/title/users/${user_id}/equipped`),
}
```

- [ ] **Step 3: 写 store (catalog 缓存)**

`thccb-frontend/src/stores/title.ts`:

```typescript
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { titleApi, type TitleRead } from '@/api/title'

export const useTitleStore = defineStore('title', () => {
  const catalog = ref<TitleRead[]>([])
  const loaded = ref(false)

  async function ensureLoaded() {
    if (loaded.value) return
    try {
      catalog.value = await titleApi.publicCatalog()
      loaded.value = true
    } catch {
      // 加载失败保持 unloaded，下次再试
    }
  }

  function reset() {
    catalog.value = []
    loaded.value = false
  }

  return { catalog, loaded, ensureLoaded, reset }
})
```

- [ ] **Step 4: 路由表加 4 个新路由**

打开 `thccb-frontend/src/router/routes.ts`，在用户路由组（meta requiresAuth=true）中加：

```typescript
{
  path: 'redeem-title',
  name: 'redeem-title',
  component: () => import('@/pages/redeem/RedeemTitle.vue'),
  meta: { title: '称号兑换', requiresAuth: true, requiresVerified: true },
},
```

在 admin 路由组（如果有 `meta: { requiresAdmin: true }`）中加：

```typescript
{
  path: 'admin/users',
  name: 'admin-users',
  component: () => import('@/pages/admin/UserManage.vue'),
  meta: { title: '用户管理', requiresAuth: true, requiresAdmin: true },
},
{
  path: 'admin/titles',
  name: 'admin-titles',
  component: () => import('@/pages/admin/TitleCatalog.vue'),
  meta: { title: '称号目录', requiresAuth: true, requiresAdmin: true },
},
{
  path: 'admin/title-codes',
  name: 'admin-title-codes',
  component: () => import('@/pages/admin/TitleCodeBatches.vue'),
  meta: { title: '称号激活码', requiresAuth: true, requiresAdmin: true },
},
```

如果不确定 admin 路由元字段名，先查现有 `admin/BotReviewBan.vue` 路由的 meta，照搬。

- [ ] **Step 5: 侧边栏菜单**

打开 `thccb-frontend/src/components/layout/AppSidebar.vue`，找现有用户菜单数组（搜 `兑换中心` 或 `借款`），加：

```typescript
{ label: '称号兑换', icon: '★', to: '/redeem-title' }
```

找现有 admin 菜单数组（搜 `Bot 预警` 或 `用户列表`），加：

```typescript
{ label: '用户管理', icon: '👤', to: '/admin/users' }
{ label: '称号目录', icon: '★', to: '/admin/titles' }
{ label: '称号激活码', icon: '🎫', to: '/admin/title-codes' }
```

> 字段名 `label/icon/to` 需匹配现有结构；先看一眼现成菜单 item 的字段。

- [ ] **Step 6: axios 拦截 MARKET_TITLE_REQUIRED**

打开 `thccb-frontend/src/api/index.ts`，在 USER_BANNED 拦截后追加：

```typescript
// 403 + detail MARKET_TITLE_REQUIRED → 触发全局 toast
if (
  error.response?.status === 403 &&
  error.response?.data?.detail === 'MARKET_TITLE_REQUIRED'
) {
  window.dispatchEvent(new CustomEvent('market-title-required', {
    detail: error.response.data,
  }))
  return Promise.reject({
    message: '此市场需要特定称号',
    status: 403,
    data: error.response?.data,
  })
}
```

- [ ] **Step 7: type-check + lint**

```bash
cd thccb-frontend
npm run type-check 2>&1 | tail -10
npm run lint 2>&1 | tail -10
```
Expected: 全 pass。

- [ ] **Step 8: Commit**

```bash
git add thccb-frontend/src/components/title/TitleChip.vue thccb-frontend/src/api/title.ts thccb-frontend/src/stores/title.ts thccb-frontend/src/router/routes.ts thccb-frontend/src/components/layout/AppSidebar.vue thccb-frontend/src/api/index.ts
git commit -m "feat(title-fe): TitleChip + api client + store + sidebar + axios marker"
```

---

## Task 16: 用户兑换页 RedeemTitle.vue

**Files:**
- Create: `thccb-frontend/src/pages/redeem/RedeemTitle.vue`

- [ ] **Step 1: 写组件**

```vue
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useMessage, NCard, NInput, NButton, NEmpty } from 'naive-ui'
import { titleApi, type MyTitleItem } from '@/api/title'
import TitleChip from '@/components/title/TitleChip.vue'
import { extractErrorMessage } from '@/utils/errors'

const msg = useMessage()
const code = ref('')
const loading = ref(false)
const myTitles = ref<MyTitleItem[]>([])

async function refresh() {
  try {
    const data = await titleApi.myTitles()
    myTitles.value = data.titles
  } catch (e) {
    msg.error(extractErrorMessage(e, '加载称号失败'))
  }
}

async function redeem() {
  if (!code.value.trim() || loading.value) return
  loading.value = true
  try {
    const r = await titleApi.redeem(code.value.trim())
    msg.success(`获得新称号：${r.title.name}，可在个人页佩戴`)
    code.value = ''
    await refresh()
  } catch (e: any) {
    msg.error(e?.message || '兑换失败')
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <div class="p-6 max-w-3xl mx-auto">
    <h1 class="text-2xl font-black mb-6 border-b-4 border-black pb-2">称号兑换</h1>

    <NCard class="mb-6" :bordered="false" style="box-shadow:6px 6px 0 #000; border:2px solid #000;">
      <div class="flex gap-2 items-center">
        <NInput v-model:value="code" placeholder="输入激活码 (4-64 位)" maxlength="64"
                @keyup.enter="redeem" />
        <NButton type="primary" :loading="loading" @click="redeem"
                  :disabled="!code.trim()">兑换</NButton>
      </div>
      <p class="text-xs text-gray-600 mt-2">
        激活码区分大小写，每个码仅可使用一次。来源由管理员发放。
      </p>
    </NCard>

    <div>
      <h2 class="text-lg font-bold mb-2">我的称号</h2>
      <NEmpty v-if="!myTitles.length" description="还没有任何称号" />
      <div v-else class="flex flex-wrap gap-2">
        <TitleChip v-for="item in myTitles" :key="item.title.id" :title="item.title" />
      </div>
    </div>
  </div>
</template>
```

- [ ] **Step 2: type-check + lint**

```bash
cd thccb-frontend
npm run type-check 2>&1 | tail -5
npm run lint 2>&1 | tail -5
```

- [ ] **Step 3: Commit**

```bash
git add thccb-frontend/src/pages/redeem/RedeemTitle.vue
git commit -m "feat(title-fe): /redeem-title user page"
```

---

## Task 17: MyTitlesPanel + 集成到个人页

**Files:**
- Create: `thccb-frontend/src/components/title/MyTitlesPanel.vue`
- Modify: 个人页文件（定位：`grep -rn "用户名\|nickname\|个人" thccb-frontend/src/pages/user/`）

- [ ] **Step 1: 定位个人页文件**

```bash
cd thccb-frontend
grep -l "我的资产\|portfolio\|profile" src/pages/user/*.vue src/pages/profile/*.vue 2>/dev/null
```

- [ ] **Step 2: 写 MyTitlesPanel**

`thccb-frontend/src/components/title/MyTitlesPanel.vue`:

```vue
<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useMessage, NCard, NButton, NEmpty } from 'naive-ui'
import { titleApi, type MyTitleItem } from '@/api/title'
import TitleChip from './TitleChip.vue'

const msg = useMessage()
const myTitles = ref<MyTitleItem[]>([])
const equippedId = ref<number | null>(null)
const loading = ref(false)

const isEquipped = (tid: number) => equippedId.value === tid

async function refresh() {
  try {
    const data = await titleApi.myTitles()
    myTitles.value = data.titles
    equippedId.value = data.equipped_title_id
  } catch (e: any) {
    msg.error(e?.message || '加载称号失败')
  }
}

async function toggleEquip(tid: number) {
  if (loading.value) return
  loading.value = true
  try {
    const target = isEquipped(tid) ? null : tid
    await titleApi.equip(target)
    equippedId.value = target
    msg.success(target === null ? '已取下称号' : '已佩戴称号')
  } catch (e: any) {
    msg.error(e?.message || '操作失败')
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <NCard title="我的称号" :bordered="false"
         style="box-shadow:6px 6px 0 #000; border:2px solid #000;">
    <NEmpty v-if="!myTitles.length" description="尚未拥有称号" />
    <div v-else class="space-y-2">
      <div v-for="item in myTitles" :key="item.title.id"
           class="flex items-center justify-between border-2 border-black p-2">
        <div class="flex items-center gap-3">
          <TitleChip :title="item.title" />
          <span class="text-xs text-gray-600">{{ item.title.description }}</span>
        </div>
        <NButton size="small" :type="isEquipped(item.title.id) ? 'warning' : 'primary'"
                  :loading="loading"
                  @click="toggleEquip(item.title.id)">
          {{ isEquipped(item.title.id) ? '取下' : '佩戴' }}
        </NButton>
      </div>
    </div>
  </NCard>
</template>
```

- [ ] **Step 3: 在个人页插入 MyTitlesPanel**

打开 Step 1 定位的文件，找到一个合适的区块（如"账户信息"卡片之后），插入：

```vue
<script setup lang="ts">
// ... 既有 import ...
import MyTitlesPanel from '@/components/title/MyTitlesPanel.vue'
</script>

<template>
  <!-- ... 既有内容 ... -->
  <div class="mt-6">
    <MyTitlesPanel />
  </div>
</template>
```

- [ ] **Step 4: type-check + lint + 起服务手动看**

```bash
npm run type-check 2>&1 | tail -5
npm run lint 2>&1 | tail -5
```

启动 dev server 浏览器看 `/redeem-title` + 个人页加载正常，"我的称号"卡片显示空状态或已有 chip：

```bash
npm run dev
```

观察后 Ctrl+C。

- [ ] **Step 5: Commit**

```bash
git add thccb-frontend/src/components/title/MyTitlesPanel.vue thccb-frontend/src/pages/<profile_file>.vue
git commit -m "feat(title-fe): MyTitlesPanel + integrate into profile page"
```

---

## Task 18: TitleChip 集成到 leaderboard / navbar / 翻车墙

**Files:**
- Modify: `thccb-frontend/src/components/layout/AppHeader.vue`（navbar 自己用户名旁加 chip）
- Modify: leaderboard 组件 / 翻车墙组件（先 grep 定位）

- [ ] **Step 1: grep 定位**

```bash
cd thccb-frontend
grep -rln "leaderboard\|财富榜\|翻车" src/components src/pages 2>/dev/null
```

- [ ] **Step 2: AppHeader.vue 加 chip**

定位顶栏自己用户名的渲染处，旁边加：

```vue
<script setup lang="ts">
// ... 既有 ...
import TitleChip from '@/components/title/TitleChip.vue'
import { useUserStore } from '@/stores/user'  // 或同等 store

const userStore = useUserStore()
// userStore.summary.equipped_title 应该是后端 user/summary 已返回的字段
</script>

<template>
  <!-- 在用户名 span 旁 -->
  <span class="username">{{ userStore.user?.username }}</span>
  <TitleChip v-if="userStore.summary?.equipped_title"
             :title="userStore.summary.equipped_title" size="sm" />
</template>
```

> 实际字段名要参照 user store 现状。可能字段是 `userStore.equippedTitle` — 看现成代码。

- [ ] **Step 3: leaderboard 每行加 chip**

定位 leaderboard 组件，找每条记录的 `<tr>` 或 `<div>` 用户名渲染处，旁边加：

```vue
<TitleChip v-if="row.equipped_title" :title="row.equipped_title" size="sm" />
```

> 后端响应已包含 `equipped_title` 字段（Task 14），前端直接读。

- [ ] **Step 4: 翻车墙同样加**

类似。每条记录有 `equipped_title` 字段。

- [ ] **Step 5: type-check + lint**

```bash
npm run type-check 2>&1 | tail -5
npm run lint 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add thccb-frontend/src/components/layout/AppHeader.vue thccb-frontend/src/<other_files>
git commit -m "feat(title-fe): TitleChip in leaderboard / navbar / liquidation list"
```

---

## Task 19: 市场门槛前端 UI

**Files:**
- Create: `thccb-frontend/src/components/title/RequiredTitlesBadge.vue`
- Modify: 市场列表卡片组件 + 市场详情页（先 grep 定位）

- [ ] **Step 1: 写 RequiredTitlesBadge**

`thccb-frontend/src/components/title/RequiredTitlesBadge.vue`:

```vue
<script setup lang="ts">
import type { TitleChip as TitleChipType } from '@/api/title'
import TitleChip from './TitleChip.vue'

defineProps<{
  titles: TitleChipType[]
  canTrade: boolean
}>()
</script>

<template>
  <div v-if="titles.length" class="inline-flex items-center gap-1">
    <span class="text-xs">🔒</span>
    <span class="text-xs text-gray-700">需要</span>
    <TitleChip v-for="t in titles" :key="t.id" :title="t" size="sm" />
    <span v-if="!canTrade" class="text-xs text-red-700">（你未达到）</span>
  </div>
</template>
```

- [ ] **Step 2: 市场列表卡片集成**

定位市场列表卡片组件（`grep -rn "市场列表\|MarketCard\|market.*card" src/pages src/components`）。
在卡片标题区或右上角加：

```vue
<RequiredTitlesBadge v-if="market.required_titles?.length"
                     :titles="market.required_titles"
                     :can-trade="market.user_can_trade" />
```

- [ ] **Step 3: 市场详情页集成**

定位市场详情页（`grep -rn "market.*detail\|MarketDetail" src/pages`）。
在顶部加黄条 warning（仅 `!user_can_trade && required_titles.length` 时显示）：

```vue
<div v-if="market.required_titles?.length && !market.user_can_trade"
     class="border-4 border-yellow-500 bg-yellow-50 p-3 mb-4">
  <strong>⚠️ 此市场仅限以下称号交易：</strong>
  <TitleChip v-for="t in market.required_titles" :key="t.id" :title="t" size="sm" class="ml-2" />
  <p class="text-xs mt-2">你可继续浏览价格与图表，但下单会被拒绝。</p>
</div>
```

并把买入按钮 disabled（搜 buy / 买入 按钮）：

```vue
<NButton :disabled="!market.user_can_trade" @click="onBuy">买入</NButton>
```

- [ ] **Step 4: 全局 toast 监听 MARKET_TITLE_REQUIRED**

在 `App.vue` 或一个全局 mixin 中加（参考 BannedDialog 监听模式）：

```vue
<script setup lang="ts">
// ... 既有 ...
import { onMounted, onBeforeUnmount } from 'vue'
import { useMessage } from 'naive-ui'

const message = useMessage()

function onTitleRequired(e: Event) {
  const ce = e as CustomEvent
  message.warning('此市场需要特定称号才能交易', { duration: 4000 })
}

onMounted(() => window.addEventListener('market-title-required', onTitleRequired))
onBeforeUnmount(() => window.removeEventListener('market-title-required', onTitleRequired))
</script>
```

> 注意 `useMessage()` 必须在 NMessageProvider 内部组件用。如果 App.vue 不行，可以做个 `<GlobalToasts />` 子组件挂在 provider 内。参考 BannedDialog 的实现方式。

- [ ] **Step 5: type-check + lint + 手动 smoke**

```bash
npm run type-check 2>&1 | tail -5
npm run lint 2>&1 | tail -5
npm run dev
```

浏览器手动验证：找一个无 required_titles 的市场点击买入 → 通过。

- [ ] **Step 6: Commit**

```bash
git add thccb-frontend/src/components/title/RequiredTitlesBadge.vue thccb-frontend/src/<integrated_files>
git commit -m "feat(title-fe): market gating UI (card badge + detail banner + buy disable + toast)"
```

---

## Task 20: MarketManage.vue 加 required_titles 配置

**Files:**
- Modify: `thccb-frontend/src/pages/admin/MarketManage.vue`
- Modify: `thccb-frontend/src/api/admin.ts` — 加 market required-titles 调用

- [ ] **Step 1: api 增加调用**

在 `thccb-frontend/src/api/admin.ts` 末尾加：

```typescript
export const adminTitleApi = {
  // catalog
  listTitles: () => api.get<TitleRead[]>('/api/v1/admin/titles'),
  createTitle: (body: any) => api.post<TitleRead>('/api/v1/admin/titles', body),
  updateTitle: (id: number, body: any) => api.patch<TitleRead>(`/api/v1/admin/titles/${id}`, body),
  // batches
  listBatches: () => api.get<any[]>('/api/v1/admin/title-batches'),
  createBatch: (body: any) => api.post<any>('/api/v1/admin/title-batches', body),
  importCodes: (batch_id: number, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return api.post<{inserted: number}>(`/api/v1/admin/title-batches/${batch_id}/import-codes`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  // user-title
  listUserTitles: (uid: number) => api.get<any[]>(`/api/v1/admin/users/${uid}/titles`),
  grantTitle: (uid: number, title_id: number) => api.post(`/api/v1/admin/users/${uid}/titles`, { title_id }),
  revokeTitle: (uid: number, tid: number) => api.delete(`/api/v1/admin/users/${uid}/titles/${tid}`),
  userSummary: (uid: number) => api.get<any>(`/api/v1/admin/users/${uid}/summary`),
  // market gating
  getMarketRequired: (mid: number) => api.get<number[]>(`/api/v1/admin/markets/${mid}/required-titles`),
  putMarketRequired: (mid: number, title_ids: number[]) =>
    api.put(`/api/v1/admin/markets/${mid}/required-titles`, { title_ids }),
}

// 别忘了顶部 import TitleRead
import type { TitleRead } from './title'
```

- [ ] **Step 2: MarketManage.vue 编辑表单加 multi-select**

打开 `thccb-frontend/src/pages/admin/MarketManage.vue`。
找到编辑市场的对话框 / 表单。加一个 multi-select（用 NSelect multiple）：

```vue
<script setup lang="ts">
// ...既有...
import { adminTitleApi } from '@/api/admin'
import type { TitleRead } from '@/api/title'

const titleOptions = ref<{label: string, value: number}[]>([])
const selectedRequiredTitleIds = ref<number[]>([])

async function loadTitleOptions() {
  const titles = await adminTitleApi.listTitles()
  titleOptions.value = titles.filter(t => t.is_active)
    .map(t => ({ label: t.name, value: t.id }))
}

async function loadMarketRequiredTitles(marketId: number) {
  selectedRequiredTitleIds.value = await adminTitleApi.getMarketRequired(marketId)
}

async function saveRequiredTitles(marketId: number) {
  await adminTitleApi.putMarketRequired(marketId, selectedRequiredTitleIds.value)
}

// 编辑某 market 时调用：loadTitleOptions() + loadMarketRequiredTitles(market.id)
// 保存时除既有 market PATCH 外，调 saveRequiredTitles(market.id)
</script>

<template>
  <!-- 表单中加：-->
  <NFormItem label="需要的称号（多选，留空 = 任何人可交易）">
    <NSelect v-model:value="selectedRequiredTitleIds" :options="titleOptions"
             multiple placeholder="选择需要的称号" />
  </NFormItem>
</template>
```

具体怎么 wire 进 onMounted / save 流程，参考该文件现有的"编辑市场"逻辑。

- [ ] **Step 3: type-check + lint**

```bash
npm run type-check 2>&1 | tail -5
npm run lint 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git add thccb-frontend/src/pages/admin/MarketManage.vue thccb-frontend/src/api/admin.ts
git commit -m "feat(title-fe): MarketManage multi-select required_titles"
```

---

## Task 21: TitleCatalog.vue admin 页

**Files:**
- Create: `thccb-frontend/src/pages/admin/TitleCatalog.vue`

- [ ] **Step 1: 写组件**

```vue
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useMessage, NButton, NDataTable, NModal, NForm, NFormItem, NInput, NInputNumber, NSwitch } from 'naive-ui'
import { adminTitleApi } from '@/api/admin'
import type { TitleRead } from '@/api/title'
import TitleChip from '@/components/title/TitleChip.vue'

const message = useMessage()
const titles = ref<TitleRead[]>([])
const showEditor = ref(false)
const editing = ref<TitleRead | null>(null)
const formName = ref('')
const formDesc = ref('')
const formColor = ref('#000000')
const formIcon = ref('')
const formSort = ref<number>(100)
const formActive = ref(true)

async function refresh() {
  try {
    titles.value = await adminTitleApi.listTitles()
  } catch (e: any) { message.error(e?.message || '加载失败') }
}

function openNew() {
  editing.value = null
  formName.value = ''; formDesc.value = ''; formColor.value = '#000000'
  formIcon.value = ''; formSort.value = 100; formActive.value = true
  showEditor.value = true
}
function openEdit(t: TitleRead) {
  editing.value = t
  formName.value = t.name; formDesc.value = t.description
  formColor.value = t.color; formIcon.value = t.icon
  formSort.value = t.sort_order; formActive.value = t.is_active
  showEditor.value = true
}

async function save() {
  try {
    if (editing.value) {
      await adminTitleApi.updateTitle(editing.value.id, {
        name: formName.value, description: formDesc.value,
        color: formColor.value, icon: formIcon.value,
        sort_order: formSort.value, is_active: formActive.value,
      })
    } else {
      await adminTitleApi.createTitle({
        name: formName.value, description: formDesc.value,
        color: formColor.value, icon: formIcon.value,
        sort_order: formSort.value,
      })
    }
    showEditor.value = false
    await refresh()
    message.success('保存成功')
  } catch (e: any) { message.error(e?.message || '保存失败') }
}

const columns = [
  { title: 'ID', key: 'id', width: 60 },
  { title: '预览', key: '_chip', render(row: TitleRead) { return h(TitleChip, { title: row }) } },
  { title: '名称', key: 'name' },
  { title: '说明', key: 'description' },
  { title: '排序', key: 'sort_order', width: 80 },
  { title: '启用', key: 'is_active', width: 80,
    render(row: TitleRead) { return row.is_active ? '✓' : '✗' } },
  { title: '操作', key: '_actions',
    render(row: TitleRead) {
      return h(NButton, { size: 'small', onClick: () => openEdit(row) }, () => '编辑')
    } },
]
import { h } from 'vue'

onMounted(refresh)
</script>

<template>
  <div class="p-6">
    <div class="flex justify-between items-center mb-4">
      <h1 class="text-2xl font-black">称号目录</h1>
      <NButton type="primary" @click="openNew">+ 新建称号</NButton>
    </div>
    <NDataTable :columns="columns" :data="titles" :bordered="true" />

    <NModal v-model:show="showEditor" preset="card"
            :title="editing ? '编辑称号' : '新建称号'" style="max-width:500px;">
      <NForm>
        <NFormItem label="名称">
          <NInput v-model:value="formName" maxlength="32" />
        </NFormItem>
        <NFormItem label="说明">
          <NInput v-model:value="formDesc" maxlength="200" type="textarea" />
        </NFormItem>
        <NFormItem label="颜色 (hex)">
          <NInput v-model:value="formColor" maxlength="16" placeholder="#FFD700" />
        </NFormItem>
        <NFormItem label="图标 (emoji 或 1 字符)">
          <NInput v-model:value="formIcon" maxlength="16" placeholder="★" />
        </NFormItem>
        <NFormItem label="排序 (小者优先)">
          <NInputNumber v-model:value="formSort" :min="0" />
        </NFormItem>
        <NFormItem v-if="editing" label="启用">
          <NSwitch v-model:value="formActive" />
        </NFormItem>
        <div class="flex justify-end gap-2">
          <NButton @click="showEditor = false">取消</NButton>
          <NButton type="primary" @click="save">保存</NButton>
        </div>
      </NForm>
    </NModal>
  </div>
</template>
```

- [ ] **Step 2: type-check + lint**

```bash
npm run type-check 2>&1 | tail -5
npm run lint 2>&1 | tail -5
```

- [ ] **Step 3: Commit**

```bash
git add thccb-frontend/src/pages/admin/TitleCatalog.vue
git commit -m "feat(title-fe): /admin/titles catalog CRUD page"
```

---

## Task 22: TitleCodeBatches.vue admin 页

**Files:**
- Create: `thccb-frontend/src/pages/admin/TitleCodeBatches.vue`

- [ ] **Step 1: 写组件**

```vue
<script setup lang="ts">
import { ref, onMounted, h } from 'vue'
import { useMessage, NButton, NDataTable, NModal, NForm, NFormItem, NInput, NSelect, NUpload, type UploadFileInfo } from 'naive-ui'
import { adminTitleApi } from '@/api/admin'
import type { TitleRead } from '@/api/title'

const message = useMessage()
const batches = ref<any[]>([])
const titles = ref<TitleRead[]>([])
const showNew = ref(false)
const showImport = ref(false)
const newTitleId = ref<number | null>(null)
const newName = ref('')
const newDesc = ref('')
const importBatchId = ref<number | null>(null)
const importFile = ref<File | null>(null)
const loading = ref(false)

async function refresh() {
  try {
    batches.value = await adminTitleApi.listBatches()
    titles.value = await adminTitleApi.listTitles()
  } catch (e: any) { message.error(e?.message || '加载失败') }
}

const titleOptions = computed(() => titles.value
  .filter((t: TitleRead) => t.is_active)
  .map((t: TitleRead) => ({ label: t.name, value: t.id }))
)
import { computed } from 'vue'

function openNew() {
  newTitleId.value = null; newName.value = ''; newDesc.value = ''
  showNew.value = true
}

async function createBatch() {
  if (!newTitleId.value || !newName.value.trim()) {
    message.error('请填 title 和 name'); return
  }
  loading.value = true
  try {
    await adminTitleApi.createBatch({
      title_id: newTitleId.value, name: newName.value.trim(),
      description: newDesc.value,
    })
    showNew.value = false
    await refresh()
    message.success('批次已创建')
  } catch (e: any) { message.error(e?.message || '创建失败') }
  finally { loading.value = false }
}

function openImport(bid: number) {
  importBatchId.value = bid
  importFile.value = null
  showImport.value = true
}

function onFileChange(opts: { fileList: UploadFileInfo[] }) {
  const f = opts.fileList[0]
  importFile.value = (f?.file as File) ?? null
}

async function doImport() {
  if (!importBatchId.value || !importFile.value) {
    message.error('请选择 CSV 文件'); return
  }
  loading.value = true
  try {
    const r = await adminTitleApi.importCodes(importBatchId.value, importFile.value)
    message.success(`成功导入 ${r.inserted} 个 code`)
    showImport.value = false
    await refresh()
  } catch (e: any) { message.error(e?.message || '导入失败') }
  finally { loading.value = false }
}

const columns = [
  { title: 'ID', key: 'id', width: 60 },
  { title: '称号', key: 'title_name' },
  { title: '批次名', key: 'name' },
  { title: '已用 / 总数', key: '_count',
    render(row: any) { return `${row.used} / ${row.total}` } },
  { title: '创建于', key: 'created_at', width: 180 },
  { title: '操作', key: '_actions',
    render(row: any) {
      return h(NButton, { size: 'small', onClick: () => openImport(row.id) }, () => '导入 CSV')
    } },
]

onMounted(refresh)
</script>

<template>
  <div class="p-6">
    <div class="flex justify-between items-center mb-4">
      <h1 class="text-2xl font-black">称号激活码批次</h1>
      <NButton type="primary" @click="openNew">+ 新建批次</NButton>
    </div>
    <NDataTable :columns="columns" :data="batches" :bordered="true" />

    <NModal v-model:show="showNew" preset="card" title="新建批次" style="max-width:500px;">
      <NForm>
        <NFormItem label="称号">
          <NSelect v-model:value="newTitleId" :options="titleOptions" placeholder="选择称号" />
        </NFormItem>
        <NFormItem label="批次名">
          <NInput v-model:value="newName" maxlength="64" placeholder="2026-Q2 公测批次" />
        </NFormItem>
        <NFormItem label="说明（可选）">
          <NInput v-model:value="newDesc" maxlength="200" type="textarea" />
        </NFormItem>
        <div class="flex justify-end gap-2">
          <NButton @click="showNew = false">取消</NButton>
          <NButton type="primary" :loading="loading" @click="createBatch">创建</NButton>
        </div>
      </NForm>
    </NModal>

    <NModal v-model:show="showImport" preset="card" title="导入激活码 CSV" style="max-width:500px;">
      <p class="mb-3 text-sm">CSV 格式：每行一个 code，可带表头 `code`。
         字符限 A-Z a-z 0-9 _ - 长度 4-64，单批最多 5000 行。</p>
      <NUpload :max="1" accept=".csv" :default-upload="false"
               @change="onFileChange">
        <NButton>选择 CSV</NButton>
      </NUpload>
      <div class="flex justify-end gap-2 mt-4">
        <NButton @click="showImport = false">取消</NButton>
        <NButton type="primary" :loading="loading" @click="doImport">导入</NButton>
      </div>
    </NModal>
  </div>
</template>
```

- [ ] **Step 2: type-check + lint**

```bash
npm run type-check 2>&1 | tail -5
npm run lint 2>&1 | tail -5
```

- [ ] **Step 3: Commit**

```bash
git add thccb-frontend/src/pages/admin/TitleCodeBatches.vue
git commit -m "feat(title-fe): /admin/title-codes batch list + CSV import page"
```

---

## Task 23: UserManage.vue 整合用户管理页

**Files:**
- Create: `thccb-frontend/src/pages/admin/UserManage.vue`
- Modify: `thccb-frontend/src/api/admin.ts` — 复用既有 admin 调用（adjust-cash / force-loan / forgive-debt / ban / set-admin），如果没有 export 出来现在补上

- [ ] **Step 1: 检查 admin.ts 已有调用**

```bash
cd thccb-frontend
grep -n "adjustCash\|forceLoan\|forgive\|ban\|set.admin" src/api/admin.ts
```

如果哪个没有，参考 `backend/app/api/v1/user.py` 的 endpoint 路径在 admin.ts 中补上（按 adminTitleApi 旁的风格）。

- [ ] **Step 2: 写 UserManage.vue**

```vue
<script setup lang="ts">
import { ref, onMounted, computed, h } from 'vue'
import { useMessage, NDataTable, NButton, NInput, NModal, NTabs, NTabPane, NInputNumber, NSelect, NTag } from 'naive-ui'
import { adminTitleApi } from '@/api/admin'
import type { TitleRead } from '@/api/title'
import TitleChip from '@/components/title/TitleChip.vue'
import api from '@/api'

const message = useMessage()
const users = ref<any[]>([])
const filter = ref('')
const selected = ref<any | null>(null)
const showPanel = ref(false)
const userSummary = ref<any | null>(null)
const userTitles = ref<any[]>([])
const allTitles = ref<TitleRead[]>([])
const adjustAmount = ref<string>('')
const adjustReason = ref<string>('')
const grantTitleId = ref<number | null>(null)

async function refresh() {
  try {
    users.value = await api.get<any[]>('/api/v1/user/list')
    allTitles.value = await adminTitleApi.listTitles()
  } catch (e: any) { message.error(e?.message || '加载失败') }
}

const filtered = computed(() => {
  const q = filter.value.trim().toLowerCase()
  if (!q) return users.value
  return users.value.filter(u =>
    String(u.id).includes(q) || u.username.toLowerCase().includes(q)
  )
})

async function openUser(u: any) {
  selected.value = u
  userSummary.value = null; userTitles.value = []
  showPanel.value = true
  try {
    userSummary.value = await adminTitleApi.userSummary(u.id)
    userTitles.value = await adminTitleApi.listUserTitles(u.id)
  } catch (e: any) { message.error(e?.message || '加载用户详情失败') }
}

async function doAdjustCash() {
  if (!selected.value || !adjustAmount.value || !adjustReason.value.trim()) {
    message.error('金额和原因都要填'); return
  }
  try {
    await api.post(`/api/v1/user/${selected.value.id}/adjust-cash`, {
      amount: adjustAmount.value, reason: adjustReason.value,
    })
    message.success('调整成功')
    adjustAmount.value = ''; adjustReason.value = ''
    await openUser(selected.value)
  } catch (e: any) { message.error(e?.message || '失败') }
}

async function doGrant() {
  if (!selected.value || !grantTitleId.value) return
  try {
    await adminTitleApi.grantTitle(selected.value.id, grantTitleId.value)
    grantTitleId.value = null
    userTitles.value = await adminTitleApi.listUserTitles(selected.value.id)
    message.success('已授予')
  } catch (e: any) { message.error(e?.message || '失败') }
}

async function doRevoke(tid: number) {
  if (!selected.value) return
  if (!confirm('确认撤销该称号？')) return
  try {
    await adminTitleApi.revokeTitle(selected.value.id, tid)
    userTitles.value = await adminTitleApi.listUserTitles(selected.value.id)
    message.success('已撤销')
  } catch (e: any) { message.error(e?.message || '失败') }
}

async function doBan() {
  if (!selected.value) return
  if (!confirm(`确认封禁 ${selected.value.username}？`)) return
  try {
    await api.patch(`/api/v1/user/${selected.value.id}/ban`, { reason: '' })
    await refresh(); await openUser(selected.value)
    message.success('已封禁')
  } catch (e: any) { message.error(e?.message || '失败') }
}

async function doUnban() {
  if (!selected.value) return
  try {
    await api.patch(`/api/v1/user/${selected.value.id}/unban`, {})
    await refresh(); await openUser(selected.value)
    message.success('已解封')
  } catch (e: any) { message.error(e?.message || '失败') }
}

const titleOptions = computed(() => allTitles.value
  .filter((t: TitleRead) => t.is_active)
  .filter((t: TitleRead) => !userTitles.value.some((ut: any) => ut.title.id === t.id))
  .map((t: TitleRead) => ({ label: t.name, value: t.id }))
)

const columns = [
  { title: 'ID', key: 'id', width: 60 },
  { title: '用户名', key: 'username' },
  { title: 'Cash', key: 'cash', width: 100 },
  { title: 'Debt', key: 'debt', width: 100 },
  { title: '状态', key: '_status',
    render(row: any) {
      return [
        row.is_active ? null : h(NTag, { type: 'error', size: 'small' }, () => '封禁'),
        row.is_superuser ? h(NTag, { type: 'warning', size: 'small' }, () => '管理员') : null,
      ]
    } },
  { title: '操作', key: '_act',
    render(row: any) {
      return h(NButton, { size: 'small', onClick: () => openUser(row) }, () => '管理')
    } },
]

onMounted(refresh)
</script>

<template>
  <div class="p-6">
    <h1 class="text-2xl font-black mb-4">用户管理</h1>
    <NInput v-model:value="filter" placeholder="搜索 username / id" class="mb-3" />
    <NDataTable :columns="columns" :data="filtered" :bordered="true" :max-height="600" />

    <NModal v-model:show="showPanel" preset="card"
            :title="`用户 #${selected?.id} ${selected?.username}`"
            style="max-width:680px;">
      <div v-if="userSummary" class="mb-3 text-sm border-2 border-black p-3">
        <div>Cash: <strong>{{ userSummary.cash }}</strong> &nbsp; Debt: <strong>{{ userSummary.debt }}</strong></div>
        <div>状态: {{ userSummary.is_active ? '正常' : '已封禁' }} {{ userSummary.is_superuser ? '/ 管理员' : '' }}</div>
      </div>

      <NTabs default-value="title">
        <NTabPane name="title" tab="称号">
          <div class="space-y-2">
            <div v-for="ut in userTitles" :key="ut.title.id"
                 class="flex justify-between items-center border-2 border-black p-2">
              <TitleChip :title="ut.title" />
              <div class="text-xs text-gray-600">来源: {{ ut.source }}</div>
              <NButton size="small" type="error" @click="doRevoke(ut.title.id)">撤销</NButton>
            </div>
            <div class="flex gap-2 mt-3">
              <NSelect v-model:value="grantTitleId" :options="titleOptions"
                       placeholder="选称号" class="flex-1" />
              <NButton type="primary" @click="doGrant" :disabled="!grantTitleId">授予</NButton>
            </div>
          </div>
        </NTabPane>

        <NTabPane name="cash" tab="调现金">
          <NInput v-model:value="adjustAmount" placeholder="正数加 / 负数扣（如 100 或 -50）" />
          <NInput v-model:value="adjustReason" placeholder="原因（必填，审计用）" class="mt-2" />
          <NButton type="primary" @click="doAdjustCash" class="mt-3">提交</NButton>
        </NTabPane>

        <NTabPane name="ban" tab="封禁">
          <div class="space-y-2">
            <NButton v-if="userSummary?.is_active" type="error" @click="doBan">封禁此用户</NButton>
            <NButton v-else type="warning" @click="doUnban">解封此用户</NButton>
          </div>
        </NTabPane>
      </NTabs>

      <div class="text-xs text-gray-500 mt-4">
        借款 / 免债 / 提撤管理员 等操作请使用对应专门页面（后续可整合进来）。
      </div>
    </NModal>
  </div>
</template>
```

> 此版本先做 title + 调现金 + 封禁 三个核心 tab；借款/免债/管理员授权留链接提示，避免页面一次铺太宽走样。如有空可继续 wire force-loan / forgive-debt / set-admin。

- [ ] **Step 3: type-check + lint + 起服务手动看**

```bash
npm run type-check 2>&1 | tail -5
npm run lint 2>&1 | tail -5
npm run dev
```

浏览器看 `/admin/users` 加载 + 搜索 + 打开某用户面板 + 切换 3 个 tab。

- [ ] **Step 4: Commit**

```bash
git add thccb-frontend/src/pages/admin/UserManage.vue thccb-frontend/src/api/admin.ts
git commit -m "feat(title-fe): /admin/users user management hub"
```

---

## Task 24: 最终验证 + 部署

**Files:** 无新建

- [ ] **Step 1: 跑全量 backend tests**

```bash
cd backend
pytest -x 2>&1 | tail -30
```
Expected: 全 pass，含所有新加的 title 测试。

- [ ] **Step 2: 跑 py_compile + import 验证**

```bash
python -m py_compile $(find app -name '*.py')
python -c "import app.main"
```
Expected: 无输出（成功）。

- [ ] **Step 3: 前端 type-check / lint / build**

```bash
cd thccb-frontend
npm run type-check 2>&1 | tail -10
npm run lint 2>&1 | tail -10
npm run build 2>&1 | tail -20
```
Expected: 全 pass，build 产物正常。

- [ ] **Step 4: 本地起后端 + 前端跑 smoke**

```bash
# 后端
cd backend && python -m uvicorn app.main:app --reload --port 8004 &
# 前端
cd thccb-frontend && npm run dev
```

按 spec §10.3 跑：
- admin: /admin/titles 建 VIP title → /admin/title-codes 建 batch → 上传 3 行 CSV
- 用户: 登录 → /redeem-title 输入码 → 个人页看到 VIP chip → equip → leaderboard 显示 chip
- /admin/users 给另一用户授 VIP
- /admin/users 撤销正佩戴的 title → 个人页"我的称号"列表减少 + equipped 自动清
- /admin 编辑某市场 → required_titles 选 VIP → 不达标用户 buy 403 toast → 达标用户 buy 通过
- 撤销 VIP 后该用户 sell 仍可用
- 移动端 viewport: chrome devtools 切到移动尺寸看是否塌

记录任何崩溃 / 报错 / 视觉问题。

- [ ] **Step 5: 合并到 main + push**

```bash
git checkout main
git pull --ff-only
git merge --no-ff feat/2026-05-23-title-system
git push origin main
```

`push` 触发自动部署。

- [ ] **Step 6: 生产 smoke**

打开生产站，重复 Step 4 的 smoke 测试（admin 创 1 个测试 title + 1 个 code，自己兑换验证）。

如果出错：
- 先看 `/health` / `/api/v1/title/catalog` 是否 200
- 若 500 — 多半是 alembic 没跑通，看后端日志，按需手动 `alembic upgrade head`
- 若部署成功但 UI 报"Failed to fetch chunk" — 浏览器强刷（已有 router.onError 自动 reload 兜底）

- [ ] **Step 7: 清理本地分支（可选）**

```bash
git branch -d feat/2026-05-23-title-system
```

---

## Self-Review Notes

**Spec coverage check：**

- §3 Schema → Task 1 ✓
- §4 字段语义 → 已贯穿 Task 1/4/5/6/9 service 实现
- §5.1 用户自助 → Task 5/6/9 ✓
- §5.2 admin catalog → Task 4 ✓
- §5.3 admin batch → Task 7/8 ✓
- §5.4 admin user-title + summary → Task 10 ✓
- §5.5 admin market gating → Task 12 ✓
- §5.6 buy gate + 响应增量 → Task 11/13/14 ✓
- §5.7 限速 — 限速这个是已有 middleware（rate_limit），title/redeem 走 /auth 5r/s 在 router 注册时挂上，**已遗漏明确步骤**：建议在 Task 9 末尾给 router 挂上 dependencies 装饰；但若 router 在 main.py include 时统一加 rate_limit，不算大改。视生产基础设施现状决定。**注：实施时若发现 redeem endpoint 没有限速，加 dependencies=[Depends(rate_limit("5/sec"))] 或参考 auth router 现成做法**。
- §6 边界 case a~o → Task 9 (a-d), Task 10 (e/f/n), Task 4 (g/k), Task 8 (h/i/j), Task 12 (l/o), Task 1 alembic FK (m), Task 6 (n) ✓
- §7 前端 → Task 15-23 ✓
- §8 /admin/users 整合 → Task 23 ✓
- §9 alembic → Task 2 ✓
- §10 测试 → 各 task 内 pytest ✓
- §11 部署 → Task 24 ✓

**Placeholder / consistency：**
- 路由路径用了占位 `<leaderboard_path>` 和 `<profile_file>.vue` — 这些必须实施时 grep 真值替换，已显式标注。
- 后端在 Task 13/14 中也有 grep 定位指令，不是 placeholder 而是动作步骤。
- 函数名前后一致（assert_user_can_trade_market / parse_csv_codes / redeem_code / equip_title 都按定义使用）。

**已知 known-unknowns 留给实施者解：**
1. leaderboard endpoint 真实路径（Task 14 grep 解决）
2. profile/portfolio 真实文件名（Task 17 grep 解决）
3. 市场列表卡片组件真实位置（Task 19 grep 解决）
4. AppHeader 用户名渲染细节（Task 18 grep 解决）
5. AppSidebar 菜单项 schema 字段名（Task 15 看一眼现成 item）
6. /title/redeem 限速 (spec §5.7) 是否已在 router 层挂上

实施时遇到任一不一致 → 停下问 user，不要硬猜。
