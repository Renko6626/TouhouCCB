import functools
import json
from collections import Counter

records = []
current_test = None


def pytest_runtest_setup(item):
    global current_test
    current_test = item.nodeid


def pytest_sessionstart(session):
    from sqlalchemy import event
    from app.core.database import engine
    from app.services.credit import risk
    from app.services.fx import net_trading

    original = risk.discover_dependencies

    @functools.wraps(original)
    async def observed(*args, **kwargs):
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith('SELECT'):
                statements.append(' '.join(statement.split()))
        event.listen(engine.sync_engine, 'before_cursor_execute', capture)
        try:
            return await original(*args, **kwargs)
        finally:
            event.remove(engine.sync_engine, 'before_cursor_execute', capture)
            rows = kwargs.get('locked_short_rows')
            records.append(dict(test=current_test,
                reused_user=kwargs.get('locked_user') is not None,
                reused_shorts=rows is not None,
                short_rows=len(rows) if rows is not None else None,
                account_locked=kwargs.get('account_locked', False),
                selects=statements))
    risk.discover_dependencies = observed
    net_trading.discover_dependencies = observed


def pytest_sessionfinish(session, exitstatus):
    with open('/tmp/thccb-locked-reuse-observation-20261018.json', 'w') as out:
        json.dump(records, out, indent=2)
    summary = Counter((r['reused_user'], r['reused_shorts'], len(r['selects'])) for r in records)
    print('\nTemporary discover SELECT observation:', dict(summary))
