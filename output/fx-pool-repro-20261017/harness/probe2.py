"""Concurrency probes against the isolated repro server (real routes, real pool)."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

TOKENS = Path("/tmp/sunyunbo/fx-pool-repro-20261017/lt/repro100/tokens.json")
PAIR = 2


def pct(vals, p):
    vals = sorted(vals)
    if not vals:
        return None
    return round(vals[min(len(vals) - 1, max(0, int(len(vals) * p) - 1))], 1)


async def call(client, token, method, path, **kw):
    start = time.monotonic()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = await client.request(method, path, headers=headers, **kw)
        body = None
        try:
            body = r.json()
        except Exception:
            pass
        return {"ok": r.status_code == 200, "status": r.status_code,
                "ms": round((time.monotonic() - start) * 1000, 1), "body": body}
    except Exception as exc:
        return {"ok": False, "status": type(exc).__name__,
                "ms": round((time.monotonic() - start) * 1000, 1)}


async def one_versions(client, token):
    return await call(client, token, "GET", "/api/v1/user/trading-versions")


async def one_trade(client, token):
    stages = {}
    q = await call(client, token, "POST", f"/api/v1/fx/pairs/{PAIR}/net/quote",
                   json={"side": "buy", "amount": "10", "borrow_amount": "0"})
    stages["quote"] = q
    if not q["ok"] or not (q["body"] or {}).get("executable"):
        return {"ok": False, "stage": "quote", "stages": stages,
                "blocked": (q["body"] or {}).get("blocked_reason")}
    body = {"side": "buy", "amount": "10", "borrow_amount": "0", "min_out": "0",
            "allow_growth": True,
            "expected_economic_version": q["body"]["economic_version"],
            "idempotency_key": f"probe:{uuid4().hex}"}
    t = await call(client, token, "POST", f"/api/v1/fx/pairs/{PAIR}/net/trades", json=body)
    stages["trade"] = t
    ctx = await call(client, token, "GET", "/api/v1/user/trading-context")
    stages["context"] = ctx
    return {"ok": t["ok"], "stage": "trade" if not t["ok"] else "done", "stages": stages,
            "ms": sum(s["ms"] for s in stages.values())}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:55571")
    ap.add_argument("--mode", choices=["versions", "trade", "control"], required=True)
    ap.add_argument("--concurrency", type=int, default=30)
    ap.add_argument("--timeout", type=float, default=45)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    tokens = [u["token"] for u in json.loads(TOKENS.read_text())["users"]][: args.concurrency]
    async with httpx.AsyncClient(base_url=args.base, timeout=args.timeout, trust_env=False,
                                 limits=httpx.Limits(max_connections=300)) as client:
        start = time.monotonic()
        if args.mode == "versions":
            results = await asyncio.gather(*(one_versions(client, t) for t in tokens))
            ms = [r["ms"] for r in results]
            out = {"mode": args.mode, "concurrency": args.concurrency,
                   "wall_s": round(time.monotonic() - start, 2),
                   "ok": sum(1 for r in results if r["ok"]),
                   "statuses": {},
                   "p50_ms": pct(ms, .5), "p95_ms": pct(ms, .95), "max_ms": max(ms)}
        elif args.mode == "control":
            results = await asyncio.gather(*(
                call(client, "ignored", "GET", "/api/v1/fx/pairs") for _ in range(args.concurrency)))
            ms = [r["ms"] for r in results]
            out = {"mode": "control /fx/pairs", "concurrency": args.concurrency,
                   "wall_s": round(time.monotonic() - start, 2),
                   "ok": sum(1 for r in results if r["ok"]),
                   "p50_ms": pct(ms, .5), "p95_ms": pct(ms, .95), "max_ms": max(ms)}
        else:
            results = await asyncio.gather(*(one_trade(client, t) for t in tokens))
            out = {"mode": args.mode, "concurrency": args.concurrency,
                   "wall_s": round(time.monotonic() - start, 2),
                   "trades_ok": sum(1 for r in results if r["ok"]),
                   "blocked": sum(1 for r in results if r.get("blocked")),
                   "failed_stages": {k: sum(1 for r in results if r["stage"] == k)
                                     for k in {r["stage"] for r in results}},
                   "quote_ms": {"p50": pct([r["stages"]["quote"]["ms"] for r in results], .5),
                                "max": max(r["stages"]["quote"]["ms"] for r in results)},
                   "trade_ms": {"p50": pct([r["stages"]["trade"]["ms"] for r in results], .5),
                                "p95": pct([r["stages"]["trade"]["ms"] for r in results], .95),
                                "max": max(r["stages"]["trade"]["ms"] for r in results)},
                   "context_ms": {"p50": pct([r["stages"]["context"]["ms"] for r in results], .5),
                                  "max": max(r["stages"]["context"]["ms"] for r in results)}}
        out["label"] = args.label
        out["statuses"] = {str(k): sum(1 for r in results if r["status"] == k)
                           for k in sorted({r["status"] for r in results}, key=str)} if "statuses" in out else {}
    path = Path("/tmp/sunyunbo/fx-pool-repro-20261017/probes.jsonl")
    with path.open("a") as fh:
        fh.write(json.dumps(out, ensure_ascii=False) + "\n")
    print(json.dumps(out, ensure_ascii=False))


asyncio.run(main())
