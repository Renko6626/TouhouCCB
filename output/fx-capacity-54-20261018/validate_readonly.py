"""Read-only admission probe against the completed local capacity database."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/"backend"), str(ROOT/"loadtest")]
os.environ["DATABASE_URL"] = "postgresql+asyncpg://tonrac@/fxcapacity_control_split_reads_300_test?host=/tmp/touhouccb-pg-socket&port=55486"
os.environ["THCCB_READ_ONLY_INSTANCE"] = "true"
os.environ["FX_CAPACITY_VARIANT"] = "split_reads"
os.environ["FX_CAPACITY_OUTPUT"] = tempfile.mkdtemp(prefix="fxcapacity_readonly_probe_")

import httpx
from sqlalchemy import text
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.services.credit.ownership import OWNERSHIP
from app.services.fx.writer import FX_WRITER
from fx_capacity_server import app


async def balances():
    async with async_session_maker() as db:
        return (await db.execute(text('''SELECT
            (SELECT sum(cash) FROM "user"), (SELECT sum(debt) FROM "user"),
            (SELECT count(*) FROM fx_trade), (SELECT count(*) FROM audit_event)'''))).one()


async def main():
    before = await balances()
    async with app.router.lifespan_context(app):
        assert not OWNERSHIP.is_owner and not OWNERSHIP.writes_enabled and not FX_WRITER.enabled
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            headers = {"Authorization": "Bearer "+create_access_token(1)}
            context = await client.get("/api/v1/user/trading-context", headers=headers,
                                       params={"current_product": "fx", "current_id": 2})
            assert context.status_code == 200 and context.json()["account"]["user_id"] == 1
            assert context.headers["cache-control"] == "no-store"
            assert context.headers["pragma"] == "no-cache"
            denied = await client.get("/api/v1/user/trading-context")
            assert denied.status_code == 401
            mutation = await client.post("/api/v1/fx/pairs/2/net/trades", headers=headers, json={})
            assert mutation.status_code == 503
            assert (await client.get("/health")).status_code == 200
        after = await balances()
        assert before == after
    print(json.dumps(dict(readonly_owner=False, fx_writer_enabled=False,
        authenticated_get=200, anonymous_get=401, mutation=503,
        api_no_store=True, balances_and_audit_unchanged=True)))


asyncio.run(main())
