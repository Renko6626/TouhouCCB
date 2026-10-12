"""Private bounded delivery and transaction rollback isolation."""
from decimal import Decimal
import pytest
from app.core.database import async_session_maker, managed_transaction
from app.models.base import User
from app.services.account_events import AccountEvents, EVENTS, notifying_transaction
from app.services.credit.version import bump_economic_version

@pytest.fixture
def private_events():
    subscriptions = []
    def subscribe(user_id):
        sub = EVENTS.subscribe(user_id)
        subscriptions.append(sub)
        return sub.queue
    yield subscribe
    for sub in subscriptions:
        EVENTS.unsubscribe(sub)

def test_private_bounded_independent_delivery():
    broker = AccountEvents()
    slow = broker.subscribe(1)
    fast = broker.subscribe(1)
    other = broker.subscribe(2)
    for i in range(65):
        broker.enqueue(1, i, 'trade')
        event, payload = fast.queue.get_nowait()
        assert event == 'account_changed' and payload['economic_version'] == str(i)
    assert slow.closed.is_set() and slow.queue.qsize() == 64
    assert not fast.closed.is_set() and other.queue.empty()
    broker.close()
    assert not broker._subscribers

@pytest.mark.asyncio
async def test_rollback_savepoint_and_noop_do_not_publish(private_events):
    async with async_session_maker() as db:
        user = User(username='rollback-hints', cash=Decimal('10'))
        db.add(user); await db.commit()
        uid = user.id
        queue = private_events(uid)
        with pytest.raises(ValueError):
            async with notifying_transaction(db, managed_transaction(db), 'failed'):
                user.cash = Decimal('2'); bump_economic_version(user)
                await db.flush()
                raise ValueError('abort')
        assert queue.empty()
        async with notifying_transaction(db, managed_transaction(db), 'savepoint'):
            user = await db.get(User, uid)
            with pytest.raises(ValueError):
                async with db.begin_nested():
                    user.cash = Decimal('1'); bump_economic_version(user)
                    await db.flush()
                    raise ValueError('abort nested')
            await db.refresh(user)
        assert queue.empty()
        async with notifying_transaction(db, managed_transaction(db), 'noop'):
            user = await db.get(User, uid)
            assert user.cash == Decimal('10') and user.economic_version == 0
        assert queue.empty()
        async with notifying_transaction(db, managed_transaction(db), 'success'):
            with pytest.raises(ValueError):
                async with db.begin_nested():
                    user.cash = Decimal('2'); bump_economic_version(user)
                    await db.flush()
                    raise ValueError('version reused after rollback')
            await db.refresh(user)
            user.cash = Decimal('9'); bump_economic_version(user)
            await db.flush()
        event, payload = queue.get_nowait()
        assert event == 'account_changed' and payload['economic_version'] == '1'
        assert queue.empty()

@pytest.mark.asyncio
async def test_notification_failure_preserves_committed_result(monkeypatch):
    async with async_session_maker() as db:
        user = User(username='hint-failure', cash=Decimal('10'))
        db.add(user); await db.commit()
        uid = user.id
        def unavailable(*args, **kwargs):
            raise RuntimeError('push unavailable')
        monkeypatch.setattr(EVENTS, 'enqueue', unavailable)
        async with notifying_transaction(db, managed_transaction(db), 'spend'):
            user.cash = Decimal('7'); bump_economic_version(user)
    async with async_session_maker() as db:
        persisted = await db.get(User, uid)
        assert persisted.cash == Decimal('7') and persisted.economic_version == 1
