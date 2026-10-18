"""Isolated repro server v2: full await-chain stacks + pool-wait markers + lock state.

External instrumentation only; product code and connection-pool settings untouched.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

BACKEND = "/tmp/sunyunbo/thccb-fx-loadtest-20261017/backend"
sys.path.insert(0, BACKEND)

import uvicorn  # noqa: E402
from sqlalchemy import event, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.main import app  # noqa: E402
from app.core.database import engine  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.services.trading_prices import REGISTRY  # noqa: E402

OUT = Path(os.environ["REPRO_DIR"])
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "snapshots").mkdir(exist_ok=True)
EVENTS = OUT / "pool-events.jsonl"
PGLOG = OUT / "pg-stat.jsonl"

POOL = engine.sync_engine.pool
POOL_MAX = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW

# Counterfactual only (never product code): reproduce the auth lookup in a short
# session so the request-scoped session never holds a connection. If the deadlock
# disappears under load, the nested borrow is the cause.
if os.environ.get("REPRO_MODE") == "short_auth":
    from fastapi import Depends  # noqa: E402

    from app.core.users import _bearer, get_current_user  # noqa: E402
    from app.core.database import async_session_maker  # noqa: E402

    async def short_auth(credentials=Depends(_bearer)):
        async with async_session_maker() as db:
            user = await get_current_user(credentials, db)
        return user

    app.dependency_overrides[get_current_user] = short_auth

state = {
    "holders": {},        # id(dbapi_conn) -> {conn_id, at, task}
    "waiters": {},         # id(task) -> {at, sync_stack}
    "checkouts": 0,
    "checkins": 0,
    "pool_timeouts": 0,
    "seq": 0,
    "hot_since": None,
    "last_dump": 0.0,
}
t0 = time.monotonic()


def now() -> float:
    return time.monotonic()


def log(rec: dict) -> None:
    rec["t"] = round(now() - t0, 3)
    with EVENTS.open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def coro_chain(task: asyncio.Task, limit: int = 90) -> list[str]:
    """Walk the await chain (cr_await), which task.get_stack() does not show."""
    out: list[str] = []
    coro = task.get_coro()
    seen = set()
    while coro is not None and len(out) < limit:
        if id(coro) in seen:
            break
        seen.add(id(coro))
        code = getattr(coro, "cr_code", None) or getattr(coro, "gi_code", None)
        frame = getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None)
        if code is not None and frame is not None:
            out.append(f"{code.co_filename.replace('/tmp/sunyunbo/thccb-fx-loadtest-20261017/backend/', '')}"
                       f":{frame.f_lineno} in {code.co_qualname}")
        nxt = getattr(coro, "cr_await", None) or getattr(coro, "gi_yieldfrom", None)
        if nxt is None or not (hasattr(nxt, "cr_code") or hasattr(nxt, "gi_code")):
            break
        coro = nxt
    return out


def endpoint_of(chain: list[str]) -> str:
    for line in chain:
        if "/app/api/v1/" in line or line.startswith("app/api/v1/"):
            return line.split(" in ")[-1]
    for line in chain:
        if line.startswith("app/services/"):
            return line.split(" in ")[-1]
    return "?"


def classify(chain: list[str]) -> str:
    joined = "\n".join(chain)
    if "sqlalchemy/util/queue.py" in joined or (
            "_concurrency_py3k.py" in joined and "asyncio/queues.py" in joined):
        return "waiting_for_pool"
    if "app/services/trading_prices.py" in joined and "asyncio/locks.py" in joined:
        return "waiting_market_lock"
    if "asyncio/locks.py" in joined:
        return "waiting_child_event"
    if "asyncio/queues.py" in joined:
        return "waiting_queue"
    return "running"


def task_record(task: asyncio.Task) -> dict:
    chain = coro_chain(task)
    return {
        "name": task.get_name(),
        "coro": repr(task.get_coro())[:120],
        "kind": classify(chain),
        "endpoint": endpoint_of(chain),
        "in_pool_wait_ms": (round((now() - state["waiters"][id(task)]["at"]) * 1000, 1)
                            if id(task) in state["waiters"] else None),
        "chain": chain,
    }


# ── pool checkout / checkin ─────────────────────────────────────────────
@event.listens_for(POOL, "checkout")
def _checkout(dbapi_conn, record, proxy):
    task = asyncio.current_task()
    state["checkouts"] += 1
    state["holders"][id(dbapi_conn)] = {"conn": id(dbapi_conn), "at": now(), "task": task}
    log({"event": "checkout", "held": POOL.checkedout(),
         "endpoint": endpoint_of(coro_chain(task)) if task else "?",
         "task": task.get_name() if task else None})


@event.listens_for(POOL, "checkin")
def _checkin(dbapi_conn, record):
    rec = state["holders"].pop(id(dbapi_conn), None)
    state["checkins"] += 1
    log({"event": "checkin", "held": POOL.checkedout(),
         "held_ms": round((now() - rec["at"]) * 1000, 1) if rec else None,
         "endpoint": endpoint_of(coro_chain(rec["task"])) if rec and rec["task"] else "?"})


# ── pool-wait marker: AsyncAdaptedQueue.get is where checkout blocks ─────
QUEUE = POOL._pool
_orig_get = QUEUE.get


def _instrumented_get(block=True, timeout=None):
    task = asyncio.current_task()
    sync_stack = [f"{f.filename.replace('/tmp/sunyunbo/thccb-fx-loadtest-20261017/backend/', '')}:{f.lineno} in {f.name}"
                  for f in traceback.extract_stack()]
    entry = {"at": now(), "sync_stack": sync_stack[-14:]}
    if task is not None:
        state["waiters"][id(task)] = entry
    try:
        return _orig_get(block=block, timeout=timeout)
    finally:
        if task is not None:
            rec = state["waiters"].pop(id(task), None)
            if rec is not None:
                log({"event": "pool_wait_done", "waited_ms": round((now() - rec["at"]) * 1000, 1)})
                if (now() - rec["at"]) > 25:
                    state["pool_timeouts"] += 1


QUEUE.get = _instrumented_get


def locks_state() -> dict:
    out = {}
    for key, lock in list(REGISTRY._locks.items()):
        waiters = getattr(lock, "_waiters", None)
        out[f"{key[0]}:{key[1]}"] = {
            "locked": lock.locked(),
            "waiters": len([w for w in (waiters or []) if w and not w.done()]),
        }
    return out


def snapshot() -> dict:
    tasks = [t for t in asyncio.all_tasks() if not t.done()]
    records = [task_record(t) for t in tasks]
    kinds = Counter(r["kind"] for r in records)
    holders = []
    for rec in list(state["holders"].values()):
        task = rec["task"]
        alive = task is not None and not task.done()
        entry = {"conn": rec["conn"], "age_ms": round((now() - rec["at"]) * 1000, 1),
                 "task_alive": alive}
        if alive:
            entry.update(task_record(task))
        holders.append(entry)
    return {
        "checkedout": POOL.checkedout(),
        "pool_max": POOL_MAX,
        "async_tasks": len(records),
        "by_kind": dict(kinds),
        "by_endpoint": dict(Counter(r["endpoint"] for r in records
                                    if r["kind"] != "running")),
        "locks": locks_state(),
        "holders": holders,
        "waiting_for_pool": [r for r in records if r["kind"] == "waiting_for_pool"],
        "waiting_market_lock": [r for r in records if r["kind"] == "waiting_market_lock"],
        "pool_waits_started": len(state["waiters"]),
        "checkouts": state["checkouts"],
        "checkins": state["checkins"],
    }


async def watch() -> None:
    while True:
        await asyncio.sleep(0.25)
        snap = snapshot()
        if snap["checkedout"] >= POOL_MAX - 2:
            state["hot_since"] = state["hot_since"] or now()
            if now() - state["last_dump"] > 2.0:
                state["last_dump"] = now()
                state["seq"] += 1
                snap["seq"] = state["seq"]
                snap["hot_s"] = round(now() - state["hot_since"], 2)
                (OUT / "snapshots" / f"hot-{state['seq']:03d}.json").write_text(
                    json.dumps(snap, indent=1, ensure_ascii=False))
                log({"event": "hot", "seq": state["seq"], "checkedout": snap["checkedout"],
                     "waiting_for_pool": len(snap["waiting_for_pool"]),
                     "waiting_market_lock": len(snap["waiting_market_lock"]),
                     "locks": snap["locks"]})
        else:
            state["hot_since"] = None


async def pg_sample() -> None:
    eng = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        while True:
            try:
                async with eng.connect() as conn:
                    rows = (await conn.execute(text("""
                        select coalesce(state,'?'), coalesce(wait_event_type,'-'),
                               coalesce(wait_event,'-'), count(*)
                        from pg_stat_activity
                        where datname = current_database() and pid <> pg_backend_pid()
                        group by 1,2,3 order by 4 desc
                    """))).all()
            except Exception as exc:
                rows = [(f"error:{type(exc).__name__}", "-", "-", 1)]
            with PGLOG.open("a") as fh:
                fh.write(json.dumps({"t": round(now() - t0, 2),
                                     "rows": [list(r) for r in rows]}) + "\n")
            await asyncio.sleep(0.5)
    finally:
        await eng.dispose()


async def main() -> None:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=55571,
                                           log_level="warning", access_log=False))
    serving = asyncio.create_task(server.serve())
    await asyncio.sleep(2.0)
    log({"event": "ready", "pool_max": POOL_MAX})
    async with asyncio.TaskGroup() as tg:
        tg.create_task(watch())
        tg.create_task(pg_sample())
        await serving


if __name__ == "__main__":
    asyncio.run(main())
