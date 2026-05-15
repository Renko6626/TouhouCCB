"""批量调整现金接口测试 —— 直接调 app endpoint 验证 admin auth + filter + 安全围栏。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User


async def _seed_user(
    cash: Decimal = Decimal("100"),
    debt: Decimal = Decimal("0"),
    is_superuser: bool = False,
    is_active: bool = True,
) -> int:
    """种用户，只返回 id。"""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as s:
        u = User(
            username=f"u_{suffix}",
            email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}",
            cash=cash,
            debt=debt,
            is_superuser=is_superuser,
            is_active=is_active,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id


async def _seed_user_with_headers(**kwargs):
    uid = await _seed_user(**kwargs)
    token = create_access_token(uid)
    return uid, {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def admin_headers(client):
    _, h = await _seed_user_with_headers(cash=Decimal("0"), is_superuser=True)
    return h


@pytest_asyncio.fixture
async def normal_headers(client):
    _, h = await _seed_user_with_headers(cash=Decimal("100"), is_superuser=False)
    return h


@pytest.mark.asyncio
async def test_dry_run_returns_preview(client, admin_headers):
    u1 = await _seed_user(cash=Decimal("100"))
    u2 = await _seed_user(cash=Decimal("200"))

    resp = await client.post(
        "/api/v1/user/batch-adjust-cash",
        headers=admin_headers,
        json={
            "filter": {"user_id_min": u1, "user_id_max": u2},
            "amount": "50",
            "reason": "活动福利",
            "dry_run": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dry_run"] is True
    assert data["matched_count"] == 2
    assert data["eligible_count"] == 2
    assert data["total_delta"] == 100.0
    # dry_run 不写入：实际 cash 不变
    async with async_session_maker() as s:
        u = await s.get(User, u1)
        assert u.cash == Decimal("100")


@pytest.mark.asyncio
async def test_execute_writes(client, admin_headers):
    u1 = await _seed_user(cash=Decimal("100"))
    u2 = await _seed_user(cash=Decimal("200"))

    resp = await client.post(
        "/api/v1/user/batch-adjust-cash",
        headers=admin_headers,
        json={
            "filter": {"user_id_min": u1, "user_id_max": u2},
            "amount": "50",
            "reason": "活动福利",
            "dry_run": False,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dry_run"] is False
    assert data["updated_count"] == 2
    assert data["failed_count"] == 0
    async with async_session_maker() as s:
        assert (await s.get(User, u1)).cash == Decimal("150.000000")
        assert (await s.get(User, u2)).cash == Decimal("250.000000")


@pytest.mark.asyncio
async def test_deduct_skips_when_would_go_negative(client, admin_headers):
    rich = await _seed_user(cash=Decimal("500"))
    poor = await _seed_user(cash=Decimal("10"))

    # 扣 100：rich 够扣，poor 不够 → poor 跳过
    resp = await client.post(
        "/api/v1/user/batch-adjust-cash",
        headers=admin_headers,
        json={
            "filter": {"user_id_min": rich, "user_id_max": poor},
            "amount": "-100",
            "reason": "回收",
            "dry_run": False,
        },
    )
    data = resp.json()
    assert data["updated_count"] == 1
    assert data["failed_count"] == 1
    assert data["failed"][0]["user_id"] == poor
    async with async_session_maker() as s:
        assert (await s.get(User, rich)).cash == Decimal("400.000000")
        assert (await s.get(User, poor)).cash == Decimal("10")  # 未变


@pytest.mark.asyncio
async def test_filter_cash_range(client, admin_headers):
    low = await _seed_user(cash=Decimal("50"))
    mid = await _seed_user(cash=Decimal("150"))
    high = await _seed_user(cash=Decimal("500"))

    # 只发 cash 在 [100, 300) 的用户（mid 命中）
    resp = await client.post(
        "/api/v1/user/batch-adjust-cash",
        headers=admin_headers,
        json={
            "filter": {"cash_min": "100", "cash_max": "299", "user_id_min": low, "user_id_max": high},
            "amount": "10",
            "reason": "test",
            "dry_run": False,
        },
    )
    data = resp.json()
    assert data["updated_count"] == 1
    async with async_session_maker() as s:
        assert (await s.get(User, mid)).cash == Decimal("160.000000")
        assert (await s.get(User, low)).cash == Decimal("50")
        assert (await s.get(User, high)).cash == Decimal("500")


@pytest.mark.asyncio
async def test_excludes_superuser_by_default(client, admin_headers):
    # admin 自己 + 一个普通用户都在 user_id 范围内；默认不影响超管
    normal = await _seed_user(cash=Decimal("100"))
    super_id = await _seed_user(cash=Decimal("100"), is_superuser=True)

    resp = await client.post(
        "/api/v1/user/batch-adjust-cash",
        headers=admin_headers,
        json={
            "filter": {"user_id_min": min(normal, super_id), "user_id_max": max(normal, super_id)},
            "amount": "50",
            "reason": "test",
            "dry_run": False,
        },
    )
    data = resp.json()
    # 只 normal 被影响
    assert data["updated_count"] == 1
    async with async_session_maker() as s:
        assert (await s.get(User, normal)).cash == Decimal("150.000000")
        assert (await s.get(User, super_id)).cash == Decimal("100")


@pytest.mark.asyncio
async def test_zero_amount_rejected(client, admin_headers):
    resp = await client.post(
        "/api/v1/user/batch-adjust-cash",
        headers=admin_headers,
        json={"filter": {}, "amount": "0", "reason": "test", "dry_run": True},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_normal_user_forbidden(client, normal_headers):
    resp = await client.post(
        "/api/v1/user/batch-adjust-cash",
        headers=normal_headers,
        json={"filter": {}, "amount": "10", "reason": "test", "dry_run": True},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_rejected(client):
    resp = await client.post(
        "/api/v1/user/batch-adjust-cash",
        json={"filter": {}, "amount": "10", "reason": "test", "dry_run": True},
    )
    # users 模块通常返 401（无 token）或 422（schema 校验）；这里只要不是 200
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_no_match_returns_empty(client, admin_headers):
    resp = await client.post(
        "/api/v1/user/batch-adjust-cash",
        headers=admin_headers,
        json={
            "filter": {"user_id_min": 999999, "user_id_max": 999999},
            "amount": "10",
            "reason": "test",
            "dry_run": False,
        },
    )
    data = resp.json()
    assert data["updated_count"] == 0
    assert data["failed_count"] == 0
