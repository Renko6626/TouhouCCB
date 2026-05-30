# 资金流水账本 LedgerEntry 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给借款/还款/管理员调现金/强制放贷/免债这五类**目前无历史流水**的资金事件补一张 `LedgerEntry` 账本表，使未来任意时刻的 cash/debt 可被精确重建。

**Architecture:** 新增 `LedgerEntry` 模型 + 一个 `ledger_service.record_entry()` 写入助手。账本写入收敛在 `loan_service` 的两个**公开加锁包装函数**（`increase_debt` / `decrease_debt`）和 `user.py` 的 adjust-cash handler 里，全部在既有事务内同步写。**不碰 buy/sell hot path，也不碰 `decrease_debt_locked`（它被 liquidation 直接调用，liquidation 已有 LiquidationEvent，不能重复记账）。** 利息不落行（可由公式 + 快照重算）。

**Tech Stack:** FastAPI + SQLModel + SQLAlchemy 2.0 async + Alembic + pytest-asyncio。后端在 `backend/`，venv 在 `backend/venv`。

**前置：** 已在分支 `feat/loan-ledger`。所有命令在 `backend/` 目录下、用 `backend/venv` 运行。

---

### Task 1: LedgerEntry 模型

**Files:**
- Create: `backend/app/models/ledger.py`
- Test: `backend/tests/test_ledger_model.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_ledger_model.py
import pytest
import pytest_asyncio
from decimal import Decimal
from datetime import datetime, timezone


@pytest_asyncio.fixture
async def session(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlmodel import SQLModel
    from app.models import base, redemption, title, ledger  # noqa: F401 注册所有表

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_ledger_entry_roundtrip(session):
    from app.models.ledger import LedgerEntry, LEDGER_ENTRY_TYPES
    from sqlalchemy import select

    assert "borrow" in LEDGER_ENTRY_TYPES
    assert "admin_adjust_cash" in LEDGER_ENTRY_TYPES

    e = LedgerEntry(
        user_id=1,
        entry_type="borrow",
        cash_delta=Decimal("100.000000"),
        debt_delta=Decimal("100.000000"),
        cash_after=Decimal("600.000000"),
        debt_after=Decimal("100.000000"),
        debt_last_accrued_at_after=datetime(2026, 5, 30, tzinfo=timezone.utc),
        daily_rate_at_event=Decimal("0.01000000"),
        operator_user_id=None,
        reason=None,
    )
    session.add(e)
    await session.commit()

    row = (await session.execute(select(LedgerEntry))).scalars().first()
    assert row.entry_type == "borrow"
    assert row.cash_delta == Decimal("100.000000")
    assert row.debt_after == Decimal("100.000000")
    assert row.daily_rate_at_event == Decimal("0.01000000")
    assert row.id is not None
    assert row.created_at is not None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && venv/bin/python -m pytest tests/test_ledger_model.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.ledger'`

- [ ] **Step 3: 写模型**

```python
# backend/app/models/ledger.py
"""资金流水账本。

补齐借/还/管理员调账这些原本无历史记录的 cash/debt 变动。
利息不在此表（确定性可由公式 + 快照重算）；buy/sell/结算/强平/兑换/弹幕
各有自己的表，也不在此。详见 docs/superpowers/specs/2026-05-30-loan-ledger-design.md。
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, DateTime, Numeric, ForeignKey, Index


# 允许的 entry_type 取值（应用层收敛，不加 DB enum 约束，沿用 Transaction.type 惯例）
LEDGER_ENTRY_TYPES = frozenset({
    "borrow",              # 用户借款
    "repay",               # 用户还款
    "admin_adjust_cash",   # 管理员调现金
    "admin_force_loan",    # 管理员强制放贷
    "admin_forgive_debt",  # 管理员免债
})


class LedgerEntry(SQLModel, table=True):
    __tablename__ = "ledger_entry"
    __table_args__ = (
        Index("ix_ledger_entry_user_created", "user_id", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)

    entry_type: str = Field(max_length=32)

    # 本次变动（+/−，可为 0）
    cash_delta: Decimal = Field(sa_type=Numeric(16, 6))
    debt_delta: Decimal = Field(sa_type=Numeric(16, 6))

    # 操作后快照（重建锚点）
    cash_after: Decimal = Field(sa_type=Numeric(16, 6))
    debt_after: Decimal = Field(sa_type=Numeric(16, 6))

    # 利息重算锚点 + 当时利率
    debt_last_accrued_at_after: Optional[datetime] = Field(
        default=None, sa_type=DateTime(timezone=True),
    )
    daily_rate_at_event: Optional[Decimal] = Field(
        default=None, sa_type=Numeric(16, 8),
    )

    # 管理员操作时记操作者；用户自助为 None
    operator_user_id: Optional[int] = Field(
        default=None,
        sa_column=Column(ForeignKey("user.id"), nullable=True),
    )
    reason: Optional[str] = Field(default=None, max_length=200)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
        index=True,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && venv/bin/python -m pytest tests/test_ledger_model.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add backend/app/models/ledger.py backend/tests/test_ledger_model.py
git commit -m "feat(ledger): LedgerEntry 模型 + 类型常量"
```

