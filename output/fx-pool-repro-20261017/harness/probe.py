"""Structural probe: fire N concurrent real requests against the repro server."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

TOKENS = Path("/tmp/sunyunbo/fx-pool-repro-20261017/lt/repro100/tokens.json")


async def one(client: httpx.AsyncClient, token: str, path: str) -> dict:
    start = time.monotonic()
    try:
        r = await client.get(path, headers={"Authorization": f"Bearer {token}"})
        return {"ok": r.status_code == 200, "status": r.status_code,
                "ms": round((time.monotonic() - start) * 1000, 1)}
    except Exception as exc:
        return {"ok": False, "status": type(exc).__name__,
                "ms": round((time.monotonic() - start) * 1000, 1)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:55571")
    ap.add_argument("--path", default="/api/v1/user/trading-versions")
    ap.add_argument("--concurrency", type=int, default=40)
    ap.add_argument("--timeout", type=float, default=40)
    args = ap.parse_args()

    tokens = [u["token"] for u in json.loads(TOKENS.read_text())["users"]]
    chosen = tokens[: args.concurrency]
    async with httpx.AsyncClient(base_url=args.base, timeout=args.timeout,
                                 limits=httpx.Limits(max_connections=200)) as client:
        start = time.monotonic()
        results = await asyncio.gather(*(one(client, t, args.path) for t in chosen))
    elapsed = round(time.monotonic() - start, 2)
    lat = sorted(r["ms"] for r in results)
    print(json.dumps({
        "path": args.path, "concurrency": args.concurrency, "wall_s": elapsed,
        "ok": sum(1 for r in results if r["ok"]),
        "statuses": {str(s): sum(1 for r in results if r["status"] == s)
                     for s in sorted({r["status"] for r in results}, key=str)},
        "p50_ms": lat[len(lat) // 2], "p95_ms": lat[int(len(lat) * 0.95) - 1],
        "max_ms": lat[-1],
    }, ensure_ascii=False))


asyncio.run(main())
