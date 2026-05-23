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
            u = User(username=f"u_{suffix}", email=f"u{suffix}@t.com",
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
    async with async_session_maker() as s:
        ut = (await s.execute(select(UserTitle).where(
            UserTitle.user_id == uid, UserTitle.title_id == tid,
        ))).scalar_one_or_none()
        assert ut is not None
        assert ut.source == "code"
    async with async_session_maker() as s:
        c = (await s.execute(select(TitleCode).where(TitleCode.code_string == code))).scalar_one()
        assert c.status == "used"
        assert c.used_by_user_id == uid
        assert c.used_at is not None
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.equipped_title_id is None  # 不自动佩戴


@pytest.mark.asyncio
async def test_redeem_already_used(client):
    uid, tid, code, h = await _mk_setup()
    await client.post("/api/v1/title/redeem", json={"code": code}, headers=h)
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


@pytest.mark.asyncio
async def test_redeem_nonexistent_code(client):
    uid, tid, _, h = await _mk_setup()
    r = await client.post("/api/v1/title/redeem",
                          json={"code": "NO-SUCH-CODE-1234"}, headers=h)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_redeem_already_owned_title_code_not_consumed(client):
    uid, tid, code, h = await _mk_setup()
    async with async_session_maker() as s:
        async with s.begin():
            s.add(UserTitle(user_id=uid, title_id=tid, source="admin"))
    r = await client.post("/api/v1/title/redeem", json={"code": code}, headers=h)
    assert r.status_code == 403
    assert "已拥有" in r.json().get("detail", "")
    async with async_session_maker() as s:
        c = (await s.execute(select(TitleCode).where(TitleCode.code_string == code))).scalar_one()
        assert c.status == "available"
        assert c.used_by_user_id is None


@pytest.mark.asyncio
async def test_redeem_inactive_title_rejected(client):
    uid, tid, code, h = await _mk_setup()
    async with async_session_maker() as s:
        async with s.begin():
            t = await s.get(Title, tid)
            t.is_active = False
            s.add(t)
    r = await client.post("/api/v1/title/redeem", json={"code": code}, headers=h)
    assert r.status_code == 403
