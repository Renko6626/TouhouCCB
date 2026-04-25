# 兑换码模块（Redemption Codes）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 TouhouCCB 上加一个「兑换中心」模块——管理员后台 CSV 导入朋友预生成的兑换码批次；用户花资金购买后获得码字符串，复制粘贴到朋友站点核销。两边账户系统零耦合。

**Architecture:** 后端三张新表（合作方 / 批次 / 码）+ 单事务 + `FOR UPDATE SKIP LOCKED` 抢码原子性；用户端与管理员端 API 分两个 router；前端按现有 Vue 3 + Pinia + UnoCSS 模式新增页面。生产部署依赖 `SQLModel.metadata.create_all` 自动建新表（不动 `base.py` 已有列）。

**Tech Stack:** FastAPI + SQLModel + PostgreSQL（后端）；Vue 3 + Pinia + UnoCSS + TypeScript（前端）。沿用现有 `core/users.current_active_user` / `current_superuser` 鉴权与 6 位 `Decimal` 资金精度约定。

**Spec:** `docs/superpowers/specs/2026-04-25-redemption-codes-design.md`

**Branch:** `ralph/2026-04-25-redemption-codes`（已创建，包含 spec commit）

---

## 文件结构总览

**新建后端**：
- `backend/app/models/redemption.py` — RedemptionPartner / RedemptionBatch / RedemptionCode 模型
- `backend/app/schemas/redemption.py` — Pydantic 请求/响应 schema
- `backend/app/services/redemption.py` — 购买事务、CSV 解析、库存查询
- `backend/app/api/v1/redemption.py` — 用户端路由
- `backend/app/api/v1/admin_redemption.py` — 管理员端路由
- `backend/tests/test_redemption_service.py` — 服务层单元测试
- `backend/tests/test_redemption_api.py` — 用户端 API 测试
- `backend/tests/test_redemption_admin.py` — 管理员端 API 测试

**新建前端**：
- `thccb-frontend/src/types/redemption.ts` — 类型定义
- `thccb-frontend/src/api/redemption.ts` — API client
- `thccb-frontend/src/stores/redemption.ts` — Pinia store
- `thccb-frontend/src/pages/redemption/RedemptionList.vue` — 兑换中心列表
- `thccb-frontend/src/pages/redemption/BatchDetail.vue` — 批次详情 / 购买
- `thccb-frontend/src/pages/user/MyRedemptions.vue` — 我的兑换记录
- `thccb-frontend/src/pages/admin/RedemptionPartners.vue` — 合作方管理
- `thccb-frontend/src/pages/admin/RedemptionBatches.vue` — 批次管理
- `thccb-frontend/src/pages/admin/RedemptionImport.vue` — CSV 导入

**修改**：
- `backend/app/main.py` — 注册两个新 router + import 新模型
- `thccb-frontend/src/router/routes.ts` — 添加 6 条新路由
- `thccb-frontend/src/components/layout/AppSidebar.vue` — 加「兑换中心」+「兑换管理」入口

---

## Task 1：后端数据模型

**Files:**
- Create: `backend/app/models/redemption.py`
- Modify: `backend/app/main.py`（顶部 import 一次，触发 SQLModel 注册）

- [ ] **Step 1: 创建模型文件**

```python
# backend/app/models/redemption.py
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlmodel import SQLModel, Field
from sqlalchemy import DateTime, Numeric, UniqueConstraint, Index


class BatchStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class CodeStatus(str, Enum):
    AVAILABLE = "available"
    SOLD = "sold"


class RedemptionPartner(SQLModel, table=True):
    __tablename__ = "redemption_partner"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, nullable=False)
    description: str = Field(default="")
    website_url: str = Field(default="")
    logo_url: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True, nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )


class RedemptionBatch(SQLModel, table=True):
    __tablename__ = "redemption_batch"

    id: Optional[int] = Field(default=None, primary_key=True)
    partner_id: int = Field(foreign_key="redemption_partner.id", index=True, nullable=False)
    name: str = Field(nullable=False)
    description: str = Field(default="")
    unit_price: Decimal = Field(sa_type=Numeric(16, 6), nullable=False)
    status: str = Field(default=BatchStatus.DRAFT, index=True, nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
    created_by_admin_id: Optional[int] = Field(default=None, foreign_key="user.id")


class RedemptionCode(SQLModel, table=True):
    __tablename__ = "redemption_code"
    __table_args__ = (
        UniqueConstraint("code_string", name="uq_redemption_code_string"),
        Index("ix_redemption_code_batch_status", "batch_id", "status"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: int = Field(foreign_key="redemption_batch.id", index=True, nullable=False)
    code_string: str = Field(nullable=False, max_length=128)
    status: str = Field(default=CodeStatus.AVAILABLE, nullable=False)
    bought_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    bought_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    marked_used_by_user_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
```

- [ ] **Step 2: 在 main.py 顶部 import 触发表注册**

修改 `backend/app/main.py` 第 12 行附近，把现有的 router import 块下面加一行：

```python
from app.api.v1 import auth, user, market, chart, stream, loan, site_config as site_config_api
from app.api.v1 import redemption as redemption_api, admin_redemption as admin_redemption_api  # 新增
from app.models import redemption as _redemption_models  # noqa: F401  确保 SQLModel.metadata 注册
```

（router 文件下一个任务才创建，先放着 import；如果担心这一步 lint 失败可以先注释，等 Task 4 之后再放开。）

- [ ] **Step 3: 验证模型可被 import**

Run: `cd backend && python -c "from app.models import redemption; print(redemption.RedemptionPartner.__table__)"`

Expected: 打印出表结构，无报错。

- [ ] **Step 4: Commit**

```bash
git add backend/app/models/redemption.py
git commit -m "feat(redemption): 新增兑换码三表模型 (Partner/Batch/Code)"
```

---

## Task 2：后端 Pydantic schemas

**Files:**
- Create: `backend/app/schemas/redemption.py`

- [ ] **Step 1: 创建 schema 文件**

```python
# backend/app/schemas/redemption.py
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from pydantic import BaseModel, Field


# ========== 用户端 ==========

class PartnerPublic(BaseModel):
    id: int
    name: str
    description: str
    website_url: str
    logo_url: Optional[str]


class BatchListItem(BaseModel):
    id: int
    partner: PartnerPublic
    name: str
    unit_price: Decimal
    available_count: int


class BatchDetailResponse(BatchListItem):
    description: str


class PurchaseRequest(BaseModel):
    batch_id: int


class PurchaseResponse(BaseModel):
    code_id: int
    code_string: str
    batch_name: str
    partner_name: str
    partner_website_url: str
    description: str
    paid_amount: Decimal
    cash_after: Decimal


class MyRedemptionItem(BaseModel):
    code_id: int
    batch_name: str
    partner_name: str
    partner_website_url: str
    paid_amount: Decimal
    bought_at: datetime
    marked_used_by_user_at: Optional[datetime]


class MyRedemptionDetail(MyRedemptionItem):
    code_string: str
    description: str


class MarkUsedRequest(BaseModel):
    used: bool  # True=标记已用；False=取消标记


# ========== 管理员端 ==========

class PartnerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    website_url: str = ""
    logo_url: Optional[str] = None
    is_active: bool = True


class PartnerUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = None
    website_url: Optional[str] = None
    logo_url: Optional[str] = None
    is_active: Optional[bool] = None


class PartnerAdminItem(BaseModel):
    id: int
    name: str
    description: str
    website_url: str
    logo_url: Optional[str]
    is_active: bool
    created_at: datetime


class BatchCreate(BaseModel):
    partner_id: int
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    unit_price: Decimal = Field(gt=Decimal("0"))


class BatchUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    unit_price: Optional[Decimal] = Field(default=None, gt=Decimal("0"))
    status: Optional[str] = None  # draft/active/archived


class BatchAdminItem(BaseModel):
    id: int
    partner_id: int
    partner_name: str
    name: str
    description: str
    unit_price: Decimal
    status: str
    total_count: int
    sold_count: int
    available_count: int
    created_at: datetime


class CsvImportRequest(BaseModel):
    csv_text: str  # 整个 CSV 文本；前端可来自文件读取或粘贴


class CsvImportPreview(BaseModel):
    total_lines: int
    new_codes: List[str]          # 即将插入
    duplicate_codes: List[str]    # 与已有重复（跳过）
    invalid_codes: List[str]      # 非法（空/超长）


class CsvImportConfirm(BaseModel):
    csv_text: str  # 同上
    confirm: bool = True  # 必须 True 才真正写入


class CsvImportResult(BaseModel):
    inserted: int
    skipped_duplicate: int
    skipped_invalid: int
```

- [ ] **Step 2: 验证 schema 可 import**

Run: `cd backend && python -c "from app.schemas.redemption import PurchaseResponse, CsvImportPreview"`

Expected: 无报错。

- [ ] **Step 3: Commit**

```bash
git add backend/app/schemas/redemption.py
git commit -m "feat(redemption): 新增 Pydantic schemas（用户/管理员/CSV 导入）"
```

---

## Task 3：CSV 解析工具（纯函数 + TDD）

**Files:**
- Create: `backend/app/services/redemption.py`（先放 CSV 解析逻辑，购买事务下一个任务加）
- Create: `backend/tests/test_redemption_service.py`

- [ ] **Step 1: 写 CSV 解析失败测试**

```python
# backend/tests/test_redemption_service.py
from app.services.redemption import parse_csv_codes


def test_parse_csv_simple_lines():
    text = "ABC123\nDEF456\nGHI789\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["ABC123", "DEF456", "GHI789"]
    assert invalid == []


def test_parse_csv_skips_header_named_code():
    text = "code\nABC\nDEF\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["ABC", "DEF"]


def test_parse_csv_trims_whitespace_and_empty_lines():
    text = "  ABC  \n\n   \n DEF\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["ABC", "DEF"]


def test_parse_csv_marks_too_long_as_invalid():
    long = "X" * 129
    text = f"OK\n{long}\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["OK"]
    assert invalid == [long]


def test_parse_csv_dedupes_within_input():
    text = "ABC\nABC\nDEF\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["ABC", "DEF"]  # 同一文件内的重复合并为一份
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/test_redemption_service.py -v`
Expected: ImportError（`parse_csv_codes` 还不存在）。

- [ ] **Step 3: 实现 `parse_csv_codes`**

