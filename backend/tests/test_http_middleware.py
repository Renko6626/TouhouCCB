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
async def test_access_preserves_body_cookies_cors_and_background(monkeypatch):
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/v1/probe', content=b'untouched body', headers={'Origin': 'https://example.test'})
    assert response.content == b'untouched body'
    assert len(response.headers.get_list('set-cookie')) == 2
    assert response.headers['access-control-allow-origin'] == 'https://example.test'
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['pragma'] == 'no-cache'
    assert completed == ['done']


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
