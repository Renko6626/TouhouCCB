"""WP2：影子 CLI 只读性 + 输出字段（子进程跑真实脚本，前后对比全库行快照）。"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import async_session_maker
from app.models.audit import AuditEvent
from app.models.base import Market, Outcome, Position, SiteConfig, User
from app.models.fx import FxPair, FxWallet

SCRIPT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "scripts", "credit_shadow_report.py",
))
TABLES = (User, Position, FxWallet, Market, Outcome, FxPair, SiteConfig, AuditEvent)


async def _snapshot(session):
    snap = {}
    for model in TABLES:
        pk = list(model.__table__.primary_key.columns)
        rows = (await session.execute(select(model).order_by(*pk))).scalars().all()
        snap[model.__tablename__] = [
            tuple(getattr(row, column.name) for column in model.__table__.columns)
            for row in rows
        ]
    return snap


async def _seed():
    async with async_session_maker() as session:
        session.add_all([
            SiteConfig(key="sell_fee_rate", value="0.01", value_type="decimal"),
            SiteConfig(key="loan_daily_rate", value="0.001", value_type="decimal"),
            SiteConfig(key="credit_leverage", value="11", value_type="decimal"),
            SiteConfig(key="credit_maintenance_ratio", value="0.04", value_type="decimal"),
        ])
        user = User(
            username="wp2-shadow", cash=Decimal("10"), debt=Decimal("500"),
            debt_last_accrued_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        market = Market(title="shadow-m", liquidity_b=100.0, status="trading")
        session.add_all([user, market])
        await session.flush()
        outcomes = [
            Outcome(market_id=market.id, label=f"o{i}", total_shares=Decimal(str(q)))
            for i, q in enumerate((120, 80))
        ]
        pair = FxPair(
            currency_code="SHD", currency_name="Shadow", status="trading",
            gold_reserve=Decimal("100"), foreign_reserve=Decimal("100"),
        )
        session.add_all(outcomes + [pair])
        await session.flush()
        session.add_all([
            Position(user_id=user.id, outcome_id=outcomes[0].id,
                     amount=Decimal("30"), cost_basis=Decimal("1")),
            Position(user_id=user.id, outcome_id=outcomes[1].id,
                     amount=Decimal("20"), cost_basis=Decimal("1")),
            FxWallet(user_id=user.id, pair_id=pair.id,
                     foreign_amount=Decimal("7"), cost_basis=Decimal("1")),
        ])
        await session.commit()
        return user.id


def _run_cli(*extra):
    env = dict(os.environ)
    env["DATABASE_URL"] = os.environ["DATABASE_URL"]
    return subprocess.run(
        [sys.executable, SCRIPT, *extra],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.asyncio
async def test_shadow_cli_outputs_reconciliation_and_writes_nothing():
    user_id = await _seed()
    async with async_session_maker() as session:
        before = await _snapshot(session)

    json_run = _run_cli("--format", "json", "--out", "-", "--limit", "10")
    assert json_run.returncode == 0, json_run.stderr
    report = json.loads(json_run.stdout)
    assert Decimal(report["meta"]["r_maintenance"]) == Decimal("0.04")
    assert report["summary"]["users"] >= 1

    row = next(item for item in report["rows"] if item["user_id"] == user_id)
    for field in (
        "old_lcv", "new_lmsr", "new_fx", "new_total", "diff", "ratio",
        "mtm_lmsr", "mtm_fx", "display_equity", "debt_effective",
        "liquidation_equity", "maintenance_breach",
    ):
        assert field in row, field
    assert Decimal(row["new_lmsr"]) > 0
    assert Decimal(row["new_fx"]) > 0
    assert Decimal(row["debt_effective"]) > Decimal("500")
    assert row["maintenance_breach"] is True
    assert Decimal(row["new_total"]) == Decimal(row["new_lmsr"]) + Decimal(row["new_fx"])

    md_run = _run_cli("--format", "md", "--out", "-", "--limit", "10")
    assert md_run.returncode == 0, md_run.stderr
    assert "## 差异来源" in md_run.stdout
    assert "| user |" in md_run.stdout

    async with async_session_maker() as session:
        after = await _snapshot(session)
    assert after == before


@pytest.mark.asyncio
async def test_shadow_cli_user_id_filter_and_out_file(tmp_path):
    user_id = await _seed()
    out_file = tmp_path / "shadow.md"
    run = _run_cli("--user-id", str(user_id), "--out", str(out_file))
    assert run.returncode == 0, run.stderr
    assert run.stdout == ""
    text = out_file.read_text(encoding="utf-8")
    assert "# 统一信贷影子对账（before）" in text
    assert f"| {user_id} |" in text
