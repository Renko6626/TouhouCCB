"""测试公共 fixture。

设计理由（见 pytest.ini + memory project_pending_perf_work）：
- APScheduler（loan sweep）在 lifespan startup 启动，跟测试 drop_all 互锁导致偶发卡死。
  → session-scope mock 掉 start/stop，全程测试内不真启 scheduler。
- LifespanManager 原本每个测试重跑一次（init_db + auto_migrate + setup_admin + start_scheduler），
  占测试时间显著。→ module-scope 共享 client/lifespan。
- 每个测试仍 drop_all + create_all 保隔离，但 LifespanManager 只跑一次/module。
- site_config 进程内缓存每测试 reset，避免上一测试写 DB 后这一测试拿到 stale。
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ⚠️ 必须在 import 任何 app.* 之前设置：app.core.database 在 import 时即创建 engine，
# pydantic-settings 的 DATABASE_URL 字段从环境变量读取。
# 测试用独立 sqlite 文件，避免：
# 1) 跟 dev DB (backend/data/thccb.db) 抢锁、互相污染
# 2) 老 dev DB 文件碎片严重时 drop_all 极慢（实测 3+ 分钟/次，fresh DB 仅 ~100ms）
# 3) 测试 drop_all 把开发者 dev 数据洗掉
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/thccb_pytest.db")

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport
from sqlmodel import SQLModel

from app.main import app
from app.core.database import engine
from app.services.site_config import clear_cache


@pytest.fixture(scope="session", autouse=True)
def _disable_scheduler():
    """禁用 APScheduler：loan_sweep + liquidation_sweep + bot_detection 都 no-op。

    main.py 通过别名导入：
    - start_loan_scheduler / stop_loan_scheduler
    - start_liquidation_scheduler / stop_liquidation_scheduler
    - start_bot_detection_scheduler / stop_bot_detection_scheduler
    所以要 patch 这些绑定在 app.main 命名空间的本地名，patch 原始模块无效。
    """
    async def _noop():
        return None

    with (
        patch("app.main.start_loan_scheduler", _noop),
        patch("app.main.stop_loan_scheduler", _noop),
        patch("app.main.start_liquidation_scheduler", _noop),
        patch("app.main.stop_liquidation_scheduler", _noop),
        patch("app.main.start_bot_detection_scheduler", _noop),
        patch("app.main.stop_bot_detection_scheduler", _noop),
    ):
        yield


@pytest_asyncio.fixture(scope="module")
async def client():
    """共享 LifespanManager + AsyncClient 到整个 module，从 N 次 lifespan 周期减到 1 次/module。"""
    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://localhost:8004",
        ) as ac:
            yield ac


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """每个测试前 drop+create 全部表，clear site_config 缓存，seed 全站默认配置。
    function-scope 保留测试间数据隔离；与 module-scope client 配合，因为 lifespan startup
    后的 DB 状态对测试无意义（测试自己 seed）。

    seed 默认 activity_mode_enabled=false：candle/market 测试经过 verify_anti_bot
    会读这个 key，缺失会抛 SiteConfigError 500。测试可以在自己 fixture 里覆盖
    （UPDATE SET value='true'），不会冲突。
    """
    from app.models.base import SiteConfig
    from app.core.database import async_session_maker
    from app.services.market_writer import WRITER
    from app.services.candle_flusher import CANDLE_FLUSHER

    # 防上一测试的 writer/flusher 状态泄漏：module-scope lifespan 意味着 lifespan
    # 只在 module 首测启动、且当时 flag 缺失 → writer 默认不启，测试用 writer_on
    # fixture 显式启
    await WRITER.stop()
    CANDLE_FLUSHER._pending.clear()

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    clear_cache()
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(
                key="activity_mode_enabled", value="false", value_type="bool",
            ))
    yield


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _dispose_engine_after_session():
    """所有测试结束后 dispose engine 的 connection pool，避免 pytest 进程不退出。

    不加这个 → 不用 `client` fixture 的纯 unit 测（如 test_loan_service.py、
    test_wealth_mtm.py）跑完所有 case 后，aiosqlite connection 仍被 engine pool
    hold 着，pytest 进程 hang 在 connection cleanup，曾经被 timeout 杀掉
    (exit 124)。

    用 `client` fixture 的测试不会有这问题，因为 LifespanManager shutdown 会调
    `main.lifespan` 里的 `await engine.dispose()`。但 unit 测试根本没跑 lifespan，
    engine 留挂。这个 session-scope teardown 兜底所有测试结束后清理。

    教训详见 docs/schema-conventions.md 后续补充 + memory feedback。
    """
    yield
    await engine.dispose()
