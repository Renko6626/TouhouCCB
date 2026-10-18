"""Local capacity experiment wrapper; never use as a deployment entrypoint.

The cadence variant changes only replaceable public/pricing notifications.
Account changes, order results, transaction durability and admission are intact.
"""
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import time

from sqlalchemy import event

from app.main import app
from app.core.database import engine
from app.services.fx.writer import FX_WRITER
from app.services.fx.publisher import PUBLISHER as PUBLIC
from app.services.trading_prices import PUBLISHER as PRICING


class DirectASGIMiddleware:
    """Preserve main.log_requests policy without a per-chunk AnyIO relay."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from app.main import _LOG_SKIP_PREFIXES, access_logger, credit_flags
        from app.services.credit.ownership import OWNERSHIP, EconomicWritesDisabled
        from starlette.datastructures import MutableHeaders
        from starlette.responses import JSONResponse
        method, path = scope["method"], scope["path"]
        if method not in {"GET", "HEAD", "OPTIONS"}:
            if credit_flags.read_only_from_env() or OWNERSHIP.reason is not None:
                try:
                    OWNERSHIP.require_writes()
                except EconomicWritesDisabled as exc:
                    return await JSONResponse(status_code=503, content={"detail": str(exc)})(scope, receive, send)
        started = time.perf_counter()
        skipped = path.startswith(_LOG_SKIP_PREFIXES)
        response_started = False

        async def send_direct(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                if path.startswith("/api/v1/"):
                    headers = MutableHeaders(scope=message)
                    headers["Cache-Control"] = "no-store"
                    headers["Pragma"] = "no-cache"
                if not skipped:
                    status = message["status"]
                    access_logger.log(logging.WARNING if status >= 500 else logging.INFO,
                        "%s %s %d %.1fms", method, path, status, (time.perf_counter()-started)*1000)
            await send(message)

        try:
            await self.app(scope, receive, send_direct)
        except Exception:
            if not skipped and not response_started:
                access_logger.exception("%s %s EXC %.1fms", method, path, (time.perf_counter()-started)*1000)
            raise


class CadencedEvent(asyncio.Event):
    def __init__(self, interval):
        super().__init__()
        self.interval = interval
        self.last = 0.0

    async def wait(self):
        await super().wait()
        await asyncio.sleep(max(0, self.last + self.interval - time.monotonic()))
        self.last = time.monotonic()
        return True


variant = os.environ["FX_CAPACITY_VARIANT"]
if variant in {"cadence8", "asgi_cadence8", "split_reads"}:
    PRICING.wake = CadencedEvent(1 / 8)
    original_publish = PUBLIC._publish
    last_public = {}

    async def cadenced_publish(publication):
        pid = publication.pair_id
        await asyncio.sleep(max(0, last_public.get(pid, 0) + 1 / 8 - time.monotonic()))
        last_public[pid] = time.monotonic()
        # The existing publisher catches up committed pool events and drains
        # every retained trade delta, independently of the coalesced hint.
        await original_publish(publication)

    PUBLIC._publish = cadenced_publish
elif variant != "current":
    raise ValueError("unknown isolated experiment variant")

if variant in {"asgi_cadence8", "split_reads"}:
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from app.main import log_requests
    replaced = 0
    for index, middleware in enumerate(app.user_middleware):
        if middleware.cls is BaseHTTPMiddleware and middleware.kwargs.get("dispatch") is log_requests:
            app.user_middleware[index] = Middleware(DirectASGIMiddleware)
            replaced += 1
    assert replaced == 1

counts = Counter()
handlers = []
loop_lags = []
peak = 0


@event.listens_for(engine.sync_engine, "before_cursor_execute")
def sql_count(*args):
    counts["sql"] += 1


@event.listens_for(engine.sync_engine, "checkout")
def checkout(*args):
    global peak
    peak = max(peak, engine.pool.checkedout())


async def lag_probe():
    while True:
        start = time.monotonic()
        await asyncio.sleep(.02)
        loop_lags.append(max(0, time.monotonic() - start - .02) * 1000)


async def sample():
    global peak
    root = Path(os.environ["FX_CAPACITY_OUTPUT"])
    before_cpu, before_wall = time.process_time(), time.monotonic()
    previous_sql = 0
    with (root / "resources.jsonl").open("w") as out:
        while True:
            await asyncio.sleep(1)
            cpu, wall = time.process_time(), time.monotonic()
            phase_root = root.parent if os.environ.get("FX_CAPACITY_ROLE") == "read" else root
            phase_file = phase_root / "http" / "phase.txt"
            phase = phase_file.read_text().strip() if phase_file.exists() else "startup"
            row = dict(t=time.time(), phase=phase, interval_s=wall-before_wall,
                       app_cpu_percent=100*(cpu-before_cpu)/(wall-before_wall),
                       rss_mb=int(Path("/proc/self/statm").read_text().split()[1])*os.sysconf("SC_PAGE_SIZE")/1024**2,
                       pool_peak=peak, pool_checked_out=engine.pool.checkedout(),
                       sql=counts["sql"]-previous_sql,
                       queue_depths={str(k): q.qsize() for k, q in FX_WRITER._queues.items()},
                       handlers_ms=list(handlers), loop_lags_ms=list(loop_lags),
                       public=PUBLIC.stats())
            out.write(json.dumps(row) + "\n")
            out.flush()
            handlers.clear()
            loop_lags.clear()
            peak = engine.pool.checkedout()
            before_cpu, before_wall, previous_sql = cpu, wall, counts["sql"]


original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application):
        for key, op in tuple(FX_WRITER._ops.items()):
            async def measured(*args, _op=op, **kwargs):
                start = time.monotonic()
                try:
                    return await _op(*args, **kwargs)
                finally:
                    handlers.append((time.monotonic()-start)*1000)
            FX_WRITER._ops[key] = measured
        tasks = [asyncio.create_task(sample()), asyncio.create_task(lag_probe())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


app.router.lifespan_context = lifespan
