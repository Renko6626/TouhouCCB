"""Observable HTTP policy behavior without starting the market runtime."""
import asyncio
import logging

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.background import BackgroundTask
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from app.core.http_middleware import AccessPolicyMiddleware
from app.services.credit.ownership import OWNERSHIP


def scope(path='/api/v1/probe', method='GET'):
    return {
        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
        'scheme': 'http', 'path': path, 'raw_path': path.encode(),
        'query_string': b'', 'method': method, 'headers': [],
        'server': ('test', 80), 'client': ('127.0.0.1', 1234),
    }


async def disconnected():
    return {'type': 'http.disconnect'}


@pytest.mark.asyncio
async def test_access_preserves_body_cookies_cors_and_background(monkeypatch, risk_records):
    from app.core.http_middleware import RiskRequestMiddleware
    monkeypatch.setattr(OWNERSHIP, '_reason', None)
    monkeypatch.delenv('THCCB_READ_ONLY_INSTANCE', raising=False)
    app = FastAPI()
    completed = []

    @app.post('/api/v1/probe')
    async def probe(request: Request):
        response = Response(await request.body(), background=BackgroundTask(completed.append, 'done'))
        response.set_cookie('first', '1')
        response.set_cookie('second', '2')
        return response

    app.add_middleware(CORSMiddleware, allow_origins=['https://example.test'])
    app.add_middleware(AccessPolicyMiddleware)
    app.add_middleware(RiskRequestMiddleware)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/v1/probe', content=b'untouched body', headers={'Origin': 'https://example.test'})
    assert response.content == b'untouched body'
    assert len(response.headers.get_list('set-cookie')) == 2
    assert response.headers['access-control-allow-origin'] == 'https://example.test'
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['pragma'] == 'no-cache'
    assert completed == ['done']
    assert [record['status'] for record in risk_records()] == [200]


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/api/v1/stream/probe', '/history/probe'])
async def test_access_skipped_paths_keep_cache_policy(path, caplog):
    async def endpoint(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'cache-control', b'public, max-age=60')]})
        await send({'type': 'http.response.body', 'body': b'ok'})

    messages = []

    async def send(message):
        messages.append(message)

    with caplog.at_level(logging.INFO, logger='thccb.access'):
        await AccessPolicyMiddleware(endpoint)(scope(path), disconnected, send)
    headers = dict(messages[0]['headers'])
    assert headers[b'cache-control'] == (b'no-store' if path.startswith('/api/') else b'public, max-age=60')
    assert not caplog.records


@pytest.mark.asyncio
@pytest.mark.parametrize('after_headers', [False, True])
async def test_access_exception_logged_once_at_headers(after_headers, caplog):
    async def endpoint(scope, receive, send):
        if after_headers:
            await send({'type': 'http.response.start', 'status': 201, 'headers': []})
        raise RuntimeError('endpoint failed')

    async def send(message):
        pass

    with caplog.at_level(logging.INFO, logger='thccb.access'):
        with pytest.raises(RuntimeError, match='endpoint failed'):
            await AccessPolicyMiddleware(endpoint)(scope(), disconnected, send)
    records = [record.getMessage() for record in caplog.records if record.name == 'thccb.access']
    assert len(records) == 1
    assert (' 201 ' if after_headers else ' EXC ') in records[0]


@pytest.mark.asyncio
async def test_access_passes_non_http_and_disconnect_cancellation(caplog):
    messages = []

    async def send(message):
        messages.append(message)

    async def websocket(scope, receive, send):
        assert scope['type'] == 'websocket'
        await send({'type': 'websocket.close', 'code': 1000})

    await AccessPolicyMiddleware(websocket)({'type': 'websocket'}, disconnected, send)
    assert messages == [{'type': 'websocket.close', 'code': 1000}]

    async def cancelled(scope, receive, send):
        assert await receive() == {'type': 'http.disconnect'}
        raise asyncio.CancelledError()

    with caplog.at_level(logging.INFO, logger='thccb.access'):
        with pytest.raises(asyncio.CancelledError):
            await AccessPolicyMiddleware(cancelled)(scope(), disconnected, send)
    assert not caplog.records


@pytest.fixture
def risk_records(monkeypatch, tmp_path):
    import json
    from app.core import risk_log

    monkeypatch.setattr(risk_log, '_ENABLED', True)
    monkeypatch.setattr(risk_log, '_DIR', tmp_path)
    monkeypatch.setattr(risk_log, '_handler', None)

    def records():
        return [json.loads(line) for path in tmp_path.glob('risk-*.jsonl') for line in path.read_text().splitlines()]

    yield records
    if risk_log._handler is not None:
        risk_log._handler[1].close()


