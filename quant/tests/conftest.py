import asyncio
import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()
