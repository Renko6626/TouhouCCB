"""admin_pve endpoints：权限 / 批量生成（ledger 记账）/ 列表 / 个体干预 / 注资复活 / 配置。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.bot import BotProfile
from app.models.ledger import LedgerEntry

BASE = "/api/v1/admin/pve"


async def _seed_user(superuser=False) -> int:
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"adm_{suffix}", casdoor_id=f"cd_{suffix}",
                     cash=Decimal("0"), is_superuser=superuser)
            s.add(u)
            await s.flush()
            return u.id


@pytest_asyncio.fixture
async def admin_headers(client):
    uid = await _seed_user(superuser=True)
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


@pytest.mark.asyncio
async def test_requires_superuser(client):
    uid = await _seed_user(superuser=False)
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    r = await client.get(f"{BASE}/bots", headers=h)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_generate_creates_users_profiles_and_ledger(client, admin_headers):
    r = await client.post(f"{BASE}/bots/generate", headers=admin_headers, json={
        "items": [{"template": "hodler", "count": 2}, {"template": "grid", "count": 1}],
        "naming_style": "npc",
        "initial_cash": "50",
    })
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert len(created) == 3
    assert all(c["username"].startswith("NPC·") for c in created)

    uids = [c["user_id"] for c in created]
    async with async_session_maker() as s:
        users = (await s.execute(select(User).where(User.id.in_(uids)))).scalars().all()
        assert all(u.is_bot and u.cash == Decimal("50") and u.casdoor_id is None for u in users)
        entries = (await s.execute(
            select(LedgerEntry).where(LedgerEntry.user_id.in_(uids))
        )).scalars().all()
        assert len(entries) == 3
        assert all(e.entry_type == "admin_adjust_cash" and e.cash_delta == Decimal("50") for e in entries)
        profiles = (await s.execute(select(BotProfile))).scalars().all()
        assert sorted(p.template for p in profiles) == ["grid", "hodler", "hodler"]
        # 生成时人格已扰动落库：注意力参数在 params 里
        assert all("check_interval_sec" in p.params for p in profiles)


@pytest.mark.asyncio
async def test_generate_unknown_template_400(client, admin_headers):
    r = await client.post(f"{BASE}/bots/generate", headers=admin_headers, json={
        "items": [{"template": "nope", "count": 1}], "initial_cash": "10",
    })
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_list_and_overview(client, admin_headers):
    await client.post(f"{BASE}/bots/generate", headers=admin_headers, json={
        "items": [{"template": "hodler", "count": 2}],
        "naming_style": "lowkey", "initial_cash": "30",
    })
    r = await client.get(f"{BASE}/bots", headers=admin_headers)
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 2
    assert all(it["cash"] == 30.0 and it["total_value"] == 30.0 for it in items)
    assert all(it["status"] == "active" for it in items)

    r2 = await client.get(f"{BASE}/overview", headers=admin_headers)
    body = r2.json()
    assert body["counts"]["active"] == 2
    assert body["enabled"] is False  # 默认关
    assert "hodler" in body["templates"]


@pytest.mark.asyncio
async def test_patch_bot(client, admin_headers):
    r = await client.post(f"{BASE}/bots/generate", headers=admin_headers, json={
        "items": [{"template": "hodler", "count": 1}], "initial_cash": "10",
    })
    pid = r.json()["created"][0]["profile_id"]

    r2 = await client.patch(f"{BASE}/bots/{pid}", headers=admin_headers, json={
        "status": "paused", "template": "grid",
        "params": {"levels": 4}, "market_scope": [1, 2],
    })
    assert r2.status_code == 200
    async with async_session_maker() as s:
        p = (await s.execute(select(BotProfile).where(BotProfile.id == pid))).scalars().one()
        assert p.status == "paused" and p.template == "grid"
        assert p.params == {"levels": 4} and p.market_scope == [1, 2]

    # 显式传 null 清空 market_scope
    r3 = await client.patch(f"{BASE}/bots/{pid}", headers=admin_headers, json={"market_scope": None})
    assert r3.status_code == 200 and "market_scope" in r3.json()["changes"]
    async with async_session_maker() as s:
        p = (await s.execute(select(BotProfile).where(BotProfile.id == pid))).scalars().one()
        assert p.market_scope is None

    assert (await client.patch(f"{BASE}/bots/{pid}", headers=admin_headers,
                               json={"template": "nope"})).status_code == 400
    assert (await client.patch(f"{BASE}/bots/99999", headers=admin_headers,
                               json={"status": "paused"})).status_code == 404


@pytest.mark.asyncio
async def test_fund_revives_dead_bot(client, admin_headers):
    r = await client.post(f"{BASE}/bots/generate", headers=admin_headers, json={
        "items": [{"template": "hodler", "count": 1}], "initial_cash": "0",
    })
    c = r.json()["created"][0]
    pid, uid = c["profile_id"], c["user_id"]
    async with async_session_maker() as s:
        async with s.begin():
            p = (await s.execute(select(BotProfile).where(BotProfile.id == pid))).scalars().one()
            p.status = "dead"

    r2 = await client.post(f"{BASE}/bots/{pid}/fund", headers=admin_headers,
                           json={"amount": "100", "reason": "复活测试"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["new_cash"] == 100.0 and body["status"] == "active"
    async with async_session_maker() as s:
        entries = (await s.execute(
            select(LedgerEntry).where(LedgerEntry.user_id == uid)
        )).scalars().all()
        # initial_cash=0 不记账，只有这次注资一条
        assert len(entries) == 1 and entries[0].reason == "复活测试"


@pytest.mark.asyncio
async def test_config_roundtrip(client, admin_headers):
    r = await client.get(f"{BASE}/config", headers=admin_headers)
    body = r.json()
    e = body["pve_enabled"]
    assert (e["value"], e["value_type"], e["is_default"]) == ("false", "bool", True)
    assert e["label"]  # 每个配置键都带中文说明（管理页直接渲染）

    r2 = await client.put(f"{BASE}/config", headers=admin_headers, json={
        "pve_enabled": "true", "pve_orders_per_min_cap": "10",
    })
    assert r2.status_code == 200, r2.text
    r3 = await client.get(f"{BASE}/config", headers=admin_headers)
    body3 = r3.json()
    assert body3["pve_enabled"]["value"] == "true" and not body3["pve_enabled"]["is_default"]
    assert body3["pve_orders_per_min_cap"]["value"] == "10"

    # 再改一次（走 set_value 已有行路径）
    r4 = await client.put(f"{BASE}/config", headers=admin_headers, json={"pve_enabled": "false"})
    assert r4.status_code == 200
    assert (await client.get(f"{BASE}/config", headers=admin_headers)).json()["pve_enabled"]["value"] == "false"

    assert (await client.put(f"{BASE}/config", headers=admin_headers,
                             json={"nope": "1"})).status_code == 400
    assert (await client.put(f"{BASE}/config", headers=admin_headers,
                             json={"pve_enabled": "maybe"})).status_code == 400
    assert (await client.put(f"{BASE}/config", headers=admin_headers,
                             json={"pve_orders_per_min_cap": "abc"})).status_code == 400


@pytest.mark.asyncio
async def test_overview_template_details_and_param_docs(client, admin_headers):
    """管理页人性化元数据：模板中文名/解说/分组/默认参数 + 全量参数说明。"""
    r = await client.get(f"{BASE}/overview", headers=admin_headers)
    body = r.json()
    details = {d["name"]: d for d in body["template_details"]}
    assert set(details) == set(body["templates"])
    fan = details["fan"]
    assert fan["title"] and fan["summary"] and fan["description"]
    assert fan["params"]["conviction"] == 0.3
    assert details["liquidity"]["group"] == "quant"    # active_preset=always → 量化
    assert details["fan"]["group"] == "retail"
    # 每个模板的每个参数键、以及注意力键，都必须有人话说明——新模板作者漏写会被这里拦下
    docs = body["param_docs"]
    from app.services.pve.attention import ATTENTION_DEFAULTS
    for d in details.values():
        for k in list(d["params"]) + list(ATTENTION_DEFAULTS):
            assert k in docs and docs[k], f"参数 {k} 缺说明"


@pytest.mark.asyncio
async def test_config_sentiment_key(client, admin_headers):
    """pve_sentiment 进配置注册表：合法 JSON / 清空可写，垃圾被拦。"""
    r = await client.get(f"{BASE}/config", headers=admin_headers)
    assert r.json()["pve_sentiment"]["value_type"] == "string"

    ok = await client.put(f"{BASE}/config", headers=admin_headers,
                          json={"pve_sentiment": '{"tilts": {"42": 0.15}}'})
    assert ok.status_code == 200, ok.text
    assert (await client.put(f"{BASE}/config", headers=admin_headers,
                             json={"pve_sentiment": ""})).status_code == 200  # 清空=撤风
    assert (await client.put(f"{BASE}/config", headers=admin_headers,
                             json={"pve_sentiment": "not json"})).status_code == 400
    assert (await client.put(f"{BASE}/config", headers=admin_headers,
                             json={"pve_sentiment": '{"tilts": {"x": 1}}'})).status_code == 400
