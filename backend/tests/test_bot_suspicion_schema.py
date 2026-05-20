import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timezone
import pytest
from app.core.database import async_session_maker
from app.models.base import BotSuspicion, User


@pytest.mark.asyncio
async def test_bot_suspicion_can_be_persisted(client):
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username="t", casdoor_id="t_cas")
            s.add(u)
            await s.flush()
            ev = BotSuspicion(
                user_id=u.id,
                signals="high_freq",
                metrics='{"total_trades": 145}',
                window_start=datetime.now(timezone.utc),
                window_end=datetime.now(timezone.utc),
            )
            s.add(ev)
            await s.flush()
            assert ev.id is not None
            assert ev.review_status == "pending"
            assert ev.reviewed_by is None