---

### Task 2: ledger_service.record_entry 写入助手

**Files:**
- Create: `backend/app/services/ledger_service.py`
- Test: `backend/tests/test_ledger_service.py`

写入助手从已变动的 user 对象读取 `cash_after`/`debt_after`/`debt_last_accrued_at_after` 快照，调用方只传 delta、type、rate、operator、reason。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_ledger_service.py
import pytest
import pytest_asyncio
from decimal import Decimal
from datetime import datetime, timezone


@pytest_asyncio.fixture
async def session_and_user(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlmodel import SQLModel
    from app.models import base, redemption, title, ledger  # noqa: F401
    from app.models.base import User

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        u = User(username="u1", cash=Decimal("600"), debt=Decimal("100"),
                 debt_last_accrued_at=datetime(2026, 5, 30, tzinfo=timezone.utc))
        s.add(u)
        await s.commit()
        await s.refresh(u)
        yield s, u
    await engine.dispose()


@pytest.mark.asyncio
async def test_record_entry_reads_snapshot_from_user(session_and_user):
    from app.services.ledger_service import record_entry
    from app.models.ledger import LedgerEntry
    from sqlalchemy import select

    s, u = session_and_user
    record_entry(
        s, user=u, entry_type="borrow",
        cash_delta=Decimal("100"), debt_delta=Decimal("100"),
        daily_rate=Decimal("0.01"),
    )
    await s.commit()

    row = (await s.execute(select(LedgerEntry))).scalars().first()
    assert row.entry_type == "borrow"
    assert row.cash_after == Decimal("600.000000")    # 从 user 快照读
    assert row.debt_after == Decimal("100.000000")
    assert row.daily_rate_at_event == Decimal("0.01000000")
    assert row.operator_user_id is None


@pytest.mark.asyncio
async def test_record_entry_rejects_bad_type(session_and_user):
    from app.services.ledger_service import record_entry
    s, u = session_and_user
    with pytest.raises(ValueError):
        record_entry(s, user=u, entry_type="nonsense",
                     cash_delta=Decimal("0"), debt_delta=Decimal("0"),
                     daily_rate=None)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && venv/bin/python -m pytest tests/test_ledger_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ledger_service'`

- [ ] **Step 3: 写助手**

```python
# backend/app/services/ledger_service.py
"""LedgerEntry 写入助手。

调用方负责事务边界（与资金变动同事务）。快照字段从已变动的 user 对象读取，
所以调用前 user.cash/debt/debt_last_accrued_at 必须已是操作后的最终值。
"""
from __future__ import annotations
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import User
from app.models.ledger import LedgerEntry, LEDGER_ENTRY_TYPES


def record_entry(
    session: AsyncSession,
    *,
    user: User,
    entry_type: str,
    cash_delta: Decimal,
    debt_delta: Decimal,
    daily_rate: Optional[Decimal],
    operator_user_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> LedgerEntry:
    """构造并 add 一条 LedgerEntry。不 commit（调用方负责）。

    快照（cash_after/debt_after/debt_last_accrued_at_after）从 user 对象当前值读，
    因此必须在 user 资金已变动之后调用。
    """
    if entry_type not in LEDGER_ENTRY_TYPES:
        raise ValueError(f"unknown ledger entry_type: {entry_type}")
    entry = LedgerEntry(
        user_id=user.id,
        entry_type=entry_type,
        cash_delta=cash_delta,
        debt_delta=debt_delta,
        cash_after=user.cash,
        debt_after=user.debt,
        debt_last_accrued_at_after=user.debt_last_accrued_at,
        daily_rate_at_event=daily_rate,
        operator_user_id=operator_user_id,
        reason=reason,
    )
    session.add(entry)
    return entry
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && venv/bin/python -m pytest tests/test_ledger_service.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add backend/app/services/ledger_service.py backend/tests/test_ledger_service.py
git commit -m "feat(ledger): record_entry 写入助手"
```

---

### Task 3: loan_service 借/还写账本

**Files:**
- Modify: `backend/app/services/loan_service.py`
- Test: `backend/tests/test_ledger_loan_service.py`

给**公开加锁包装** `increase_debt` 和 `decrease_debt` 加 `source` / `operator_user_id` / `reason` 参数，在资金变动后调 `record_entry`。**不动 `decrease_debt_locked`**（被 liquidation 直接调用，liquidation 有自己的 LiquidationEvent，不能重复记账）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_ledger_loan_service.py
import pytest
import pytest_asyncio
from decimal import Decimal
from datetime import datetime, timezone


@pytest_asyncio.fixture
async def session_and_user(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlmodel import SQLModel
    from app.models import base, redemption, title, ledger  # noqa: F401
    from app.models.base import User

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        u = User(username="u1", cash=Decimal("1000"), debt=Decimal("0"))
        s.add(u)
        await s.commit()
        await s.refresh(u)
        uid = u.id
    yield maker, uid
    await engine.dispose()


@pytest.mark.asyncio
async def test_increase_debt_writes_borrow_ledger(session_and_user):
    from app.services import loan_service
    from app.models.ledger import LedgerEntry
    from sqlalchemy import select

    maker, uid = session_and_user
    async with maker() as s:
        await loan_service.increase_debt(
            s, uid, Decimal("200"), grant_cash=True, daily_rate=Decimal("0.01"),
            source="borrow", operator_user_id=None,
        )
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(LedgerEntry))).scalars().first()
        assert row.entry_type == "borrow"
        assert row.debt_delta == Decimal("200.000000")
        assert row.cash_delta == Decimal("200.000000")
        assert row.debt_after == Decimal("200.000000")
        assert row.cash_after == Decimal("1200.000000")
        assert row.daily_rate_at_event == Decimal("0.01000000")