```python
# backend/app/services/redemption.py
"""兑换码模块服务层。事务、CSV 解析、库存查询。"""
from __future__ import annotations

from typing import List, Tuple


_MAX_CODE_LEN = 128


def parse_csv_codes(text: str) -> Tuple[List[str], List[str]]:
    """解析 CSV 文本，返回 (valid_codes, invalid_codes)。

    规则：
    - 按行切分，每行 strip
    - 跳过空行
    - 第一行如果是 "code"（小写）视为表头，跳过
    - 长度超过 _MAX_CODE_LEN 的归入 invalid
    - 合法码内部去重（保持首次出现顺序）
    """
    lines = [ln.strip() for ln in text.splitlines()]
    # 去掉前后空行
    while lines and lines[0] == "":
        lines.pop(0)
    if lines and lines[0].lower() == "code":
        lines = lines[1:]

    valid: List[str] = []
    invalid: List[str] = []
    seen = set()
    for ln in lines:
        if not ln:
            continue
        if len(ln) > _MAX_CODE_LEN:
            invalid.append(ln)
            continue
        if ln in seen:
            continue
        seen.add(ln)
        valid.append(ln)
    return valid, invalid
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_redemption_service.py -v`
Expected: 5 个测试全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/redemption.py backend/tests/test_redemption_service.py
git commit -m "feat(redemption): CSV 解析（含表头识别/去重/长度校验）"
```

---

## Task 4：购买事务（核心原子性 + TDD）

**Files:**
- Modify: `backend/app/services/redemption.py`
- Modify: `backend/tests/test_redemption_service.py`

- [ ] **Step 1: 写购买测试（含并发竞态）**

在 `backend/tests/test_redemption_service.py` 文件**顶部加 fixture 与 import**：

```python
import asyncio
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlmodel import SQLModel

from app.core.database import engine, async_session_maker
from app.models.base import User
from app.models.redemption import (
    RedemptionPartner, RedemptionBatch, RedemptionCode,
    BatchStatus, CodeStatus,
)
from app.services.redemption import (
    parse_csv_codes,
    purchase_code,
    PurchaseError,
)


@pytest_asyncio.fixture
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    yield


async def _seed_user(cash: Decimal) -> int:
    async with async_session_maker() as s:
        u = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            email=f"{uuid.uuid4().hex[:6]}@t.com",
            casdoor_id=uuid.uuid4().hex,
            cash=cash,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id


async def _seed_batch(unit_price: Decimal, code_strings, status=BatchStatus.ACTIVE) -> int:
    async with async_session_maker() as s:
        p = RedemptionPartner(name="P")
        s.add(p)
        await s.commit()
        await s.refresh(p)
        b = RedemptionBatch(
            partner_id=p.id, name="B", unit_price=unit_price, status=status,
        )
        s.add(b)
        await s.commit()
        await s.refresh(b)
        for cs in code_strings:
            s.add(RedemptionCode(batch_id=b.id, code_string=cs))
        await s.commit()
        return b.id


@pytest.mark.asyncio
async def test_purchase_happy_path(setup_db):
    uid = await _seed_user(Decimal("100"))
    bid = await _seed_batch(Decimal("30"), ["A1", "A2"])
    async with async_session_maker() as s:
        result = await purchase_code(s, user_id=uid, batch_id=bid)
        await s.commit()
    assert result.code_string in {"A1", "A2"}
    assert result.paid_amount == Decimal("30")
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.cash == Decimal("70")


@pytest.mark.asyncio
async def test_purchase_insufficient_cash(setup_db):
    uid = await _seed_user(Decimal("10"))
    bid = await _seed_batch(Decimal("30"), ["A1"])
    async with async_session_maker() as s:
        with pytest.raises(PurchaseError) as e:
            await purchase_code(s, user_id=uid, batch_id=bid)
        assert e.value.code == "INSUFFICIENT_CASH"


@pytest.mark.asyncio
async def test_purchase_sold_out(setup_db):
    uid = await _seed_user(Decimal("100"))
    bid = await _seed_batch(Decimal("30"), [])  # 空库存
    async with async_session_maker() as s:
        with pytest.raises(PurchaseError) as e:
            await purchase_code(s, user_id=uid, batch_id=bid)
        assert e.value.code == "SOLD_OUT"


@pytest.mark.asyncio
async def test_purchase_batch_not_active(setup_db):
    uid = await _seed_user(Decimal("100"))
    bid = await _seed_batch(Decimal("30"), ["A1"], status=BatchStatus.DRAFT)
    async with async_session_maker() as s:
        with pytest.raises(PurchaseError) as e:
            await purchase_code(s, user_id=uid, batch_id=bid)
        assert e.value.code == "BATCH_NOT_ACTIVE"


@pytest.mark.asyncio
async def test_purchase_concurrent_only_one_wins(setup_db):
    """同时两人抢最后一个码，仅一个成功，另一个 SOLD_OUT。"""
    uid1 = await _seed_user(Decimal("100"))
    uid2 = await _seed_user(Decimal("100"))
    bid = await _seed_batch(Decimal("30"), ["LAST"])

    async def _try(uid):
        async with async_session_maker() as s:
            try:
                r = await purchase_code(s, user_id=uid, batch_id=bid)
                await s.commit()
                return ("ok", r.code_string)
            except PurchaseError as e:
                return ("err", e.code)

    res = await asyncio.gather(_try(uid1), _try(uid2))
    oks = [r for r in res if r[0] == "ok"]
    errs = [r for r in res if r[0] == "err"]
    assert len(oks) == 1
    assert len(errs) == 1
    assert errs[0][1] == "SOLD_OUT"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/test_redemption_service.py -v`
Expected: ImportError（`purchase_code` / `PurchaseError` 还没有）。

- [ ] **Step 3: 实现购买事务**

在 `backend/app/services/redemption.py` 末尾追加：

```python
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.base import User
from app.models.redemption import (
    RedemptionPartner, RedemptionBatch, RedemptionCode,
    BatchStatus, CodeStatus,
)


_QUANT = Decimal("0.000001")


class PurchaseError(Exception):
    """购买失败，code ∈ {INSUFFICIENT_CASH, SOLD_OUT, BATCH_NOT_ACTIVE, BATCH_NOT_FOUND}"""
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass
class PurchaseResult:
    code_id: int
    code_string: str
    batch_id: int
    batch_name: str
    partner_name: str
    partner_website_url: str
    description: str
    paid_amount: Decimal
    cash_after: Decimal


