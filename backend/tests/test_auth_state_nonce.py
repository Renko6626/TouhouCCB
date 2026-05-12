# state / nonce / redirect_uri 强校验测试
# 覆盖 P0-AUTH-02/03 修复（SSO 登录 CSRF 防护 + redirect_uri 注入防护）
# 这些测试仅测请求验证层，不涉及真实的 Casdoor / OIDC 调用。

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from asgi_lifespan import LifespanManager
from sqlmodel import SQLModel
from app.main import app
from app.core.database import engine


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


@pytest_asyncio.fixture
async def client():
    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://localhost:8004",
        ) as ac:
            yield ac


# ── 1. login-start 正常返回 state / nonce + 写 HttpOnly cookie ──────────────

@pytest.mark.asyncio
async def test_login_start_returns_state_and_nonce_with_cookies(client):
    """POST /login-start 必须返回随机 state & nonce 并写 HttpOnly cookie。"""
    r = await client.post("/api/v1/auth/login-start")
    assert r.status_code == 200

    data = r.json()
    assert "state" in data, "响应体缺少 state"
    assert "nonce" in data, "响应体缺少 nonce"

    # token_urlsafe(32) 输出约 43 字符 URL-safe base64；>= 32 已够随机
    assert len(data["state"]) >= 32, f"state 长度不足: {len(data['state'])}"
    assert len(data["nonce"]) >= 32, f"nonce 长度不足: {len(data['nonce'])}"

    # 检查 Set-Cookie 头（httpx 的 r.cookies 不总保留原始属性）
    set_cookie_headers = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else [
        v for k, v in r.headers.items() if k.lower() == "set-cookie"
    ]
    # 至少应有两条 Set-Cookie
    assert len(set_cookie_headers) >= 2, "应有两条 Set-Cookie（state + nonce）"

    names_in_headers = " ".join(set_cookie_headers)
    assert "thccb_oauth_state" in names_in_headers, "thccb_oauth_state cookie 未出现在 Set-Cookie 头"
    assert "thccb_oauth_nonce" in names_in_headers, "thccb_oauth_nonce cookie 未出现在 Set-Cookie 头"

    # HttpOnly 属性
    for h in set_cookie_headers:
        if "thccb_oauth_state" in h or "thccb_oauth_nonce" in h:
            assert "httponly" in h.lower(), f"cookie 应带 HttpOnly: {h}"


# ── 2. callback 无 state cookie → 400 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_callback_rejects_when_no_state_cookie(client):
    """没有先调 login-start 就直接 POST callback → 缺少 state cookie → 400。"""
    r = await client.post(
        "/api/v1/auth/callback",
        json={"code": "fake_code", "state": "some_state_value"},
    )
    assert r.status_code == 400
    detail = r.json().get("detail", "")
    assert "state" in detail, f"错误详情应提及 state，实际: {detail!r}"


# ── 3. callback state 不匹配 → 400 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_callback_rejects_state_mismatch(client):
    """login-start 写入的 state 与 callback 提交的 state 不一致 → 400。"""
    # 先获取真实 state/nonce cookie
    start = await client.post("/api/v1/auth/login-start")
    assert start.status_code == 200

    # 故意提交错误的 state
    r = await client.post(
        "/api/v1/auth/callback",
        json={"code": "fake_code", "state": "wrong_state_xxx"},
    )
    assert r.status_code == 400
    detail = r.json().get("detail", "")
    assert "state" in detail, f"错误详情应提及 state，实际: {detail!r}"


# ── 4. callback 缺少 state 字段 → 422（Pydantic 必填校验）───────────────────

@pytest.mark.asyncio
async def test_callback_request_state_is_required(client):
    """CallbackRequest.state 是必填字段；省略时 FastAPI 返回 422。"""
    r = await client.post(
        "/api/v1/auth/callback",
        json={"code": "fake_code"},  # 无 state
    )
    assert r.status_code == 422


# ── 5. callback 带额外 redirect_uri 字段 → Pydantic 忽略，仍因 state 失败 ──

@pytest.mark.asyncio
async def test_callback_extra_redirect_uri_is_ignored(client):
    """
    恶意客户端在请求体中带 redirect_uri 字段：
    - Pydantic 应忽略（CallbackRequest 未声明该字段）
    - 请求仍因无效 state cookie 返回 400（而非 redirect_uri 相关错误）
    """
    r = await client.post(
        "/api/v1/auth/callback",
        json={
            "code": "fake_code",
            "state": "anything",
            "redirect_uri": "https://evil.com/steal",
        },
    )
    assert r.status_code == 400
    detail = r.json().get("detail", "")
    # 应是 state 校验失败，而非 redirect_uri 相关错误
    assert "state" in detail, f"应因 state 失败，实际: {detail!r}"
    assert "redirect" not in detail.lower(), f"redirect_uri 不应被处理，实际: {detail!r}"
