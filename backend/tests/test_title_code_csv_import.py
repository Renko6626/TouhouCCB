import pytest, uuid
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
        assert cnt == 0


@pytest.mark.asyncio
async def test_import_conflict_with_existing_rejected(client):
    _, bid, h = await _mk_admin()
    await client.post(f"/api/v1/admin/title-batches/{bid}/import-codes",
        files={"file": ("c.csv", _csv("EXIST-001"), "text/csv")}, headers=h)
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
        assert cnt == 0


@pytest.mark.asyncio
async def test_import_hardcap_5000(client):
    _, bid, h = await _mk_admin()
    codes = [f"BULK-{i:05d}" for i in range(5001)]
    files = {"file": ("c.csv", _csv(*codes), "text/csv")}
    r = await client.post(f"/api/v1/admin/title-batches/{bid}/import-codes",
                          files=files, headers=h)
    assert r.status_code == 400