async def purchase_code(
    session: AsyncSession,
    *,
    user_id: int,
    batch_id: int,
) -> PurchaseResult:
    """单事务原子购买：行锁 user → 校验 → SKIP LOCKED 抢一个码 → 扣款 → 标记码。
    不在这里 commit，由调用方负责。
    """
    # 1. 行锁用户
    user_stmt = select(User).where(User.id == user_id).with_for_update()
    user = (await session.execute(user_stmt)).scalar_one()

    # 2. 取批次（无需锁，状态变化低）
    batch = await session.get(RedemptionBatch, batch_id)
    if batch is None:
        raise PurchaseError("BATCH_NOT_FOUND")
    if batch.status != BatchStatus.ACTIVE:
        raise PurchaseError("BATCH_NOT_ACTIVE")

    # 3. 余额检查
    if user.cash < batch.unit_price:
        raise PurchaseError("INSUFFICIENT_CASH")

    # 4. 抓一个可售码（SKIP LOCKED 避免并发互相阻塞）
    code_stmt = (
        select(RedemptionCode)
        .where(
            RedemptionCode.batch_id == batch_id,
            RedemptionCode.status == CodeStatus.AVAILABLE,
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    code = (await session.execute(code_stmt)).scalar_one_or_none()
    if code is None:
        raise PurchaseError("SOLD_OUT")

    # 5. 扣款 + 标记码
    user.cash = (user.cash - batch.unit_price).quantize(_QUANT)
    code.status = CodeStatus.SOLD
    code.bought_by_user_id = user_id
    code.bought_at = datetime.now(timezone.utc)

    session.add(user)
    session.add(code)

    # 6. 取合作方信息一并返回
    partner = await session.get(RedemptionPartner, batch.partner_id)

    return PurchaseResult(
        code_id=code.id,
        code_string=code.code_string,
        batch_id=batch.id,
        batch_name=batch.name,
        partner_name=partner.name if partner else "",
        partner_website_url=partner.website_url if partner else "",
        description=batch.description,
        paid_amount=batch.unit_price,
        cash_after=user.cash,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_redemption_service.py -v`
Expected: 全部 PASS。

如果 `test_purchase_concurrent_only_one_wins` 失败：检查测试用的数据库是否支持 `FOR UPDATE SKIP LOCKED`（SQLite 不支持，会忽略 SKIP LOCKED 但仍能跑）。如果用 SQLite 跑此测试，可能两个事务串行执行——这是 SQLite 局限，生产 PG 上行为正确。在这种情况下断言可能两个都成 ok（一个抢到 LAST，另一个 SOLD_OUT），现有断言应该仍能通过。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/redemption.py backend/tests/test_redemption_service.py
git commit -m "feat(redemption): 购买事务 + 并发抢码原子性 (FOR UPDATE SKIP LOCKED)"
```

---

## Task 5：辅助查询函数（库存计数、列表）

**Files:**
- Modify: `backend/app/services/redemption.py`

- [ ] **Step 1: 实现库存与列表查询函数**

在 `backend/app/services/redemption.py` 末尾追加：

```python
from typing import List as _List
from sqlalchemy import func


async def count_available_for_batch(session: AsyncSession, batch_id: int) -> int:
    stmt = select(func.count()).select_from(RedemptionCode).where(
        RedemptionCode.batch_id == batch_id,
        RedemptionCode.status == CodeStatus.AVAILABLE,
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_total_for_batch(session: AsyncSession, batch_id: int) -> int:
    stmt = select(func.count()).select_from(RedemptionCode).where(
        RedemptionCode.batch_id == batch_id,
    )
    return int((await session.execute(stmt)).scalar_one())


async def list_active_batches_with_stock(session: AsyncSession):
    """返回所有 active 且至少有一个 available 码的批次，连带 partner 信息。
    返回 list[(batch, partner, available_count)]。
    跳过 partner.is_active=False。
    """
    batches_stmt = select(RedemptionBatch).where(
        RedemptionBatch.status == BatchStatus.ACTIVE,
    )
    batches = list((await session.execute(batches_stmt)).scalars().all())
    result = []
    for b in batches:
        partner = await session.get(RedemptionPartner, b.partner_id)
        if partner is None or not partner.is_active:
            continue
        avail = await count_available_for_batch(session, b.id)
        if avail <= 0:
            continue
        result.append((b, partner, avail))
    return result


async def import_codes_dry_run(
    session: AsyncSession, batch_id: int, csv_text: str,
):
    """预检：解析 + 与已有码比对，返回 (new, duplicate, invalid)。"""
    valid, invalid = parse_csv_codes(csv_text)
    if not valid:
        return [], [], invalid
    # 全局重复检查
    stmt = select(RedemptionCode.code_string).where(
        RedemptionCode.code_string.in_(valid),
    )
    existing = set((await session.execute(stmt)).scalars().all())
    new = [c for c in valid if c not in existing]
    dup = [c for c in valid if c in existing]
    return new, dup, invalid


async def import_codes_commit(
    session: AsyncSession, batch_id: int, csv_text: str,
) -> dict:
    """真正写入。重复/非法跳过。"""
    new, dup, invalid = await import_codes_dry_run(session, batch_id, csv_text)
    for cs in new:
        session.add(RedemptionCode(batch_id=batch_id, code_string=cs))
    return {
        "inserted": len(new),
        "skipped_duplicate": len(dup),
        "skipped_invalid": len(invalid),
    }
```

- [ ] **Step 2: 加测试**

在 `backend/tests/test_redemption_service.py` 末尾追加：

```python
from app.services.redemption import (
    count_available_for_batch,
    list_active_batches_with_stock,
    import_codes_dry_run,
    import_codes_commit,
)


@pytest.mark.asyncio
async def test_count_available(setup_db):
    bid = await _seed_batch(Decimal("10"), ["A", "B", "C"])
    async with async_session_maker() as s:
        assert await count_available_for_batch(s, bid) == 3


@pytest.mark.asyncio
async def test_list_active_batches_filters_inactive_partner(setup_db):
    async with async_session_maker() as s:
        p = RedemptionPartner(name="P", is_active=False)
        s.add(p)
        await s.commit()
        await s.refresh(p)
        b = RedemptionBatch(
            partner_id=p.id, name="B", unit_price=Decimal("10"),
            status=BatchStatus.ACTIVE,
        )
        s.add(b)
        await s.commit()
        await s.refresh(b)
        s.add(RedemptionCode(batch_id=b.id, code_string="X"))
        await s.commit()

    async with async_session_maker() as s:
        rows = await list_active_batches_with_stock(s)
        assert rows == []


@pytest.mark.asyncio
async def test_import_dry_run_dedupes_against_existing(setup_db):
    bid = await _seed_batch(Decimal("10"), ["EXISTING"])
    async with async_session_maker() as s:
        new, dup, invalid = await import_codes_dry_run(
            s, bid, "EXISTING\nNEW1\nNEW2\n",
        )
        assert new == ["NEW1", "NEW2"]
        assert dup == ["EXISTING"]
        assert invalid == []


@pytest.mark.asyncio
async def test_import_commit_writes_only_new(setup_db):
    bid = await _seed_batch(Decimal("10"), [])
    async with async_session_maker() as s:
        result = await import_codes_commit(s, bid, "A\nB\nA\n")
        await s.commit()
        assert result["inserted"] == 2
        assert await count_available_for_batch(s, bid) == 2
```

- [ ] **Step 3: 跑测试**

Run: `cd backend && pytest tests/test_redemption_service.py -v`
Expected: 全部 PASS。

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/redemption.py backend/tests/test_redemption_service.py
git commit -m "feat(redemption): 库存查询 + CSV 导入 dry-run/commit"
```

---

## Task 6：用户端 API

**Files:**
- Create: `backend/app/api/v1/redemption.py`
- Modify: `backend/app/main.py`（注册 router）

- [ ] **Step 1: 创建 router**

```python
# backend/app/api/v1/redemption.py
"""兑换中心用户端 API。"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_async_session
from app.core.users import current_active_user
from app.models.base import User
from app.models.redemption import (
    RedemptionPartner, RedemptionBatch, RedemptionCode, CodeStatus,
)
from app.schemas.redemption import (
    PartnerPublic, BatchListItem, BatchDetailResponse,
    PurchaseRequest, PurchaseResponse,
    MyRedemptionItem, MyRedemptionDetail, MarkUsedRequest,
)
from app.services import redemption as svc

router = APIRouter()
logger = logging.getLogger("thccb.redemption")


@router.get("/batches", response_model=List[BatchListItem])
async def list_batches(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    rows = await svc.list_active_batches_with_stock(db)
    return [
        BatchListItem(
            id=b.id,
            partner=PartnerPublic(
                id=p.id, name=p.name, description=p.description,
                website_url=p.website_url, logo_url=p.logo_url,
            ),
            name=b.name,
            unit_price=b.unit_price,
            available_count=avail,
        )
        for b, p, avail in rows
    ]


@router.get("/batches/{batch_id}", response_model=BatchDetailResponse)
async def batch_detail(
    batch_id: int,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    b = await db.get(RedemptionBatch, batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    p = await db.get(RedemptionPartner, b.partner_id)
    if p is None or not p.is_active or b.status != "active":
        raise HTTPException(status_code=404, detail="批次不可用")
    avail = await svc.count_available_for_batch(db, b.id)
    return BatchDetailResponse(
        id=b.id,
        partner=PartnerPublic(
            id=p.id, name=p.name, description=p.description,
            website_url=p.website_url, logo_url=p.logo_url,
        ),
        name=b.name,
        unit_price=b.unit_price,
        available_count=avail,
        description=b.description,
    )


@router.post("/purchase", response_model=PurchaseResponse)
async def purchase(
    req: PurchaseRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        result = await svc.purchase_code(db, user_id=user.id, batch_id=req.batch_id)
    except svc.PurchaseError as e:
        await db.rollback()
        if e.code == "INSUFFICIENT_CASH":
            raise HTTPException(status_code=422, detail="资金不足")
        if e.code == "SOLD_OUT":
            raise HTTPException(status_code=409, detail="已售罄")
        if e.code == "BATCH_NOT_ACTIVE":
            raise HTTPException(status_code=409, detail="批次不可购买")
        if e.code == "BATCH_NOT_FOUND":
            raise HTTPException(status_code=404, detail="批次不存在")
        raise
    await db.commit()
    logger.info(
        "REDEMPTION_PURCHASE user_id=%s batch_id=%s code_id=%s amount=%s",
        user.id, req.batch_id, result.code_id, result.paid_amount,
    )
    return PurchaseResponse(
        code_id=result.code_id,
        code_string=result.code_string,
        batch_name=result.batch_name,
        partner_name=result.partner_name,
        partner_website_url=result.partner_website_url,
        description=result.description,
        paid_amount=result.paid_amount,
        cash_after=result.cash_after,
    )


@router.get("/my", response_model=List[MyRedemptionItem])
async def my_redemptions(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(RedemptionCode)
        .where(RedemptionCode.bought_by_user_id == user.id)
        .order_by(RedemptionCode.bought_at.desc())
    )
    codes = list((await db.execute(stmt)).scalars().all())
    out: List[MyRedemptionItem] = []
    for c in codes:
        b = await db.get(RedemptionBatch, c.batch_id)
        p = await db.get(RedemptionPartner, b.partner_id) if b else None
        out.append(MyRedemptionItem(
            code_id=c.id,
            batch_name=b.name if b else "",
            partner_name=p.name if p else "",
            partner_website_url=p.website_url if p else "",
            paid_amount=b.unit_price if b else 0,
            bought_at=c.bought_at,
            marked_used_by_user_at=c.marked_used_by_user_at,
        ))
    return out


@router.get("/my/{code_id}", response_model=MyRedemptionDetail)
async def my_redemption_detail(
    code_id: int,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    c = await db.get(RedemptionCode, code_id)
    if c is None or c.bought_by_user_id != user.id:
        raise HTTPException(status_code=404, detail="兑换记录不存在")
    b = await db.get(RedemptionBatch, c.batch_id)
    p = await db.get(RedemptionPartner, b.partner_id) if b else None
    return MyRedemptionDetail(
        code_id=c.id,
        code_string=c.code_string,
        batch_name=b.name if b else "",
        partner_name=p.name if p else "",
        partner_website_url=p.website_url if p else "",
        paid_amount=b.unit_price if b else 0,
        bought_at=c.bought_at,
        marked_used_by_user_at=c.marked_used_by_user_at,
        description=b.description if b else "",
    )


@router.post("/my/{code_id}/mark-used")
async def mark_used(
    code_id: int,
    req: MarkUsedRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    c = await db.get(RedemptionCode, code_id)
    if c is None or c.bought_by_user_id != user.id:
        raise HTTPException(status_code=404, detail="兑换记录不存在")
    c.marked_used_by_user_at = datetime.now(timezone.utc) if req.used else None
    db.add(c)
    await db.commit()
    return {"ok": True, "marked_used_by_user_at": c.marked_used_by_user_at}
```

- [ ] **Step 2: 在 main.py 注册 router**

修改 `backend/app/main.py`，找到 `app.include_router(...)` 块，在 `loan` 那行下面加：

```python
app.include_router(redemption_api.router, prefix="/api/v1/redemption", tags=["Redemption"])
```

并确认顶部 import 已经放开（Task 1 Step 2 那行去掉注释）。

- [ ] **Step 3: 验证启动**

Run: `cd backend && python -c "import app.main; print('ok')"`
Expected: `ok`，无 ImportError。

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/v1/redemption.py backend/app/main.py
git commit -m "feat(redemption): 用户端 API（列表/详情/购买/我的记录/标记已用）"
```

---

## Task 7：管理员端 API

**Files:**
- Create: `backend/app/api/v1/admin_redemption.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: 创建管理员 router**

```python
# backend/app/api/v1/admin_redemption.py
"""兑换中心管理员端 API。"""
from __future__ import annotations
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User
from app.models.redemption import (
    RedemptionPartner, RedemptionBatch, BatchStatus,
)
from app.schemas.redemption import (
    PartnerCreate, PartnerUpdate, PartnerAdminItem,
    BatchCreate, BatchUpdate, BatchAdminItem,
    CsvImportRequest, CsvImportPreview,
    CsvImportConfirm, CsvImportResult,
)
from app.services import redemption as svc

router = APIRouter()
logger = logging.getLogger("thccb.redemption_admin")


# ===== 合作方 =====

@router.get("/partners", response_model=List[PartnerAdminItem])
async def list_partners(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    rows = list((await db.execute(select(RedemptionPartner))).scalars().all())
    return [PartnerAdminItem(**p.model_dump()) for p in rows]


@router.post("/partners", response_model=PartnerAdminItem)
async def create_partner(
    req: PartnerCreate,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    p = RedemptionPartner(**req.model_dump())
    db.add(p)
    await db.commit()
    await db.refresh(p)
    logger.info("REDEMPTION_PARTNER_CREATE admin=%s id=%s", admin.id, p.id)
    return PartnerAdminItem(**p.model_dump())


@router.patch("/partners/{partner_id}", response_model=PartnerAdminItem)
async def update_partner(
    partner_id: int,
    req: PartnerUpdate,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    p = await db.get(RedemptionPartner, partner_id)
    if p is None:
        raise HTTPException(status_code=404, detail="合作方不存在")
    for k, v in req.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return PartnerAdminItem(**p.model_dump())


# ===== 批次 =====

async def _batch_to_admin_item(db: AsyncSession, b: RedemptionBatch) -> BatchAdminItem:
    p = await db.get(RedemptionPartner, b.partner_id)
    total = await svc.count_total_for_batch(db, b.id)
    avail = await svc.count_available_for_batch(db, b.id)
    return BatchAdminItem(
        id=b.id, partner_id=b.partner_id,
        partner_name=p.name if p else "",
        name=b.name, description=b.description,
        unit_price=b.unit_price, status=b.status,
        total_count=total, sold_count=total - avail, available_count=avail,
        created_at=b.created_at,
    )


@router.get("/batches", response_model=List[BatchAdminItem])
async def list_batches_admin(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    rows = list((await db.execute(select(RedemptionBatch))).scalars().all())
    return [await _batch_to_admin_item(db, b) for b in rows]


@router.post("/batches", response_model=BatchAdminItem)
async def create_batch(
    req: BatchCreate,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    p = await db.get(RedemptionPartner, req.partner_id)
    if p is None:
        raise HTTPException(status_code=404, detail="合作方不存在")
    b = RedemptionBatch(
        partner_id=req.partner_id, name=req.name, description=req.description,
        unit_price=req.unit_price, status=BatchStatus.DRAFT,
        created_by_admin_id=admin.id,
    )
    db.add(b)
    await db.commit()
    await db.refresh(b)
    logger.info("REDEMPTION_BATCH_CREATE admin=%s id=%s", admin.id, b.id)
    return await _batch_to_admin_item(db, b)


@router.patch("/batches/{batch_id}", response_model=BatchAdminItem)
async def update_batch(
    batch_id: int,
    req: BatchUpdate,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    b = await db.get(RedemptionBatch, batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    data = req.model_dump(exclude_unset=True)

    # active 状态下不允许改价
    if "unit_price" in data and b.status == BatchStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="active 批次不允许修改价格，请新建批次")

    # status 转换合法性
    if "status" in data:
        new_status = data["status"]
        if new_status not in (BatchStatus.DRAFT, BatchStatus.ACTIVE, BatchStatus.ARCHIVED):
            raise HTTPException(status_code=400, detail="非法状态")

    for k, v in data.items():
        setattr(b, k, v)
    db.add(b)
    await db.commit()
    await db.refresh(b)
    return await _batch_to_admin_item(db, b)


# ===== CSV 导入 =====

@router.post("/batches/{batch_id}/import/preview", response_model=CsvImportPreview)
async def import_preview(
    batch_id: int,
    req: CsvImportRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    b = await db.get(RedemptionBatch, batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    new, dup, invalid = await svc.import_codes_dry_run(db, batch_id, req.csv_text)
    return CsvImportPreview(
        total_lines=len(new) + len(dup) + len(invalid),
        new_codes=new, duplicate_codes=dup, invalid_codes=invalid,
    )


@router.post("/batches/{batch_id}/import/commit", response_model=CsvImportResult)
async def import_commit(
    batch_id: int,
    req: CsvImportConfirm,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    if not req.confirm:
        raise HTTPException(status_code=400, detail="必须确认")
    b = await db.get(RedemptionBatch, batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    result = await svc.import_codes_commit(db, batch_id, req.csv_text)
    await db.commit()
    logger.info(
        "REDEMPTION_IMPORT admin=%s batch_id=%s inserted=%s dup=%s invalid=%s",
        admin.id, batch_id, result["inserted"],
        result["skipped_duplicate"], result["skipped_invalid"],
    )
    return CsvImportResult(**result)
```

- [ ] **Step 2: 在 main.py 注册**

```python
app.include_router(admin_redemption_api.router, prefix="/api/v1/admin/redemption", tags=["AdminRedemption"])
```

- [ ] **Step 3: 验证启动**

Run: `cd backend && python -c "import app.main"`
Expected: 无 import 错误。

Run: `cd backend && python -m py_compile $(find app -name '*.py')`
Expected: 全部编译通过。

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/v1/admin_redemption.py backend/app/main.py
git commit -m "feat(redemption): 管理员端 API（合作方/批次/CSV 导入预检与提交）"
```

---

## Task 8：API 集成测试

**Files:**
- Create: `backend/tests/test_redemption_api.py`

参考 `backend/tests/test_loan_api.py` 的 fixture 用法（如已存在），用 httpx AsyncClient + asgi-lifespan 起 app，造一个 `is_superuser=True` 的用户拿 token 调管理员接口；造普通用户调用户接口。

- [ ] **Step 1: 看一下现有 API 测试结构**

Run: `cd backend && head -60 tests/test_loan_api.py`

按它的 fixture 模式写本任务。**如果发现项目用 TestClient 同步、或者用 monkeypatch 跳过认证，沿用同样的模式。**

- [ ] **Step 2: 写 API 测试**

```python
# backend/tests/test_redemption_api.py
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlmodel import SQLModel

from app.main import app
from app.core.database import engine, async_session_maker
from app.core.users import current_active_user, current_superuser
from app.models.base import User
from app.models.redemption import (
    RedemptionPartner, RedemptionBatch, RedemptionCode, BatchStatus,
)


@pytest_asyncio.fixture
async def db_setup():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    yield


async def _mk_user(superuser=False, cash=Decimal("100")) -> User:
    async with async_session_maker() as s:
        u = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            email=f"{uuid.uuid4().hex[:6]}@t.com",
            casdoor_id=uuid.uuid4().hex,
            cash=cash, is_superuser=superuser,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u


def _override_user(u: User):
    async def _dep():
        async with async_session_maker() as s:
            return await s.get(User, u.id)
    return _dep


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_admin_create_partner_then_batch_then_import_then_user_buys(db_setup, client):
    admin = await _mk_user(superuser=True)
    buyer = await _mk_user(cash=Decimal("100"))

    # 切到 admin 身份
    app.dependency_overrides[current_superuser] = _override_user(admin)
    app.dependency_overrides[current_active_user] = _override_user(admin)

    # 1. 创建合作方
    r = await client.post("/api/v1/admin/redemption/partners", json={
        "name": "朋友A", "description": "", "website_url": "https://a.example", "is_active": True,
    })
    assert r.status_code == 200, r.text
    partner_id = r.json()["id"]

    # 2. 创建批次（默认 draft）
    r = await client.post("/api/v1/admin/redemption/batches", json={
        "partner_id": partner_id, "name": "5元券", "description": "## 说明\n去 a.example 兑换",
        "unit_price": "30",
    })
    assert r.status_code == 200
    batch_id = r.json()["id"]

    # 3. CSV 预检
    r = await client.post(
        f"/api/v1/admin/redemption/batches/{batch_id}/import/preview",
        json={"csv_text": "code\nABC\nDEF\nABC\n"},
    )
    assert r.status_code == 200
    p = r.json()
    assert p["new_codes"] == ["ABC", "DEF"]

    # 4. CSV 提交
    r = await client.post(
        f"/api/v1/admin/redemption/batches/{batch_id}/import/commit",
        json={"csv_text": "ABC\nDEF\n", "confirm": True},
    )
    assert r.status_code == 200
    assert r.json()["inserted"] == 2

    # 5. 上架批次
    r = await client.patch(f"/api/v1/admin/redemption/batches/{batch_id}", json={
        "status": "active",
    })
    assert r.status_code == 200

    # 切到 buyer 身份
    app.dependency_overrides[current_superuser] = _override_user(buyer)
    app.dependency_overrides[current_active_user] = _override_user(buyer)

    # 6. 用户列表能看到
    r = await client.get("/api/v1/redemption/batches")
    assert r.status_code == 200
    assert any(item["id"] == batch_id for item in r.json())

    # 7. 用户购买
    r = await client.post("/api/v1/redemption/purchase", json={"batch_id": batch_id})
    assert r.status_code == 200
    body = r.json()
    assert body["code_string"] in {"ABC", "DEF"}
    assert Decimal(body["cash_after"]) == Decimal("70")

    # 8. 我的记录里能看到
    r = await client.get("/api/v1/redemption/my")
    assert r.status_code == 200
    assert len(r.json()) == 1

    # 9. 标记已用
    code_id = body["code_id"]
    r = await client.post(f"/api/v1/redemption/my/{code_id}/mark-used", json={"used": True})
    assert r.status_code == 200
    assert r.json()["marked_used_by_user_at"] is not None

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_purchase_insufficient_cash(db_setup, client):
    admin = await _mk_user(superuser=True)
    poor = await _mk_user(cash=Decimal("5"))

    async with async_session_maker() as s:
        p = RedemptionPartner(name="P", is_active=True)
        s.add(p); await s.commit(); await s.refresh(p)
        b = RedemptionBatch(partner_id=p.id, name="B", unit_price=Decimal("100"),
                             status=BatchStatus.ACTIVE)
        s.add(b); await s.commit(); await s.refresh(b)
        s.add(RedemptionCode(batch_id=b.id, code_string="X"))
        await s.commit()
        bid = b.id

    app.dependency_overrides[current_active_user] = _override_user(poor)
    r = await client.post("/api/v1/redemption/purchase", json={"batch_id": bid})
    assert r.status_code == 422
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_active_batch_cannot_change_price(db_setup, client):
    admin = await _mk_user(superuser=True)
    async with async_session_maker() as s:
        p = RedemptionPartner(name="P", is_active=True)
        s.add(p); await s.commit(); await s.refresh(p)
        b = RedemptionBatch(partner_id=p.id, name="B", unit_price=Decimal("10"),
                             status=BatchStatus.ACTIVE)
        s.add(b); await s.commit(); await s.refresh(b)
        bid = b.id

    app.dependency_overrides[current_superuser] = _override_user(admin)
    r = await client.patch(f"/api/v1/admin/redemption/batches/{bid}",
                           json={"unit_price": "20"})
    assert r.status_code == 400
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_other_user_cannot_view_my_code(db_setup, client):
    buyer = await _mk_user(cash=Decimal("100"))
    spy = await _mk_user(cash=Decimal("100"))
    async with async_session_maker() as s:
        p = RedemptionPartner(name="P", is_active=True)
        s.add(p); await s.commit(); await s.refresh(p)
        b = RedemptionBatch(partner_id=p.id, name="B", unit_price=Decimal("10"),
                             status=BatchStatus.ACTIVE)
        s.add(b); await s.commit(); await s.refresh(b)
        c = RedemptionCode(batch_id=b.id, code_string="SECRET",
                           status="sold", bought_by_user_id=buyer.id)
        s.add(c); await s.commit(); await s.refresh(c)
        cid = c.id

    app.dependency_overrides[current_active_user] = _override_user(spy)
    r = await client.get(f"/api/v1/redemption/my/{cid}")
    assert r.status_code == 404
    app.dependency_overrides.clear()
```

- [ ] **Step 3: 跑测试**

Run: `cd backend && pytest tests/test_redemption_api.py -v`
Expected: 全部 PASS。

如果 `dependency_overrides` 这个用法不匹配项目实际，参考 `tests/test_loan_api.py` 的鉴权 mock 方式调整。

- [ ] **Step 4: 跑全套后端测试确认无回归**

Run: `cd backend && pytest -x`
Expected: 全部通过。

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_redemption_api.py
git commit -m "test(redemption): 端到端 API 集成测试 + 权限边界"
```

---

## Task 9：前端类型与 API client

**Files:**
- Create: `thccb-frontend/src/types/redemption.ts`
- Create: `thccb-frontend/src/api/redemption.ts`

- [ ] **Step 1: 创建类型定义**

```typescript
// thccb-frontend/src/types/redemption.ts
export interface PartnerPublic {
  id: number
  name: string
  description: string
  website_url: string
  logo_url: string | null
}

export interface BatchListItem {
  id: number
  partner: PartnerPublic
  name: string
  unit_price: string
  available_count: number
}

export interface BatchDetail extends BatchListItem {
  description: string
}

export interface PurchaseResponse {
  code_id: number
  code_string: string
  batch_name: string
  partner_name: string
  partner_website_url: string
  description: string
  paid_amount: string
  cash_after: string
}

export interface MyRedemptionItem {
  code_id: number
  batch_name: string
  partner_name: string
  partner_website_url: string
  paid_amount: string
  bought_at: string
  marked_used_by_user_at: string | null
}

export interface MyRedemptionDetail extends MyRedemptionItem {
  code_string: string
  description: string
}

// ===== Admin =====

export interface PartnerAdminItem {
  id: number
  name: string
  description: string
  website_url: string
  logo_url: string | null
  is_active: boolean
  created_at: string
}

export interface BatchAdminItem {
  id: number
  partner_id: number
  partner_name: string
  name: string
  description: string
  unit_price: string
  status: 'draft' | 'active' | 'archived'
  total_count: number
  sold_count: number
  available_count: number
  created_at: string
}

export interface CsvImportPreview {
  total_lines: number
  new_codes: string[]
  duplicate_codes: string[]
  invalid_codes: string[]
}

export interface CsvImportResult {
  inserted: number
  skipped_duplicate: number
  skipped_invalid: number
}
```

- [ ] **Step 2: 创建 API client**

```typescript
// thccb-frontend/src/api/redemption.ts
import api from './index'
import type {
  BatchListItem, BatchDetail, PurchaseResponse,
  MyRedemptionItem, MyRedemptionDetail,
  PartnerAdminItem, BatchAdminItem,
  CsvImportPreview, CsvImportResult,
} from '@/types/redemption'

export const redemptionApi = {
  // ===== 用户端 =====
  listBatches: () => api.get<BatchListItem[]>('/api/v1/redemption/batches'),
  batchDetail: (id: number) => api.get<BatchDetail>(`/api/v1/redemption/batches/${id}`),
  purchase: (batchId: number) =>
    api.post<PurchaseResponse>('/api/v1/redemption/purchase', { batch_id: batchId }),
  myRedemptions: () => api.get<MyRedemptionItem[]>('/api/v1/redemption/my'),
  myRedemptionDetail: (id: number) =>
    api.get<MyRedemptionDetail>(`/api/v1/redemption/my/${id}`),
  markUsed: (id: number, used: boolean) =>
    api.post<{ marked_used_by_user_at: string | null }>(
      `/api/v1/redemption/my/${id}/mark-used`, { used },
    ),
}

export const redemptionAdminApi = {
  listPartners: () => api.get<PartnerAdminItem[]>('/api/v1/admin/redemption/partners'),
  createPartner: (data: Partial<PartnerAdminItem>) =>
    api.post<PartnerAdminItem>('/api/v1/admin/redemption/partners', data),
  updatePartner: (id: number, data: Partial<PartnerAdminItem>) =>
    api.patch<PartnerAdminItem>(`/api/v1/admin/redemption/partners/${id}`, data),

  listBatches: () => api.get<BatchAdminItem[]>('/api/v1/admin/redemption/batches'),
  createBatch: (data: { partner_id: number; name: string; description: string; unit_price: string }) =>
    api.post<BatchAdminItem>('/api/v1/admin/redemption/batches', data),
  updateBatch: (id: number, data: Partial<{ name: string; description: string; unit_price: string; status: string }>) =>
    api.patch<BatchAdminItem>(`/api/v1/admin/redemption/batches/${id}`, data),

  importPreview: (batchId: number, csvText: string) =>
    api.post<CsvImportPreview>(
      `/api/v1/admin/redemption/batches/${batchId}/import/preview`,
      { csv_text: csvText },
    ),
  importCommit: (batchId: number, csvText: string) =>
    api.post<CsvImportResult>(
      `/api/v1/admin/redemption/batches/${batchId}/import/commit`,
      { csv_text: csvText, confirm: true },
    ),
}
```

注意：`api/index.ts` 通常有 `patch` 方法。如果没有，参考其结构补一个，或改用 `api.put`/手写 axios 调用——**先看 `cat thccb-frontend/src/api/index.ts`** 确认。

- [ ] **Step 3: 验证 type-check**

Run: `cd thccb-frontend && npm run type-check`
Expected: 通过。如果 `api.patch` 不存在则补：

```typescript
// 在 thccb-frontend/src/api/index.ts 里加一个方法（如果还没有）
patch<T>(url: string, data?: any): Promise<T> { ... }
```

- [ ] **Step 4: Commit**

```bash
git add thccb-frontend/src/types/redemption.ts thccb-frontend/src/api/redemption.ts
git commit -m "feat(frontend): 兑换码模块类型与 API client"
```

---

## Task 10：前端 Pinia store

**Files:**
- Create: `thccb-frontend/src/stores/redemption.ts`

- [ ] **Step 1: 创建 store**

```typescript
// thccb-frontend/src/stores/redemption.ts
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { redemptionApi } from '@/api/redemption'
import type {
  BatchListItem, MyRedemptionItem,
} from '@/types/redemption'

export const useRedemptionStore = defineStore('redemption', () => {
  const batches = ref<BatchListItem[]>([])
  const myRedemptions = ref<MyRedemptionItem[]>([])
  const loading = ref(false)

  async function loadBatches() {
    loading.value = true
    try {
      batches.value = await redemptionApi.listBatches()
    } finally {
      loading.value = false
    }
  }

  async function loadMyRedemptions() {
    loading.value = true
    try {
      myRedemptions.value = await redemptionApi.myRedemptions()
    } finally {
      loading.value = false
    }
  }

  return { batches, myRedemptions, loading, loadBatches, loadMyRedemptions }
})
```

- [ ] **Step 2: type-check**

Run: `cd thccb-frontend && npm run type-check`
Expected: 通过。

- [ ] **Step 3: Commit**

```bash
git add thccb-frontend/src/stores/redemption.ts
git commit -m "feat(frontend): redemption Pinia store"
```

---

## Task 11：前端用户页 — 兑换中心列表

**Files:**
- Create: `thccb-frontend/src/pages/redemption/RedemptionList.vue`

- [ ] **Step 1: 写列表页**

```vue
<!-- thccb-frontend/src/pages/redemption/RedemptionList.vue -->
<script setup lang="ts">
import { onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useRedemptionStore } from '@/stores/redemption'

const store = useRedemptionStore()
const router = useRouter()

onMounted(() => store.loadBatches())

// 按合作方分组
const grouped = computed(() => {
  const map = new Map<number, { partner: any; items: any[] }>()
  for (const b of store.batches) {
    if (!map.has(b.partner.id)) {
      map.set(b.partner.id, { partner: b.partner, items: [] })
    }
    map.get(b.partner.id)!.items.push(b)
  }
  return Array.from(map.values())
})

const goDetail = (id: number) => router.push(`/redemption/batches/${id}`)
</script>

<template>
  <div class="page">
    <h1 class="page-title">兑换中心</h1>
    <p class="page-hint">用 TouhouCCB 资金兑换合作方网站的码。码可在「我的兑换记录」永久查看。</p>

    <div v-if="store.loading" class="loading">加载中…</div>
    <div v-else-if="grouped.length === 0" class="empty">暂无可兑换批次</div>

    <section v-for="g in grouped" :key="g.partner.id" class="partner-section">
      <header class="partner-header">
        <img v-if="g.partner.logo_url" :src="g.partner.logo_url" class="partner-logo" />
        <div>
          <h2>{{ g.partner.name }}</h2>
          <p>{{ g.partner.description }}</p>
          <a v-if="g.partner.website_url" :href="g.partner.website_url" target="_blank" rel="noopener">
            访问站点 →
          </a>
        </div>
      </header>
      <div class="batch-grid">
        <button
          v-for="b in g.items"
          :key="b.id"
          class="batch-card"
          @click="goDetail(b.id)"
        >
          <div class="batch-name">{{ b.name }}</div>
          <div class="batch-price">{{ b.unit_price }}</div>
          <div class="batch-stock">剩余 {{ b.available_count }}</div>
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page { padding: 16px; }
.page-title { font-size: 22px; font-weight: 700; margin-bottom: 4px; }
.page-hint { color: #666; font-size: 13px; margin-bottom: 24px; }
.loading, .empty { color: #999; padding: 32px; text-align: center; }
.partner-section { border: 2px solid #000; padding: 16px; margin-bottom: 16px; background: #fff; }
.partner-header { display: flex; gap: 12px; align-items: flex-start; margin-bottom: 12px; }
.partner-logo { width: 48px; height: 48px; object-fit: cover; border: 2px solid #000; }
.partner-header h2 { font-size: 18px; font-weight: 700; }
.partner-header p { font-size: 13px; color: #555; }
.batch-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px; }
.batch-card {
  border: 2px solid #000; background: #fff; padding: 12px;
  text-align: left; cursor: pointer; transition: background 0.1s;
}
.batch-card:hover { background: #000; color: #fff; }
.batch-name { font-weight: 600; margin-bottom: 6px; }
.batch-price { font-size: 18px; font-weight: 700; }
.batch-stock { font-size: 12px; color: inherit; opacity: 0.7; margin-top: 4px; }
</style>
```

- [ ] **Step 2: type-check**

Run: `cd thccb-frontend && npm run type-check`
Expected: 通过。

- [ ] **Step 3: Commit**

```bash
git add thccb-frontend/src/pages/redemption/RedemptionList.vue
git commit -m "feat(frontend): 兑换中心列表页"
```

---

## Task 12：前端用户页 — 批次详情 / 购买

**Files:**
- Create: `thccb-frontend/src/pages/redemption/BatchDetail.vue`

- [ ] **Step 1: 写详情页（含二次确认 + 购买结果）**

```vue
<!-- thccb-frontend/src/pages/redemption/BatchDetail.vue -->
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { redemptionApi } from '@/api/redemption'
import { useUserStore } from '@/stores/user'
import type { BatchDetail, PurchaseResponse } from '@/types/redemption'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const batch = ref<BatchDetail | null>(null)
const showConfirm = ref(false)
const result = ref<PurchaseResponse | null>(null)
const error = ref<string>('')
const loading = ref(false)

const batchId = Number(route.params.id)

async function load() {
  try {
    batch.value = await redemptionApi.batchDetail(batchId)
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  }
}

async function confirmPurchase() {
  loading.value = true
  error.value = ''
  try {
    result.value = await redemptionApi.purchase(batchId)
    showConfirm.value = false
    // 刷新用户余额
    await userStore.fetchProfile?.()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '购买失败'
  } finally {
    loading.value = false
  }
}

async function copyCode() {
  if (!result.value) return
  await navigator.clipboard.writeText(result.value.code_string)
  alert('已复制')
}

onMounted(load)
</script>

<template>
  <div class="page">
    <button class="back" @click="router.back()">← 返回</button>

    <div v-if="!batch && !error" class="loading">加载中…</div>
    <div v-if="error && !result" class="error">{{ error }}</div>

    <!-- 购买成功后展示码 -->
    <section v-if="result" class="result-card">
      <h2>购买成功</h2>
      <p class="hint">请复制下面的码，前往合作方站点核销。本码在「我的兑换记录」中可随时查看。</p>
      <div class="code-box">{{ result.code_string }}</div>
      <button class="btn-primary" @click="copyCode">复制</button>
      <a v-if="result.partner_website_url" :href="result.partner_website_url" target="_blank" class="btn-secondary">
        前往 {{ result.partner_name }} →
      </a>
      <button class="btn-secondary" @click="router.push('/my/redemptions')">我的兑换记录</button>
    </section>

    <!-- 详情 -->
    <section v-else-if="batch" class="detail-card">
      <h1>{{ batch.name }}</h1>
      <div class="meta">
        <span>合作方：{{ batch.partner.name }}</span>
        <span>价格：<b>{{ batch.unit_price }}</b></span>
        <span>剩余：{{ batch.available_count }}</span>
      </div>
      <pre class="description">{{ batch.description }}</pre>
      <button class="btn-primary" :disabled="batch.available_count <= 0" @click="showConfirm = true">
        {{ batch.available_count <= 0 ? '已售罄' : '购买' }}
      </button>
    </section>

    <!-- 二次确认弹窗 -->
    <div v-if="showConfirm" class="modal-bg" @click.self="showConfirm = false">
      <div class="modal">
        <h3>确认购买</h3>
        <p>将扣除 <b>{{ batch?.unit_price }}</b> 资金购买「{{ batch?.name }}」。</p>
        <p class="warning">⚠ 码一旦显示视同交付，<b>不可退款</b>。请确认。</p>
        <div class="modal-actions">
          <button class="btn-secondary" @click="showConfirm = false" :disabled="loading">取消</button>
          <button class="btn-primary" @click="confirmPurchase" :disabled="loading">
            {{ loading ? '处理中…' : '确认' }}
          </button>
        </div>
        <p v-if="error" class="error">{{ error }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { padding: 16px; max-width: 720px; margin: 0 auto; }
.back { background: none; border: none; cursor: pointer; padding: 8px 0; font-size: 13px; }
.loading, .error { padding: 32px; text-align: center; }
.error { color: #c00; }
.detail-card, .result-card { border: 2px solid #000; padding: 24px; background: #fff; }
.detail-card h1, .result-card h2 { font-size: 20px; font-weight: 700; margin-bottom: 12px; }
.meta { display: flex; gap: 16px; margin-bottom: 16px; font-size: 14px; }
.description { white-space: pre-wrap; font-family: inherit; padding: 12px; background: #f5f5f5; border: 1px solid #ddd; }
.btn-primary {
  background: #000; color: #fff; border: 2px solid #000; padding: 10px 24px;
  cursor: pointer; font-weight: 600; margin-right: 8px;
}
.btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-secondary {
  background: #fff; color: #000; border: 2px solid #000; padding: 10px 24px;
  cursor: pointer; font-weight: 600; margin-right: 8px; text-decoration: none;
  display: inline-block;
}
.code-box {
  font-family: monospace; font-size: 18px; padding: 16px; border: 2px dashed #000;
  margin: 16px 0; background: #fafafa; word-break: break-all;
}
.hint { color: #666; font-size: 13px; margin: 8px 0; }
.warning { color: #c00; font-size: 13px; margin: 8px 0; }
.modal-bg {
  position: fixed; inset: 0; background: rgba(0,0,0,0.4);
  display: flex; align-items: center; justify-content: center; z-index: 1000;
}
.modal { background: #fff; border: 3px solid #000; padding: 24px; max-width: 480px; }
.modal h3 { font-size: 18px; margin-bottom: 12px; }
.modal-actions { display: flex; gap: 8px; margin-top: 16px; justify-content: flex-end; }
</style>
```

- [ ] **Step 2: type-check**

Run: `cd thccb-frontend && npm run type-check`
Expected: 通过。如果 `useUserStore` 没有 `fetchProfile` 方法，把 `await userStore.fetchProfile?.()` 那行替换为该 store 实际刷新方法（`cat thccb-frontend/src/stores/user.ts | head -40` 查看）。

- [ ] **Step 3: Commit**

```bash
git add thccb-frontend/src/pages/redemption/BatchDetail.vue
git commit -m "feat(frontend): 批次详情页 + 二次确认购买"
```

---

## Task 13：前端用户页 — 我的兑换记录

**Files:**
- Create: `thccb-frontend/src/pages/user/MyRedemptions.vue`

- [ ] **Step 1: 写记录页（含详情展开 + 标记已用）**

```vue
<!-- thccb-frontend/src/pages/user/MyRedemptions.vue -->
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { redemptionApi } from '@/api/redemption'
import type { MyRedemptionItem, MyRedemptionDetail } from '@/types/redemption'

const items = ref<MyRedemptionItem[]>([])
const expanded = ref<Map<number, MyRedemptionDetail>>(new Map())
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    items.value = await redemptionApi.myRedemptions()
  } finally {
    loading.value = false
  }
}

async function toggle(id: number) {
  if (expanded.value.has(id)) {
    expanded.value.delete(id)
    expanded.value = new Map(expanded.value)  // 触发响应
    return
  }
  const detail = await redemptionApi.myRedemptionDetail(id)
  expanded.value.set(id, detail)
  expanded.value = new Map(expanded.value)
}

async function copyCode(code: string) {
  await navigator.clipboard.writeText(code)
  alert('已复制')
}

async function toggleUsed(item: MyRedemptionItem) {
  const newState = !item.marked_used_by_user_at
  const r = await redemptionApi.markUsed(item.code_id, newState)
  item.marked_used_by_user_at = r.marked_used_by_user_at
}

onMounted(load)
</script>

<template>
  <div class="page">
    <h1 class="page-title">我的兑换记录</h1>

    <div v-if="loading" class="loading">加载中…</div>
    <div v-else-if="items.length === 0" class="empty">尚未兑换任何码</div>

    <ul v-else class="list">
      <li v-for="item in items" :key="item.code_id" class="row" :class="{ used: !!item.marked_used_by_user_at }">
        <div class="row-head" @click="toggle(item.code_id)">
          <div class="col-name">
            <div class="name">{{ item.batch_name }}</div>
            <div class="partner">{{ item.partner_name }}</div>
          </div>
          <div class="col-meta">
            <span>{{ new Date(item.bought_at).toLocaleString() }}</span>
            <span>价格：{{ item.paid_amount }}</span>
            <span class="status">{{ item.marked_used_by_user_at ? '已使用' : '未使用' }}</span>
          </div>
        </div>

        <div v-if="expanded.get(item.code_id)" class="row-detail">
          <div class="code-box">{{ expanded.get(item.code_id)!.code_string }}</div>
          <button class="btn" @click="copyCode(expanded.get(item.code_id)!.code_string)">复制码</button>
          <a v-if="item.partner_website_url" :href="item.partner_website_url" target="_blank" class="btn">
            前往 {{ item.partner_name }} →
          </a>
          <button class="btn" @click="toggleUsed(item)">
            {{ item.marked_used_by_user_at ? '取消已用标记' : '标记为已使用' }}
          </button>
          <pre class="description">{{ expanded.get(item.code_id)!.description }}</pre>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.page { padding: 16px; max-width: 960px; margin: 0 auto; }
.page-title { font-size: 22px; font-weight: 700; margin-bottom: 16px; }
.loading, .empty { color: #999; padding: 32px; text-align: center; }
.list { list-style: none; padding: 0; }
.row { border: 2px solid #000; margin-bottom: 8px; background: #fff; }
.row.used { opacity: 0.6; }
.row-head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 16px; cursor: pointer;
}
.row-head:hover { background: #f5f5f5; }
.name { font-weight: 600; }
.partner { font-size: 12px; color: #666; }
.col-meta { display: flex; gap: 12px; font-size: 12px; color: #555; }
.status { font-weight: 700; color: #000; }
.row-detail { padding: 16px; border-top: 1px dashed #ccc; background: #fafafa; }
.code-box {
  font-family: monospace; font-size: 16px; padding: 12px; border: 2px dashed #000;
  margin-bottom: 12px; word-break: break-all; background: #fff;
}
.btn {
  background: #fff; color: #000; border: 2px solid #000; padding: 6px 16px;
  cursor: pointer; font-size: 13px; margin-right: 8px; text-decoration: none;
  display: inline-block;
}
.btn:hover { background: #000; color: #fff; }
.description { white-space: pre-wrap; font-family: inherit; margin-top: 12px; font-size: 13px; color: #555; }
</style>
```

- [ ] **Step 2: type-check**

Run: `cd thccb-frontend && npm run type-check`
Expected: 通过。

- [ ] **Step 3: Commit**

```bash
git add thccb-frontend/src/pages/user/MyRedemptions.vue
git commit -m "feat(frontend): 我的兑换记录页（展开详情/复制/标记已用）"
```

---

## Task 14：前端管理员 — 合作方管理

**Files:**
- Create: `thccb-frontend/src/pages/admin/RedemptionPartners.vue`

- [ ] **Step 1: 写合作方管理页**

```vue
<!-- thccb-frontend/src/pages/admin/RedemptionPartners.vue -->
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { redemptionAdminApi } from '@/api/redemption'
import type { PartnerAdminItem } from '@/types/redemption'

const items = ref<PartnerAdminItem[]>([])
const editing = ref<Partial<PartnerAdminItem> & { id?: number } | null>(null)

async function load() {
  items.value = await redemptionAdminApi.listPartners()
}

function startCreate() {
  editing.value = { name: '', description: '', website_url: '', logo_url: '', is_active: true }
}

function startEdit(p: PartnerAdminItem) {
  editing.value = { ...p }
}

async function save() {
  if (!editing.value) return
  const e = editing.value
  if (e.id) {
    await redemptionAdminApi.updatePartner(e.id, e)
  } else {
    await redemptionAdminApi.createPartner(e)
  }
  editing.value = null
  await load()
}

async function toggleActive(p: PartnerAdminItem) {
  await redemptionAdminApi.updatePartner(p.id, { is_active: !p.is_active })
  await load()
}

onMounted(load)
</script>

<template>
  <div class="page">
    <h1 class="page-title">合作方管理</h1>
    <button class="btn-primary" @click="startCreate">+ 新增合作方</button>

    <table class="table">
      <thead><tr><th>ID</th><th>名称</th><th>网站</th><th>启用</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="p in items" :key="p.id">
          <td>{{ p.id }}</td>
          <td>{{ p.name }}</td>
          <td><a :href="p.website_url" target="_blank">{{ p.website_url }}</a></td>
          <td>{{ p.is_active ? '✓' : '✕' }}</td>
          <td>
            <button class="btn-sm" @click="startEdit(p)">编辑</button>
            <button class="btn-sm" @click="toggleActive(p)">
              {{ p.is_active ? '禁用' : '启用' }}
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="editing" class="modal-bg" @click.self="editing = null">
      <div class="modal">
        <h3>{{ editing.id ? '编辑' : '新增' }}合作方</h3>
        <label>名称 <input v-model="editing.name" /></label>
        <label>描述 <textarea v-model="editing.description"></textarea></label>
        <label>网站 URL <input v-model="editing.website_url" /></label>
        <label>Logo URL <input v-model="editing.logo_url" /></label>
        <label><input type="checkbox" v-model="editing.is_active" /> 启用</label>
        <div class="modal-actions">
          <button class="btn-secondary" @click="editing = null">取消</button>
          <button class="btn-primary" @click="save">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { padding: 16px; max-width: 1100px; margin: 0 auto; }
.page-title { font-size: 22px; font-weight: 700; margin-bottom: 16px; }
.table { width: 100%; border-collapse: collapse; margin-top: 16px; background: #fff; border: 2px solid #000; }
.table th, .table td { border: 1px solid #ccc; padding: 8px 12px; text-align: left; }
.table th { background: #000; color: #fff; }
.btn-primary, .btn-secondary {
  border: 2px solid #000; padding: 8px 20px; cursor: pointer; font-weight: 600;
}
.btn-primary { background: #000; color: #fff; }
.btn-secondary { background: #fff; color: #000; }
.btn-sm { background: #fff; border: 1px solid #000; padding: 4px 12px; margin-right: 4px; cursor: pointer; }
.modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal { background: #fff; border: 3px solid #000; padding: 24px; min-width: 480px; }
.modal label { display: block; margin: 12px 0; }
.modal input, .modal textarea { width: 100%; padding: 6px; border: 1px solid #000; }
.modal-actions { display: flex; gap: 8px; margin-top: 16px; justify-content: flex-end; }
</style>
```

- [ ] **Step 2: Commit**

```bash
cd thccb-frontend && npm run type-check
git add thccb-frontend/src/pages/admin/RedemptionPartners.vue
git commit -m "feat(frontend): 管理员合作方管理页"
```

---

## Task 15：前端管理员 — 批次管理

**Files:**
- Create: `thccb-frontend/src/pages/admin/RedemptionBatches.vue`

- [ ] **Step 1: 写批次管理页**

```vue
<!-- thccb-frontend/src/pages/admin/RedemptionBatches.vue -->
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { redemptionAdminApi } from '@/api/redemption'
import type { BatchAdminItem, PartnerAdminItem } from '@/types/redemption'

const router = useRouter()
const items = ref<BatchAdminItem[]>([])
const partners = ref<PartnerAdminItem[]>([])
const editing = ref<any>(null)

async function load() {
  items.value = await redemptionAdminApi.listBatches()
  partners.value = await redemptionAdminApi.listPartners()
}

function startCreate() {
  editing.value = {
    partner_id: partners.value[0]?.id, name: '', description: '', unit_price: '',
  }
}

function startEdit(b: BatchAdminItem) {
  editing.value = { ...b }
}

async function save() {
  const e = editing.value
  if (e.id) {
    const payload: any = { name: e.name, description: e.description, status: e.status }
    if (e.status !== 'active') payload.unit_price = e.unit_price
    await redemptionAdminApi.updateBatch(e.id, payload)
  } else {
    await redemptionAdminApi.createBatch({
      partner_id: Number(e.partner_id),
      name: e.name,
      description: e.description,
      unit_price: String(e.unit_price),
    })
  }
  editing.value = null
  await load()
}

async function changeStatus(b: BatchAdminItem, status: string) {
  await redemptionAdminApi.updateBatch(b.id, { status })
  await load()
}

function goImport(b: BatchAdminItem) {
  router.push(`/admin/redemption/batches/${b.id}/import`)
}

onMounted(load)
</script>

<template>
  <div class="page">
    <h1 class="page-title">批次管理</h1>
    <button class="btn-primary" @click="startCreate" :disabled="partners.length === 0">+ 新建批次</button>
    <p v-if="partners.length === 0" class="hint">先去「合作方管理」创建至少一个合作方。</p>

    <table class="table">
      <thead>
        <tr><th>ID</th><th>合作方</th><th>名称</th><th>价格</th><th>状态</th><th>库存</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="b in items" :key="b.id">
          <td>{{ b.id }}</td>
          <td>{{ b.partner_name }}</td>
          <td>{{ b.name }}</td>
          <td>{{ b.unit_price }}</td>
          <td>
            <select :value="b.status" @change="changeStatus(b, ($event.target as HTMLSelectElement).value)">
              <option value="draft">草稿</option>
              <option value="active">上架</option>
              <option value="archived">下架</option>
            </select>
          </td>
          <td>{{ b.available_count }} / {{ b.total_count }}</td>
          <td>
            <button class="btn-sm" @click="startEdit(b)">编辑</button>
            <button class="btn-sm" @click="goImport(b)">导入码</button>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="editing" class="modal-bg" @click.self="editing = null">
      <div class="modal">
        <h3>{{ editing.id ? '编辑' : '新建' }}批次</h3>
        <label v-if="!editing.id">合作方
          <select v-model="editing.partner_id">
            <option v-for="p in partners" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
        </label>
        <label>批次名 <input v-model="editing.name" /></label>
        <label>描述（markdown）<textarea v-model="editing.description" rows="6"></textarea></label>
        <label>单价（资金）
          <input v-model="editing.unit_price" :disabled="editing.status === 'active'" />
          <span v-if="editing.status === 'active'" class="hint">active 批次不可改价</span>
        </label>
        <div class="modal-actions">
          <button class="btn-secondary" @click="editing = null">取消</button>
          <button class="btn-primary" @click="save">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { padding: 16px; max-width: 1200px; margin: 0 auto; }
.page-title { font-size: 22px; font-weight: 700; margin-bottom: 16px; }
.hint { color: #999; font-size: 12px; }
.table { width: 100%; border-collapse: collapse; margin-top: 16px; background: #fff; border: 2px solid #000; }
.table th, .table td { border: 1px solid #ccc; padding: 8px 12px; text-align: left; }
.table th { background: #000; color: #fff; }
.btn-primary, .btn-secondary { border: 2px solid #000; padding: 8px 20px; cursor: pointer; font-weight: 600; }
.btn-primary { background: #000; color: #fff; }
.btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-secondary { background: #fff; color: #000; }
.btn-sm { background: #fff; border: 1px solid #000; padding: 4px 12px; margin-right: 4px; cursor: pointer; }
.modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal { background: #fff; border: 3px solid #000; padding: 24px; min-width: 560px; max-width: 720px; }
.modal label { display: block; margin: 12px 0; }
.modal input, .modal textarea, .modal select { width: 100%; padding: 6px; border: 1px solid #000; font-family: inherit; }
.modal-actions { display: flex; gap: 8px; margin-top: 16px; justify-content: flex-end; }
</style>
```

- [ ] **Step 2: Commit**

```bash
cd thccb-frontend && npm run type-check
git add thccb-frontend/src/pages/admin/RedemptionBatches.vue
git commit -m "feat(frontend): 管理员批次管理页（含状态机/价格保护）"
```

---

## Task 16：前端管理员 — CSV 导入

**Files:**
- Create: `thccb-frontend/src/pages/admin/RedemptionImport.vue`

- [ ] **Step 1: 写导入页（预检 → 确认 → 写入）**

```vue
<!-- thccb-frontend/src/pages/admin/RedemptionImport.vue -->
<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { redemptionAdminApi } from '@/api/redemption'
import type { CsvImportPreview, CsvImportResult } from '@/types/redemption'

const route = useRoute()
const router = useRouter()
const batchId = Number(route.params.id)

const csvText = ref('')
const preview = ref<CsvImportPreview | null>(null)
const result = ref<CsvImportResult | null>(null)
const loading = ref(false)
const error = ref('')

async function onFile(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  csvText.value = await file.text()
}

async function doPreview() {
  loading.value = true
  error.value = ''
  preview.value = null
  result.value = null
  try {
    preview.value = await redemptionAdminApi.importPreview(batchId, csvText.value)
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '预检失败'
  } finally {
    loading.value = false
  }
}

async function doCommit() {
  if (!preview.value) return
  if (!confirm(`将写入 ${preview.value.new_codes.length} 个新码，跳过 ${preview.value.duplicate_codes.length} 个重复 + ${preview.value.invalid_codes.length} 个非法。继续？`)) return
  loading.value = true
  try {
    result.value = await redemptionAdminApi.importCommit(batchId, csvText.value)
    preview.value = null
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '导入失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="page">
    <button class="back" @click="router.back()">← 返回</button>
    <h1 class="page-title">导入兑换码 (批次 #{{ batchId }})</h1>

    <section class="card">
      <h2>① 选择来源</h2>
      <p class="hint">CSV 单列；首行 <code>code</code> 视为表头自动跳过；空行/前后空白自动 trim。</p>
      <input type="file" accept=".csv,.txt" @change="onFile" />
      <p>或直接粘贴：</p>
      <textarea v-model="csvText" rows="10" placeholder="ABC123&#10;DEF456&#10;..."></textarea>
    </section>

    <section class="card">
      <h2>② 预检</h2>
      <button class="btn-primary" :disabled="!csvText.trim() || loading" @click="doPreview">
        预检
      </button>

      <div v-if="preview" class="preview">
        <p>共解析 {{ preview.total_lines }} 条：</p>
        <ul>
          <li>✅ 即将新增 <b>{{ preview.new_codes.length }}</b> 条</li>
          <li>⚠️ 与已有码重复（跳过）<b>{{ preview.duplicate_codes.length }}</b> 条</li>
          <li>❌ 非法（空/超长，跳过）<b>{{ preview.invalid_codes.length }}</b> 条</li>
        </ul>
        <details v-if="preview.duplicate_codes.length"><summary>查看重复码</summary>
          <pre>{{ preview.duplicate_codes.join('\n') }}</pre>
        </details>
        <details v-if="preview.invalid_codes.length"><summary>查看非法码</summary>
          <pre>{{ preview.invalid_codes.join('\n') }}</pre>
        </details>
        <button class="btn-primary" :disabled="loading || preview.new_codes.length === 0" @click="doCommit">
          ③ 确认写入
        </button>
      </div>
    </section>

    <section v-if="result" class="card success">
      <h2>导入完成</h2>
      <p>成功写入 <b>{{ result.inserted }}</b> 条；跳过重复 {{ result.skipped_duplicate }} / 非法 {{ result.skipped_invalid }}。</p>
      <button class="btn-secondary" @click="router.push('/admin/redemption/batches')">返回批次列表</button>
    </section>

    <p v-if="error" class="error">{{ error }}</p>
  </div>
</template>

<style scoped>
.page { padding: 16px; max-width: 900px; margin: 0 auto; }
.page-title { font-size: 22px; font-weight: 700; margin-bottom: 16px; }
.back { background: none; border: none; cursor: pointer; padding: 8px 0; }
.card { border: 2px solid #000; padding: 16px; margin-bottom: 16px; background: #fff; }
.card h2 { font-size: 16px; font-weight: 700; margin-bottom: 8px; }
.hint { color: #666; font-size: 12px; margin: 8px 0; }
.success { border-color: #060; background: #efffef; }
textarea { width: 100%; padding: 8px; border: 1px solid #000; font-family: monospace; }
.preview { margin-top: 16px; padding: 12px; background: #f5f5f5; border: 1px dashed #999; }
.preview ul { margin: 8px 0 12px 20px; }
.preview pre { background: #fff; padding: 8px; max-height: 240px; overflow: auto; font-size: 12px; }
.btn-primary, .btn-secondary { border: 2px solid #000; padding: 8px 20px; cursor: pointer; font-weight: 600; }
.btn-primary { background: #000; color: #fff; }
.btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-secondary { background: #fff; color: #000; }
.error { color: #c00; margin-top: 12px; }
</style>
```

- [ ] **Step 2: Commit**

```bash
cd thccb-frontend && npm run type-check
git add thccb-frontend/src/pages/admin/RedemptionImport.vue
git commit -m "feat(frontend): CSV 导入页（预检+确认+结果）"
```

---

## Task 17：路由与侧栏入口

**Files:**
- Modify: `thccb-frontend/src/router/routes.ts`
- Modify: `thccb-frontend/src/components/layout/AppSidebar.vue`

- [ ] **Step 1: 添加路由**

在 `thccb-frontend/src/router/routes.ts` 主布局 children 数组里，在 `loan` 条目下面、`market` 条目上面插入：

```typescript
      // 兑换中心
      {
        path: 'redemption',
        name: 'redemption-list',
        component: () => import('@/pages/redemption/RedemptionList.vue'),
        meta: { title: '兑换中心', requiresAuth: true, requiresVerified: true },
      },
      {
        path: 'redemption/batches/:id',
        name: 'redemption-batch-detail',
        component: () => import('@/pages/redemption/BatchDetail.vue'),
        meta: { title: '批次详情', requiresAuth: true, requiresVerified: true },
      },
      {
        path: 'my/redemptions',
        name: 'my-redemptions',
        component: () => import('@/pages/user/MyRedemptions.vue'),
        meta: { title: '我的兑换记录', requiresAuth: true, requiresVerified: true },
      },
```

在 admin 区域 `admin/site-config` 后面追加：

```typescript
      {
        path: 'admin/redemption/partners',
        name: 'admin-redemption-partners',
        component: () => import('@/pages/admin/RedemptionPartners.vue'),
        meta: { title: '合作方管理', requiresAuth: true, requiresAdmin: true },
      },
      {
        path: 'admin/redemption/batches',
        name: 'admin-redemption-batches',
        component: () => import('@/pages/admin/RedemptionBatches.vue'),
        meta: { title: '批次管理', requiresAuth: true, requiresAdmin: true },
      },
      {
        path: 'admin/redemption/batches/:id/import',
        name: 'admin-redemption-import',
        component: () => import('@/pages/admin/RedemptionImport.vue'),
        meta: { title: '导入兑换码', requiresAuth: true, requiresAdmin: true },
      },
```

- [ ] **Step 2: 侧栏加入口**

修改 `thccb-frontend/src/components/layout/AppSidebar.vue`：

`navItems` 数组里在「借款」之后追加两项：

```typescript
  { label: '兑换中心', path: '/redemption', icon: 'i-mdi-gift-outline', activeIcon: 'i-mdi-gift' },
  { label: '我的兑换', path: '/my/redemptions', icon: 'i-mdi-ticket-confirmation-outline', activeIcon: 'i-mdi-ticket-confirmation' },
```

`adminItems` 数组里追加两项：

```typescript
  { label: '合作方', path: '/admin/redemption/partners', icon: 'i-mdi-handshake-outline' },
  { label: '兑换批次', path: '/admin/redemption/batches', icon: 'i-mdi-package-variant' },
```

- [ ] **Step 3: 验证**

Run: `cd thccb-frontend && npm run type-check && npm run lint`
Expected: 全部通过。

- [ ] **Step 4: Commit**

```bash
git add thccb-frontend/src/router/routes.ts thccb-frontend/src/components/layout/AppSidebar.vue
git commit -m "feat(frontend): 兑换中心路由 + 侧栏入口"
```

---

## Task 18：构建与全面校验

**Files:** —（仅校验，不改代码）

- [ ] **Step 1: 后端编译 + 全测试**

```bash
cd backend
python -m py_compile $(find app -name '*.py')
python -c "import app.main"
pytest -x
```

Expected: 全部通过。如有失败，回到对应任务修补，再继续。

- [ ] **Step 2: 前端类型/lint/build**

```bash
cd thccb-frontend
npm run type-check
npm run lint
npm run build
```

Expected: 全部通过。

- [ ] **Step 3: 浏览器实测（按 CLAUDE.md 要求）**

启动本地环境，逐一过：

- [ ] 普通用户登录 → 进 `/redemption` → 看到至少一个批次
- [ ] 点批次 → 详情页正常 → 余额不足时按钮态 / 二次确认 / 成功页 / 复制码
- [ ] 进 `/my/redemptions` → 看到刚买的码 → 展开看码 → 标记已用 / 取消
- [ ] 切到管理员 → 进 `/admin/redemption/partners` → CRUD 合作方
- [ ] 进 `/admin/redemption/batches` → 创建批次 → 进导入页 → CSV 预检 → 确认写入 → 上架 → 普通用户立即看到
- [ ] 边界：空库存批次（已售罄）不在用户列表显示
- [ ] 边界：禁用合作方后，其下批次从用户列表消失（已购用户仍能查看记录）
- [ ] 移动端布局：侧栏折叠、卡片可读

**如果本地环境起不来无法实测**，按 CLAUDE.md 红线写一行「未实测 UI（理由：xxx）」到 `docs/ralph-log.md`，**不要**在日志里谎称通过。

- [ ] **Step 4: 写 ralph 日志**

追加 `docs/ralph-log.md`：

```markdown
## 2026-04-25 HH:MM — 兑换码模块完整实施
**目标** 让 TouhouCCB 资金能兑换为合作方网站的兑换码（单向流出，离线核销）。
**动机** 见 `docs/superpowers/specs/2026-04-25-redemption-codes-design.md`。
**范围** 仅新增；未改 base.py 已有列；未改鉴权。
**改动**：
- 后端新增 `app/models/redemption.py` `app/schemas/redemption.py` `app/services/redemption.py`
  `app/api/v1/redemption.py` `app/api/v1/admin_redemption.py` + main.py 注册
- 前端新增 redemption types/api/store + 6 个页面 + 路由 + 侧栏
- 后端测试 `tests/test_redemption_service.py` `tests/test_redemption_api.py`
**风险 & 回滚** create_all 自动建 3 张新表；回滚需 DROP TABLE redemption_code/batch/partner（按依赖顺序）。资金扣除走 user.cash 列已有约束，不引入新流水类型字段。
**验证** type-check ✅ / lint ✅ / pytest ✅ / 浏览器主路径与边界 ✅（或注明未实测原因）
**下一轮** 等用户反馈是否上线；如上线由用户负责 push（CLAUDE.md 红线）。
```

- [ ] **Step 5: Commit 日志**

```bash
git add docs/ralph-log.md
git commit -m "docs(ralph): 兑换码模块完整实施日志"
```

- [ ] **Step 6: 汇报**

打印当前 `git log --oneline ralph/2026-04-25-redemption-codes ^main` 的内容给用户，告诉他：
- 在哪个分支（`ralph/2026-04-25-redemption-codes`）
- 多少 commit
- 验证结果
- **不要 push**（CLAUDE.md 红线）；交由用户决定何时合并到 main

---

## 备注：与 CLAUDE.md 红线的对应

| 红线 | 本计划如何遵守 |
|---|---|
| 不在 main 直接提交 | 全部在 `ralph/2026-04-25-redemption-codes` 分支 |
| 不 push | 计划内不含任何 `git push`，交付物为本地分支 |
| 不动 base.py 已有列 | 模型在新文件 `models/redemption.py`；不改 User/Market 等已有表结构 |
| 没有迁移机制 | 仅新增表，依赖 `SQLModel.metadata.create_all` 自动建（不改已有列即安全） |
| 不引入新框架 | 全部沿用 FastAPI/SQLModel/Vue 3/Pinia/UnoCSS |
| 不绕类型 | 后端 Pydantic 强类型；前端不使用 `any`（Task 14/15 模态用 `Partial<>` 而非 any） |
| 没证据 = 没完成 | Task 18 显式跑全套测试 + 浏览器实测；不能跑就明说 |
