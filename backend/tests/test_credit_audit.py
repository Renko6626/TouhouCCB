"""WP1：新审计类型、replay 折叠口径与 init_db metadata 注册（F10 / 计划 §3.5）。"""
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app.core.database import async_session_maker
from app.models.audit import AUDIT_EVENT_TYPES, AuditEvent
from app.models.base import User
from app.services import audit_replay, audit_service

BACKEND_DIR = Path(__file__).parents[1]

CREDIT_AUDIT_TYPES = (
    "liquidation_run_start",
    "liquidation_action",
    "liquidation_blocked",
    "liquidation_run_close",
    "credit_freeze_set",
)


def test_credit_audit_types_registered():
    for event_type in CREDIT_AUDIT_TYPES:
        assert event_type in AUDIT_EVENT_TYPES, event_type
    # 既有类型不得被改名（replay / 前端依赖）
    for legacy in ("trade_liquidate", "liquidation_repay", "liquidation", "interest_accrual"):
        assert legacy in AUDIT_EVENT_TYPES


@pytest.mark.asyncio
async def test_record_accepts_credit_events_and_rejects_unknown(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username="audit_credit", cash=Decimal("10"))
            s.add(u)
            await s.flush()
            ev = audit_service.record(
                s, "credit_freeze_set", user_id=u.id,
                payload={"frozen": True, "reason": "insolvent"},
                user_after=audit_service.user_snapshot(u),
            )
            assert ev.event_type == "credit_freeze_set"
            with pytest.raises(ValueError):
                audit_service.record(s, "credit_something_else", user_id=u.id)


async def _load_events():
    async with async_session_maker() as s:
        return await audit_replay.load_events(s)


@pytest.mark.asyncio
async def test_credit_events_after_money_events_fold_cleanly(setup_db):
    """§3.5：新事件的 user_after 是提交后权威快照，且必须排在资金事件之后。"""
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username="fold", cash=Decimal("100"), debt=Decimal("0"))
            s.add(u)
            await s.flush()
            uid = u.id
            audit_service.record(
                s, "user_register", user_id=u.id, payload={"username": u.username},
                user_after=audit_service.user_snapshot(u),
            )
            u.cash = Decimal("150")
            u.debt = Decimal("50")
            u.debt_last_accrued_at = None
            audit_service.record(
                s, "loan_borrow", user_id=u.id,
                payload={"cash_delta": "50", "debt_delta": "50", "interest_accrued": "0"},
                user_after=audit_service.user_snapshot(u),
            )
            # 资金事件（liquidation_repay）先写
            u.cash = Decimal("140")
            u.debt = Decimal("40")
            audit_service.record(
                s, "liquidation_repay", user_id=u.id,
                payload={"repaid": "10", "interest_accrued": "0"},
                user_after=audit_service.user_snapshot(u),
            )
            # 记录型信贷事件后写，携带同一权威快照
            audit_service.record(
                s, "liquidation_run_start", user_id=u.id, payload={"run_id": 1},
                user_after=audit_service.user_snapshot(u),
            )
            audit_service.record(
                s, "liquidation_action", user_id=u.id,
                payload={"run_id": 1, "round_no": 1, "kind": "repay_cash", "repaid": "10"},
                user_after=audit_service.user_snapshot(u),
            )
            audit_service.record(
                s, "liquidation_run_close", user_id=u.id, payload={"run_id": 1, "status": "recovered"},
                user_after=audit_service.user_snapshot(u),
            )

    snap, mism = audit_replay.fold(await _load_events(), check=True)
    assert mism == []
    assert snap.users[uid].cash == Decimal("140")
    assert snap.users[uid].debt == Decimal("40")


@pytest.mark.asyncio
async def test_credit_event_before_money_event_breaks_fold(setup_db):
    """反例：顺序写反会被 replay 自检抓到（提醒后续 WP 不要抢在资金事件前面写）。"""
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username="fold_bad", cash=Decimal("100"), debt=Decimal("0"))
            s.add(u)
            await s.flush()
            audit_service.record(
                s, "user_register", user_id=u.id, payload={"username": u.username},
                user_after=audit_service.user_snapshot(u),
            )
            u.cash = Decimal("150")
            u.debt = Decimal("50")
            # 先写记录型事件（user_after 已经是还款后的状态），再写资金事件 → 锚点错位
            u.cash = Decimal("140")
            u.debt = Decimal("40")
            audit_service.record(
                s, "liquidation_action", user_id=u.id, payload={"kind": "repay_cash"},
                user_after=audit_service.user_snapshot(u),
            )
            audit_service.record(
                s, "loan_borrow", user_id=u.id,
                payload={"cash_delta": "50", "debt_delta": "50", "interest_accrued": "0"},
                user_after=audit_service.user_snapshot(u),
            )

    _, mism = audit_replay.fold(await _load_events(), check=True)
    assert mism, "顺序错误必须被 replay 自检发现"


@pytest.mark.asyncio
async def test_credit_audit_events_are_not_preserved_by_season_reset():
    """赛季重置要清掉信贷审计（它们不在 PRESERVED_AUDIT_TYPES 里）。"""
    from scripts.season_reset import PRESERVED_AUDIT_TYPES

    for event_type in CREDIT_AUDIT_TYPES:
        assert event_type not in PRESERVED_AUDIT_TYPES


def test_init_db_registers_fx_bot_and_credit_tables(tmp_path):
    """F10 证据：`import init_db` 的 metadata 必须含 FX / bot / 统一信贷表。

    Controller 裁定：断言必需表集合，不用过期的 21/27 计数。
    """
    db_path = tmp_path / "initdb_metadata.db"
    code = (
        "import json; from sqlmodel import SQLModel; import init_db; "
        "print(json.dumps(sorted(SQLModel.metadata.tables)))"
    )
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PYTHONPATH": str(BACKEND_DIR),
    }
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND_DIR, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    tables = set(json.loads(result.stdout.strip().splitlines()[-1]))
    assert {
        "fx_pair", "fx_treasury", "fx_wallet", "fx_trade",
        "bot_profile", "liquidation_run", "liquidation_action",
    } <= tables
    # init_db 的既有注册不能被删掉
    assert {"user", "market", "outcome", "position", "siteconfig", "audit_event", "ledger_entry"} <= tables
