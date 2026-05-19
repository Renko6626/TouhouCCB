import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest


@pytest.mark.asyncio
async def test_sqladmin_bot_suspicion_view_renders(client):
    """SQLAdmin bot_suspicion 列表页能渲染（不抛 500）。"""
    # SQLAdmin 默认路径 /api/v1/admin/<table_identity>/list
    r = await client.get("/api/v1/admin/bot-suspicion/list")
    # 未登录可能 401/302，关键是不能 500
    assert r.status_code in (200, 302, 401), r.text
