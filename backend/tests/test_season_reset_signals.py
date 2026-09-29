import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys

import pytest


@pytest.fixture
def setup_db():
    # 子进程只验证 CLI 的终止/清理生命周期，不需要共享应用数据库。
    yield


def test_cli_sigterm_unwinds_coroutine_before_exit(tmp_path):
    script = """
import asyncio
from scripts import season_reset

async def pending_reset(_):
    try:
        print('READY', flush=True)
        await asyncio.sleep(60)
    finally:
        print('COROUTINE_CLEANUP_FINISHED', flush=True)

season_reset.run = pending_reset
raise SystemExit(asyncio.run(season_reset._main(False)))
"""
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}", "APP_ENV": "development"}
    process = subprocess.Popen([sys.executable, "-u", "-c", script],
                               cwd=Path(__file__).resolve().parents[1], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=10), 'CLI did not start'
        assert process.stdout.readline().strip() == 'READY'
        selector.close()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 143, stderr
        assert 'COROUTINE_CLEANUP_FINISHED' in stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