@pytest.mark.asyncio
async def test_decrease_debt_writes_repay_ledger(session_and_user):
    from app.services import loan_service
    from app.models.ledger import LedgerEntry
    from sqlalchemy import select

    maker, uid = session_and_user
    async with maker() as s:
        await loan_service.increase_debt(
            s, uid, Decimal("200"), grant_cash=True, daily_rate=Decimal("0"),
            source="borrow", operator_user_id=None,
        )
        await s.commit()
    async with maker() as s:
        await loan_service.decrease_debt(
            s, uid, Decimal("50"), consume_cash=True, daily_rate=Decimal("0"),
            source="repay", operator_user_id=None,
        )
        await s.commit()
    async with maker() as s:
        rows = (await s.execute(
            select(LedgerEntry).order_by(LedgerEntry.id)
        )).scalars().all()
        assert len(rows) == 2
        repay = rows[1]
        assert repay.entry_type == "repay"
        assert repay.debt_delta == Decimal("-50.000000")
        assert repay.cash_delta == Decimal("-50.000000")
        assert repay.debt_after == Decimal("150.000000")


@pytest.mark.asyncio
async def test_decrease_debt_locked_does_NOT_write_ledger(session_and_user):
    """liquidation 直接调 decrease_debt_locked，不应产生 ledger 行（它有 LiquidationEvent）。"""
    from app.services import loan_service
    from app.models.ledger import LedgerEntry
    from app.models.base import User
    from sqlalchemy import select

    maker, uid = session_and_user
    async with maker() as s:
        await loan_service.increase_debt(
            s, uid, Decimal("200"), grant_cash=True, daily_rate=Decimal("0"),
            source="borrow", operator_user_id=None,
        )
        await s.commit()
    async with maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalars().first()
        await loan_service.decrease_debt_locked(
            s, u, Decimal("30"), consume_cash=True, daily_rate=Decimal("0"),
        )
        await s.commit()
    async with maker() as s:
        rows = (await s.execute(select(LedgerEntry))).scalars().all()
        # 只有 borrow 那一条，decrease_debt_locked 不写
        assert len(rows) == 1
        assert rows[0].entry_type == "borrow"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && venv/bin/python -m pytest tests/test_ledger_loan_service.py -v`
Expected: FAIL — `increase_debt() got an unexpected keyword argument 'source'`

- [ ] **Step 3: 改 loan_service**

在 `backend/app/services/loan_service.py` 顶部 import 区加：

```python
from app.services import ledger_service
```

把 `increase_debt` 函数签名与函数体改为（在 `session.add(u)` 之前插入 ledger 写入）：

```python
async def increase_debt(
    session: AsyncSession,
    user_id: int,
    amount: Decimal,
    *,
    grant_cash: bool,
    daily_rate: Decimal,
    source: str,
    operator_user_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> User:
    """SELECT FOR UPDATE user → accrue → debt += amount；grant_cash=True 时 cash += amount。
    调用方负责 commit。amount 必须 > 0，否则 ValueError。

    source: ledger entry_type（"borrow" / "admin_force_loan"）。
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    u = result.scalar_one()
    now = _compat_now(u)
    accrue_interest(u, daily_rate, now)
    u.debt = (u.debt + amount).quantize(_QUANT)
    if u.debt_last_accrued_at is None:
        u.debt_last_accrued_at = now
    if grant_cash:
        u.cash = (u.cash + amount).quantize(_QUANT)
    # 防御性兜底：debt/cash 不应出现负值
    if u.debt < 0 or u.cash < 0:
        raise LoanServiceError(f"invariant violated post-increase: debt={u.debt} cash={u.cash}")
    ledger_service.record_entry(
        session, user=u, entry_type=source,
        cash_delta=(amount if grant_cash else Decimal("0")),
        debt_delta=amount,
        daily_rate=daily_rate,
        operator_user_id=operator_user_id,
        reason=reason,
    )
    session.add(u)
    return u
```

把 `decrease_debt`（公开加锁版，**不是** `_locked`）改为：

```python
async def decrease_debt(
    session: AsyncSession,
    user_id: int,
    amount: Decimal,
    *,
    consume_cash: bool,
    daily_rate: Decimal,
    source: str,
    operator_user_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> tuple[User, Decimal]:
    """SELECT FOR UPDATE user → accrue → effective 扣减 → 写 ledger。

    source: ledger entry_type（"repay" / "admin_forgive_debt"）。
    返回 (user, effective_amount)。
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    u = result.scalar_one()
    effective = await decrease_debt_locked(
        session, u, amount,
        consume_cash=consume_cash, daily_rate=daily_rate,
    )
    if effective > 0:
        ledger_service.record_entry(
            session, user=u, entry_type=source,
            cash_delta=(-effective if consume_cash else Decimal("0")),
            debt_delta=-effective,
            daily_rate=daily_rate,
            operator_user_id=operator_user_id,
            reason=reason,
        )
    return u, effective
```

确认顶部已有 `from typing import Optional`（如无则加；同文件已用 `Optional` 则跳过）。检查：

```bash
cd backend && grep -n "from typing import" app/services/loan_service.py
```
若没有 `Optional`，在 import 区加 `from typing import Optional`。

> **不要改** `decrease_debt_locked` —— 它被 liquidation_service 直接调用，liquidation 已有 LiquidationEvent，不能在此写 ledger。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && venv/bin/python -m pytest tests/test_ledger_loan_service.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add backend/app/services/loan_service.py backend/tests/test_ledger_loan_service.py
git commit -m "feat(ledger): loan_service 借/还写账本（decrease_debt_locked 不写）"
```

---

### Task 4: 更新 loan_service 的 4 个调用点

`increase_debt` / `decrease_debt` 新增了必填的 `source` 参数，所有调用点必须更新，否则现有 loan/admin 测试会失败。

**Files:**
- Modify: `backend/app/api/v1/loan.py`（borrow:81、repay:115）
- Modify: `backend/app/api/v1/user.py`（force-loan:680、forgive-debt:709）

- [ ] **Step 1: 改 loan.py borrow 调用（约 81 行）**

把：
```python
    u = await loan_service.increase_debt(
        db, user.id, amount, grant_cash=True, daily_rate=rate,
    )
```
改为：
```python
    u = await loan_service.increase_debt(
        db, user.id, amount, grant_cash=True, daily_rate=rate,
        source="borrow", operator_user_id=None,
    )
```

- [ ] **Step 2: 改 loan.py repay 调用（约 115 行）**

把：
```python
        u, effective = await loan_service.decrease_debt(
            db, user.id, amount, consume_cash=True, daily_rate=rate,
        )
```
改为：
```python
        u, effective = await loan_service.decrease_debt(
            db, user.id, amount, consume_cash=True, daily_rate=rate,
            source="repay", operator_user_id=None,
        )
```

- [ ] **Step 3: 改 user.py force-loan 调用（约 680 行）**

把：
```python
        u = await _loan_service.increase_debt(
            db, user_id, Decimal(req.amount), grant_cash=True, daily_rate=rate,
        )
```
改为：
```python
        u = await _loan_service.increase_debt(
            db, user_id, Decimal(req.amount), grant_cash=True, daily_rate=rate,
            source="admin_force_loan", operator_user_id=admin.id, reason=req.reason,
        )
```

- [ ] **Step 4: 改 user.py forgive-debt 调用（约 709 行）**

把：
```python
        u, effective = await _loan_service.decrease_debt(
            db, user_id, Decimal(req.amount), consume_cash=False, daily_rate=rate,
        )
```
改为：
```python
        u, effective = await _loan_service.decrease_debt(
            db, user_id, Decimal(req.amount), consume_cash=False, daily_rate=rate,
            source="admin_forgive_debt", operator_user_id=admin.id, reason=req.reason,
        )
```

- [ ] **Step 5: 确认无遗漏调用点**

Run: `cd backend && grep -rn "increase_debt\|decrease_debt(" app/ | grep -v "decrease_debt_locked\|def "`
Expected: 上面 4 处都带 `source=`；`liquidation_service` 不出现（它用 `decrease_debt_locked`）。

- [ ] **Step 6: 跑既有 loan/admin 测试确认未回归**

Run: `cd backend && venv/bin/python -m pytest tests/test_loan_api.py tests/test_loan_admin.py tests/test_loan_sweep.py -v`
Expected: 全 PASS（现有断言不变；新增的 ledger 行不影响原返回值）

- [ ] **Step 7: 提交**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add backend/app/api/v1/loan.py backend/app/api/v1/user.py
git commit -m "feat(ledger): 借/还/强制放贷/免债 调用点传 source"
```

---

### Task 5: adjust-cash 写账本

管理员调现金不走 loan_service，需在 handler 内直接写 ledger（debt 不变，cash 变）。

**Files:**
- Modify: `backend/app/api/v1/user.py`（adjust-cash handler，约 312-336 行）
- Test: `backend/tests/test_ledger_adjust_cash.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_ledger_adjust_cash.py
import pytest
import pytest_asyncio
from decimal import Decimal
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_async_session
from app.core.users import current_superuser


@pytest_asyncio.fixture
async def client_ctx(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlmodel import SQLModel
    from app.models import base, redemption, title, ledger  # noqa: F401
    from app.models.base import User

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        u1 = User(username="u1", cash=Decimal("500"), debt=Decimal("0"))
        admin = User(username="admin", cash=Decimal("0"), debt=Decimal("0"), is_superuser=True)
        s.add(u1); s.add(admin)
        await s.commit(); await s.refresh(u1); await s.refresh(admin)
        u1_id, admin_id, admin_obj = u1.id, admin.id, admin

    async def _sess():
        async with maker() as s:
            yield s
    app.dependency_overrides[get_async_session] = _sess
    app.dependency_overrides[current_superuser] = lambda: admin_obj

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, maker, u1_id, admin_id
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_adjust_cash_writes_ledger(client_ctx):
    from app.models.ledger import LedgerEntry
    from sqlalchemy import select

    ac, maker, u1_id, admin_id = client_ctx
    r = await ac.post(f"/api/v1/user/{u1_id}/adjust-cash",
                      json={"amount": "100", "reason": "活动奖励"})
    assert r.status_code == 200

    async with maker() as s:
        row = (await s.execute(select(LedgerEntry))).scalars().first()
        assert row is not None
        assert row.entry_type == "admin_adjust_cash"
        assert row.cash_delta == Decimal("100.000000")
        assert row.debt_delta == Decimal("0.000000")
        assert row.cash_after == Decimal("600.000000")
        assert row.operator_user_id == admin_id
        assert row.reason == "活动奖励"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && venv/bin/python -m pytest tests/test_ledger_adjust_cash.py -v`
Expected: FAIL — 查不到 LedgerEntry 行（`row is None`）

- [ ] **Step 3: 改 adjust-cash handler**

在 `backend/app/api/v1/user.py` 顶部 import 区加：
```python
from app.services import ledger_service as _ledger_service
```

把 adjust-cash handler 的事务块（`async with managed_transaction(db):` 内）改为在设置 `user.cash` 之后写 ledger：

```python
    async with managed_transaction(db):
        result = await db.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
        user = result.scalars().first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        new_cash = user.cash + req.amount
        if new_cash < 0:
            raise HTTPException(status_code=400, detail=f"操作后现金为 {new_cash}，不能为负")

        user.cash = new_cash
        _ledger_service.record_entry(
            db, user=user, entry_type="admin_adjust_cash",
            cash_delta=req.amount, debt_delta=Decimal("0"),
            daily_rate=None,
            operator_user_id=admin.id, reason=req.reason,
        )
```

> `record_entry` 读 `user.cash`（已 = new_cash）作 `cash_after`，正确。ledger 写入与现金变动同在 `managed_transaction` 块内，原子。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && venv/bin/python -m pytest tests/test_ledger_adjust_cash.py -v`
Expected: PASS

- [ ] **Step 5: 跑既有 user 测试确认未回归**

Run: `cd backend && venv/bin/python -m pytest tests/test_user_admin.py -v 2>/dev/null || venv/bin/python -m pytest tests/ -k "adjust" -v`
Expected: PASS（若无对应文件，第二条按 keyword 跑）

- [ ] **Step 6: 提交**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add backend/app/api/v1/user.py backend/tests/test_ledger_adjust_cash.py
git commit -m "feat(ledger): adjust-cash 写账本"
```

---

### Task 6: 模型注册 + alembic migration

让 `SQLModel.metadata` 认得新表，并生成迁移。

**Files:**
- Modify: `backend/init_db.py`（加 ledger import）
- Modify: `backend/alembic/env.py`（确认 import 了 ledger 模型）
- Create: `backend/alembic/versions/<autogen>.py`

- [ ] **Step 1: init_db.py 注册 ledger**

确认 `backend/init_db.py` 顶部已有这一组 model import（约 8-10 行）：
```python
from app.models import base  # noqa: F401 确保 SQLModel.metadata 注册所有表
from app.models import redemption  # noqa
from app.models import title  # noqa
```
在其后加一行：
```python
from app.models import ledger  # noqa
```

- [ ] **Step 2: alembic/env.py 注册 ledger**

Run: `cd backend && grep -n "from app.models\|import.*models\|target_metadata" alembic/env.py`

确认 env.py 里有引入模型让 metadata 完整（通常是 `from app.models import base, redemption, title` 或 import app.models）。若 ledger 未被传递性导入，在对应 import 行补上 `ledger`，例如把 `from app.models import base, redemption, title` 改为 `from app.models import base, redemption, title, ledger`。

- [ ] **Step 3: 生成迁移**

Run:
```bash
cd backend && venv/bin/alembic revision --autogenerate -m "add ledger_entry table"
```
Expected: 在 `backend/alembic/versions/` 生成一个新文件，内含 `op.create_table("ledger_entry", ...)`。

- [ ] **Step 4: 人工 review 生成的迁移**

打开新生成的 `backend/alembic/versions/<hash>_add_ledger_entry_table.py`，确认：
- `upgrade()` 只有 `create_table("ledger_entry", ...)` + 建索引（`ix_ledger_entry_user_created`、user_id/created_at 索引），**没有**对其它表的意外 drop/alter。
- 字段类型与 `ledger.py` 一致（Numeric(16,6) / Numeric(16,8) / DateTime(timezone=True)）。
- `downgrade()` 是 `op.drop_table("ledger_entry")`。
- 若 autogen 多带了无关改动（例如对既有表的 server_default 调整），删掉那些行，只保留 ledger_entry 相关。

- [ ] **Step 5: 本地试跑迁移（用临时 sqlite，不碰任何真实库）**

Run:
```bash
cd backend && DB_BACKEND=sqlite SQLITE_PATH=/tmp/ledger_mig_test.db venv/bin/alembic upgrade head && echo "UPGRADE_OK"
```
Expected: 末尾打印 `UPGRADE_OK`，无报错。

验证表与列：
```bash
cd backend && venv/bin/python -c "
import sqlite3
c=sqlite3.connect('/tmp/ledger_mig_test.db')
cols=[r[1] for r in c.execute('PRAGMA table_info(ledger_entry)')]
print('cols:', cols)
assert 'cash_after' in cols and 'daily_rate_at_event' in cols and 'operator_user_id' in cols
print('LEDGER_TABLE_OK')
"
rm -f /tmp/ledger_mig_test.db
```
Expected: 打印 `LEDGER_TABLE_OK`。

> 注意：**不要**在任何真实 DB（dev/prod）上跑 upgrade。这一步只用 `/tmp` 临时 sqlite 验证迁移语法正确。

- [ ] **Step 6: 提交（模型注册 + 迁移一起进，原子）**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add backend/init_db.py backend/alembic/env.py backend/alembic/versions/
git commit -m "feat(ledger): 注册模型 + alembic 迁移 add ledger_entry table"
```

---

### Task 7: 全量验证

**Files:** 无（只跑验证）

- [ ] **Step 1: 后端编译**

Run: `cd backend && venv/bin/python -m py_compile $(find app -name '*.py')`
Expected: 无输出（成功）

- [ ] **Step 2: app 可导入**

Run: `cd backend && venv/bin/python -c "import app.main; print('IMPORT_OK')"`
Expected: 打印 `IMPORT_OK`

- [ ] **Step 3: 全量 pytest**

Run: `cd backend && venv/bin/python -m pytest -x -q`
Expected: 全 PASS（含新增 4 个 ledger 测试 + 既有全部）。若有 1 个 memory 里记录的"已知过期 fail"，确认是同一个、与本次改动无关即可。

- [ ] **Step 4: 确认 buy/sell hot path 与 liquidation 未被改动**

Run: `cd /data/sunyunbo/www/TouhouCCB && git diff main --stat -- backend/app/api/v1/market.py backend/app/services/liquidation_service.py backend/app/services/loan_service.py`
Expected: `market.py` 和 `liquidation_service.py` **0 改动**；只有 `loan_service.py` 有改动。

- [ ] **Step 5: 合并前留在分支，等人工 review**

不在此步 merge/push。实施完成后由 subagent-driven 流程或用户决定合并。

---

## 自检记录（writing-plans self-review）

- **Spec 覆盖**：LedgerEntry 表（T1）✓ / record_entry 助手（T2）✓ / 借·还写入 + decrease_debt_locked 不写（T3）✓ / 5 类事件全覆盖：borrow·repay（T3+T4）、admin_force_loan·admin_forgive_debt（T4）、admin_adjust_cash（T5）✓ / 利息不落行（设计即如此，无写入点）✓ / alembic（T6）✓ / 同事务原子（T3/T5 写入都在调用方事务内）✓ / 不碰 hot path（T7 Step4 验证）✓。
- **占位符**：无 TBD/TODO，所有代码步给出完整代码。
- **类型一致**：`record_entry` 签名在 T2 定义，T3/T5 调用参数一致（`user=`、`entry_type=`、`cash_delta=`、`debt_delta=`、`daily_rate=`、`operator_user_id=`、`reason=`）；`increase_debt`/`decrease_debt` 新签名在 T3 定义，T4 调用一致（`source=`、`operator_user_id=`、`reason=`）；`LEDGER_ENTRY_TYPES` 取值与各处 entry_type 字符串一致。