@pytest.mark.asyncio
async def test_risk_readonly_records_real_503_before_send(monkeypatch, risk_records):
    from app.core.http_middleware import RiskRequestMiddleware

    monkeypatch.setattr(OWNERSHIP, '_reason', 'read_only')
    monkeypatch.setattr(OWNERSHIP, '_writes_enabled', False)
    messages = []

    async def endpoint(scope, receive, send):
        pytest.fail('readonly request reached endpoint')

    async def receive():
        pytest.fail('middleware consumed request body')

    async def send(message):
        if message['type'] == 'http.response.start':
            assert [record['status'] for record in risk_records()] == [503]
        messages.append(message)

    await RiskRequestMiddleware(AccessPolicyMiddleware(endpoint))(scope(method='POST'), receive, send)
    assert messages[0]['status'] == 503
    assert b'cache-control' not in dict(messages[0]['headers'])
    assert b'read_only' in messages[1]['body']
    record, = risk_records()
    assert (record['method'], record['path'], record['ip']) == ('POST', '/api/v1/probe', '127.0.0.1')


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['before_headers', 'after_headers', 'background', 'send', 'disconnect_cancel', 'after_headers_cancel'])
async def test_risk_failure_boundaries_record_actual_status_once(failure, risk_records):
    from app.core.http_middleware import RiskRequestMiddleware

    async def fail_background():
        raise RuntimeError('background failed')

    async def endpoint(scope, receive, send):
        if failure == 'before_headers':
            raise RuntimeError('before headers')
        if failure == 'disconnect_cancel':
            assert await receive() == {'type': 'http.disconnect'}
            raise asyncio.CancelledError()
        if failure == 'background':
            await Response(status_code=202, background=BackgroundTask(fail_background))(scope, receive, send)
            return
        await send({'type': 'http.response.start', 'status': 202, 'headers': []})
        if failure == 'after_headers_cancel':
            raise asyncio.CancelledError()
        raise RuntimeError('after headers')

    async def send(message):
        if message['type'] == 'http.response.start':
            assert [record['status'] for record in risk_records()] == [202]
            if failure == 'send':
                raise RuntimeError('send failed')

    expected_exception = asyncio.CancelledError if failure in {'disconnect_cancel', 'after_headers_cancel'} else RuntimeError
    with pytest.raises(expected_exception):
        await RiskRequestMiddleware(endpoint)(scope(method='POST'), disconnected, send)
    assert [record['status'] for record in risk_records()] == (
        [] if failure == 'disconnect_cancel' else [500 if failure == 'before_headers' else 202]
    )


@pytest.mark.asyncio
async def test_risk_bypasses_safe_non_api_and_non_http(risk_records):
    from app.core.http_middleware import RiskRequestMiddleware

    seen = []

    async def endpoint(scope, receive, send):
        seen.append(scope)
        assert await receive() == {'type': 'http.disconnect'}

    middleware = RiskRequestMiddleware(endpoint)
    scopes = [scope(method=method) for method in ('GET', 'HEAD', 'OPTIONS')]
    scopes += [scope('/outside', 'POST'), {'type': 'websocket'}]
    for request_scope in scopes:
        await middleware(request_scope, disconnected, None)
    assert seen == scopes
    assert risk_records() == []


@pytest.mark.asyncio
async def test_risk_log_write_failure_keeps_response(monkeypatch, risk_records, tmp_path, caplog):
    from app.core import risk_log
    from app.core.http_middleware import RiskRequestMiddleware

    blocked = tmp_path / 'file-not-directory'
    blocked.write_text('occupied')
    monkeypatch.setattr(risk_log, '_DIR', blocked)
    messages = []

    async def endpoint(scope, receive, send):
        await Response(b'accepted', status_code=201)(scope, receive, send)

    async def send(message):
        messages.append(message)

    with caplog.at_level(logging.ERROR, logger='app.core.risk_log'):
        await RiskRequestMiddleware(endpoint)(scope(method='POST'), disconnected, send)
    assert messages[0]['status'] == 201
    assert messages[1]['body'] == b'accepted'
    assert any('risk log: write failed' in record.getMessage() for record in caplog.records)
