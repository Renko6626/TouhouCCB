#!/usr/bin/env python3
"""FX HTTP load test. Run on a separate machine with ordinary test-user tokens."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import json
import math
from pathlib import Path
import random
import time
from urllib.parse import urlparse
from uuid import uuid4

import httpx


def percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None}
    return {"count": len(ordered), **{f"p{p}_ms": round(ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)], 3)
                                      for p in (50, 95, 99)}}


def classify(status, detail=""):
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    if status == 409 and "version" in str(detail):
        return "version_conflict"
    if status >= 400:
        return "business_rejected" if status in (400, 409) else "http_error"
    return "ok"


class Metrics:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=False)
        self.samples = (directory / "requests.jsonl").open("w")
        self.directory = directory
        self.started = time.monotonic()
        self.phase = "preflight"
        self.stages = []
        self.counts = defaultdict(Counter)
        self.latencies = defaultdict(list)
        self.recent = deque()
        self.stop = asyncio.Event()
        self.reason = None
        self.connected = {"public": set(), "private": set()}
        self.seen = {"public": set(), "private": set()}
        self.first_frame = defaultdict(list)
        self.intervals = defaultdict(list)
        self.event_age = defaultdict(list)
        self.sse_counts = defaultdict(Counter)
        self.ready_users = set()
        self.trade_seen = {}  # First observer's local arrival time, indexed by trade ID.
        self.visibility = defaultdict(list)
        self.visibility_missing = Counter()
        self.slot_lag = defaultdict(list)
        self.trade_targets = Counter()
        self.phase_started = {}

    def record(self, name, category, ms, status=None, detail=None):
        now = time.monotonic()
        phase = self.phase
        self.counts[(phase, name)][category] += 1
        self.latencies[(phase, name, category)].append(ms)
        self.recent.append((now, name, category, ms))
        self.samples.write(json.dumps({"t_s": round(now-self.started, 3), "phase": phase,
            "name": name, "category": category, "status": status, "ms": round(ms, 3),
            "detail": detail}) + "\n")
        if category == "auth":
            self.abort("401/403: authentication or account admission failed")

    def abort(self, reason):
        if not self.stop.is_set():
            self.reason = reason
            self.stop.set()

    def summary(self, metadata):
        rows = []
        for (phase, name), categories in self.counts.items():
            rows.append({"phase": phase, "name": name, "results": dict(categories),
                         "latency_by_result": {c: percentiles(self.latencies[(phase, name, c)]) for c in categories}})
        return {"metadata": metadata, "abort_reason": self.reason, "stages": self.stages,
                "requests": rows,
                "sse": {key: {"first_frame": percentiles(self.first_frame[key]),
                              "receive_interval": percentiles(self.intervals[key]),
                              "event_age_ms_clock_dependent": percentiles(self.event_age[key]),
                              "counts": dict(self.sse_counts[key])} for key in ("public", "private")},
                "trade_visibility": {phase: {"first_observer_ms_from_request_start": percentiles(values),
                                             "unmatched": self.visibility_missing[phase]}
                                     for phase, values in self.visibility.items()}}


class Runner:
    def __init__(self, args, credentials, metrics, client):
        self.args, self.credentials, self.m, self.client = args, credentials, metrics, client
        self.pid = credentials["pair_id"]
        self.group = {"current_product": "fx", "current_id": self.pid}

    async def request(self, user, name, method, path, *, body=None, params=None):
        start = time.monotonic()
        headers = {"Authorization": "Bearer " + user["token"]} if user else {}
        try:
            response = await self.client.request(method, path, headers=headers, json=body, params=params)
            try:
                data = response.json()
            except ValueError:
                category = classify(response.status_code) if response.status_code >= 400 else "invalid_json"
                self.m.record(name, category, (time.monotonic()-start)*1000, response.status_code)
                return None
            detail = data.get("detail") if isinstance(data, dict) else None
            category = classify(response.status_code, detail)
            # Only stable string error markers, never arbitrary response bodies.
            marker = detail[:160] if isinstance(detail, str) else None
            self.m.record(name, category, (time.monotonic()-start)*1000, response.status_code, marker)
            return data if category == "ok" else None
        except httpx.TimeoutException:
            self.m.record(name, "timeout", (time.monotonic()-start)*1000)
        except httpx.RequestError:
            self.m.record(name, "network_error", (time.monotonic()-start)*1000)
        return None

    async def stream(self, user, kind, ready):
        path = f"/api/v1/fx/stream/{self.pid}" if kind == "public" else "/api/v1/stream/trading"
        headers = {"Accept": "text/event-stream"}
        if kind == "private":
            headers["Authorization"] = "Bearer " + user["token"]
        started, last, first = time.monotonic(), None, True
        try:
            async with self.client.stream("GET", path, headers=headers,
                    params=self.group if kind == "private" else None,
                    timeout=httpx.Timeout(35, connect=10, pool=10)) as response:
                if response.status_code != 200:
                    self.m.record("sse_"+kind, classify(response.status_code),
                                  (time.monotonic()-started)*1000, response.status_code)
                    return
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    self.m.record("sse_"+kind, "invalid_stream", (time.monotonic()-started)*1000)
                    return
                event, chunks = "", []
                async for line in response.aiter_lines():
                    if self.m.stop.is_set():
                        break
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        chunks.append(line[5:].lstrip())
                    elif not line and chunks:
                        try:
                            payload = json.loads("\n".join(chunks))
                        except ValueError:
                            self.m.sse_counts[kind]["invalid_frames"] += 1
                            chunks = []
                            continue
                        now = time.monotonic()
                        if first:
                            valid = (kind == "public" and event == "snapshot" and
                                     isinstance(payload.get("data"), dict) and "price" in payload["data"]) or (
                                     kind == "private" and event == "sync" and "economic_version" in payload)
                            if not valid:
                                self.m.record("sse_"+kind, "invalid_first_frame", (now-started)*1000)
                                return
                            self.m.first_frame[kind].append((now-started)*1000)
                            self.m.connected[kind].add(user["uid"])
                            self.m.seen[kind].add(user["uid"])
                            ready.set()
                            first = False
                        if last is not None:
                            self.m.intervals[kind].append((now-last)*1000)
                        last = now
                        self.m.sse_counts[kind]["frames"] += 1
                        data = payload.get("data", payload)
                        stamp = payload.get("ts") or data.get("occurred_at") or data.get("server_time")
                        if stamp:
                            try:
                                age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()*1000
                                self.m.event_age[kind].append(age)
                            except (ValueError, TypeError):
                                pass
                        if kind == "public" and user["uid"] == self.credentials["users"][0]["uid"]:
                            for trade in data.get("trades", []):
                                self.m.trade_seen.setdefault(int(trade["id"]), now)
                        event, chunks = "", []
                if not self.m.stop.is_set():
                    self.m.sse_counts[kind]["unexpected_disconnects"] += 1
                    self.m.record("sse_"+kind, "disconnected", (time.monotonic()-started)*1000)
        except httpx.TimeoutException:
            self.m.record("sse_"+kind, "timeout", (time.monotonic()-started)*1000)
        except httpx.RequestError:
            self.m.record("sse_"+kind, "network_error", (time.monotonic()-started)*1000)
        finally:
            self.m.connected[kind].discard(user["uid"])

    async def trade(self, user, *, force_buy=False):
        context = await self.request(user, "context", "GET", "/api/v1/user/trading-context", params=self.group)
        if not context:
            return
        account = context["account"]
        wallet = next((w for w in account["fx_wallets"] if w["pair_id"] == self.pid), None)
        held = Decimal(wallet["foreign_amount"]) if wallet else Decimal("0")
        side = "sell" if held > 0 and not force_buy else "buy"
        amount = held if side == "sell" else min(Decimal(self.args.amount), Decimal(account["cash"]))
        amount = amount.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if amount <= 0:
            self.m.record("cycle", "no_funds", 0)
            return
        body = {"side": side, "amount": str(amount), "borrow_amount": "0"}
        quote = await self.request(user, "quote", "POST", f"/api/v1/fx/pairs/{self.pid}/net/quote", body=body)
        if not quote:
            return
        if not quote.get("executable"):
            self.m.record("cycle", "quote_blocked", 0, detail=quote.get("blocked_reason"))
            return
        # Quote response supplies the real account version; never retry automatically.
        body.update(min_out=str((Decimal(quote["output_amount"]) * Decimal("0.95")).quantize(
            Decimal("0.000001"), rounding=ROUND_DOWN)), expected_economic_version=quote["economic_version"],
            allow_growth=side == "buy", idempotency_key=f"lt:{self.credentials['run_id']}:{uuid4().hex}")
        start = time.monotonic()
        phase = self.m.phase
        result = await self.request(user, "trade", "POST", f"/api/v1/fx/pairs/{self.pid}/net/trades", body=body)
        if result:
            trade_ids = result.get("trade_ids", [])
            self.m.visibility[phase]  # Include missing-only observations in the report.
            # Observe asynchronously so waiting for broadcast does not throttle traders.
            return phase, start, trade_ids

    async def observer(self, observations):
        for phase, started, ids in observations:
            found = [self.m.trade_seen[i] for i in ids if i in self.m.trade_seen]
            if ids and len(found) == len(ids):
                self.m.visibility[phase].append((max(found)-started)*1000)
            else:
                self.m.visibility_missing[phase] += 1

    async def versions(self, user, deadline, phase_stop):
        await asyncio.sleep(random.uniform(0, 15))
        while time.monotonic() < deadline and not phase_stop.is_set() and not self.m.stop.is_set():
            await self.request(user, "versions", "GET", "/api/v1/user/trading-versions", params=self.group)
            await asyncio.sleep(min(15, max(0, deadline-time.monotonic())))

    async def stage(self, name, users, seconds, *, burst=False):
        self.m.phase = name
        start = time.monotonic()
        self.m.phase_started[name] = start
        deadline = start+seconds
        self.m.recent.clear()
        observations = []
        completed = set()
        async def worker(user, i):
            slot = start + (2*i/max(1, len(users)-1) if burst else random.uniform(0, min(5, seconds/2)))
            while slot < deadline and not self.m.stop.is_set():
                await asyncio.sleep(max(0, slot-time.monotonic()))
                if self.m.stop.is_set() or time.monotonic() >= deadline:
                    break
                actual_start = time.monotonic()
                self.m.slot_lag[name].append((actual_start-slot)*1000)
                self.m.trade_targets[name] += 1
                result = await self.trade(user, force_buy=burst)
                completed.add(user["uid"])
                if result:
                    observations.append(result)
                if burst:
                    break
                # Rebase on this cycle's actual start (no accumulated catch-up
                # backlog), but preserve an overdue next slot so its delay is
                # measured rather than erased when the server is slow.
                slot = actual_start + random.uniform(self.args.min_interval, self.args.max_interval)
        phase_stop = asyncio.Event()
        traders = [asyncio.create_task(worker(u, i)) for i, u in enumerate(users)]
        readers = [] if burst else [asyncio.create_task(self.versions(u, deadline, phase_stop)) for u in users]
        try:
            # Keep observing for the full stage (including the burst recovery window).
            while time.monotonic() < deadline and not self.m.stop.is_set():
                await asyncio.sleep(min(1, max(0, deadline-time.monotonic())))
                if self.args.mode == "smoke" and any(t.done() and t.exception() for t in traders):
                    break
            await asyncio.gather(*traders)
            # Allow in-flight broadcast to arrive, without adding HTTP load.
            await asyncio.sleep(1)
            await self.observer(observations)
        finally:
            phase_stop.set()
            for task in traders+readers:
                task.cancel()
            await asyncio.gather(*traders, *readers, return_exceptions=True)
            elapsed = time.monotonic()-start
            successes = self.m.counts[(name, "trade")]["ok"]
            row = {"name": name, "target_users": len(users), "active_users": len(completed),
                   "duration_s": round(elapsed, 3), "attempted_trade_cycles": self.m.trade_targets[name],
                   "successful_trades": successes, "successful_trades_per_s": round(successes/elapsed, 3),
                   "trade_cycle_starts_per_s": round(self.m.trade_targets[name]/elapsed, 3),
                   "schedule_lag": percentiles(self.m.slot_lag[name]),
                   "public_connected_at_end": len(self.m.connected["public"]),
                   "private_connected_at_end": len(self.m.connected["private"])}
            completed_requests = sum(sum(counts.values()) for (phase, _), counts in self.m.counts.items() if phase == name)
            row.update(completed_requests=completed_requests, completed_requests_per_s=round(completed_requests/elapsed, 3))
            self.m.stages.append(row)
            print(json.dumps(row), flush=True)

    async def monitor(self):
        while not self.m.stop.is_set():
            await asyncio.sleep(1)
            now = time.monotonic()
            while self.m.recent and self.m.recent[0][0] < now-30:
                self.m.recent.popleft()
            if int(now-self.m.started) % 5 == 0:
                self.m.samples.flush()
                print(json.dumps({"phase": self.m.phase, "elapsed_s": round(now-self.m.started),
                    "connected": {k: len(v) for k, v in self.m.connected.items()},
                    "recent_requests": len(self.m.recent)}), flush=True)
            if self.m.phase != "connect" and any(len(v) < len(self.m.ready_users) for v in self.m.connected.values()):
                self.m.abort("an admitted SSE subscriber disconnected; inspect stream samples before increasing load")
            if self.m.phase in {"preflight", "connect", "burst"} or now-self.m.phase_started.get(self.m.phase, now) < 30:
                continue
            recent = list(self.m.recent)
            if recent:
                bad = sum(category in {"rate_limited", "server_error", "timeout", "network_error"}
                          for _, _, category, _ in recent)
                trades = [ms for _, name, category, ms in recent if name == "trade" and category == "ok"]
                if bad/len(recent) > .05:
                    self.m.abort("30s window: 429/5xx/network/timeout rate exceeds 5%")
                elif trades and percentiles(trades)["p99_ms"] > 2000:
                    self.m.abort("30s window: successful trade P99 exceeds 2000ms")

    async def run(self):
        snap = await self.request(None, "snapshot", "GET", f"/api/v1/fx/pairs/{self.pid}/snapshot")
        if not snap or snap.get("pair", {}).get("currency_code") != self.credentials["currency_code"] or not self.credentials["currency_code"].startswith("LT"):
            raise ValueError("target must match the dedicated test market in the token file")
        if snap["pair"]["status"] != "trading":
            raise ValueError("test market is not trading")
        if not snap.get("history_ready"):
            raise ValueError("market history is not ready; wait for backend startup to finish before testing")
        users = self.credentials["users"][:1 if self.args.mode == "smoke" else self.args.users]
        self.m.phase = "connect"
        streams, monitor = [], asyncio.create_task(self.monitor())
        try:
            for i, user in enumerate(users):
                if self.m.stop.is_set():
                    break
                # Spread initial context/chart/SSE setup; all originate from one IP.
                context = await self.request(user, "initial_context", "GET", "/api/v1/user/trading-context", params=self.group)
                if not context or context["account"]["user_id"] != user["uid"] or context["account"]["is_bot"]:
                    raise ValueError("test identity/context preflight failed")
                end = datetime.now(timezone.utc)
                chart = await self.request(user, "initial_chart", "GET", f"/api/v1/fx/pairs/{self.pid}/chart",
                    params={"interval": "1m", "from": (end-timedelta(hours=1)).isoformat(), "to": end.isoformat()})
                if chart is None:
                    raise ValueError("initial chart preflight failed; inspect errors before increasing load")
                ready = [asyncio.Event(), asyncio.Event()]
                streams.extend(asyncio.create_task(self.stream(user, kind, signal))
                               for kind, signal in zip(("public", "private"), ready))
                try:
                    await asyncio.wait_for(asyncio.gather(*(r.wait() for r in ready)), 20)
                except TimeoutError:
                    raise ValueError("SSE preflight did not receive both valid first frames") from None
                self.m.ready_users.add(user["uid"])
                if i == 0:
                    await self.stage("smoke", users[:1], self.args.smoke_seconds)
                    if self.m.counts[("smoke", "trade")]["ok"] < 2:
                        raise ValueError("smoke needs at least two successful trades (buy then sell); do not ramp")
                    smoke_errors = sum(v for (phase, _), counts in self.m.counts.items() if phase == "smoke"
                                       for category, v in counts.items() if category != "ok")
                    if smoke_errors or self.m.stop.is_set():
                        raise ValueError("smoke reported failures; do not ramp")
                    self.m.phase = "connect"
                if i+1 < len(users):
                    await asyncio.sleep(self.args.join_interval)
            if self.args.mode == "full":
                for n in sorted({min(n, len(users)) for n in (10, 30, self.args.users)}):
                    if self.m.stop.is_set():
                        break
                    await self.stage(f"ramp_{n}", users[:n], self.args.ramp_seconds)
                if not self.m.stop.is_set():
                    await self.stage("steady", users, self.args.steady_seconds)
                if not self.m.stop.is_set():
                    await self.stage("burst", users, self.args.burst_observe_seconds, burst=True)
        finally:
            for task in streams+[monitor]:
                task.cancel()
            await asyncio.gather(*streams, monitor, return_exceptions=True)


async def main(args):
    credentials = json.loads(Path(args.tokens).read_text())
    if datetime.fromisoformat(credentials["expires_at"]) <= datetime.now(timezone.utc):
        raise ValueError("tokens have expired; run mint on the server again")
    users = credentials["users"]
    required = 1 if args.mode == "smoke" else args.users
    if len(users) < required or len({u["uid"] for u in users}) != len(users) or len({u["token"] for u in users}) != len(users):
        raise ValueError("need enough distinct ordinary-user identities and tokens")
    amount = Decimal(args.amount)
    if not 1 <= args.users <= 200 or not 0 < args.min_interval <= args.max_interval or not amount.is_finite() or amount <= 0:
        raise ValueError("invalid user count, trading interval or amount")
    if min(args.smoke_seconds, args.ramp_seconds, args.steady_seconds, args.burst_observe_seconds) <= 0 or args.join_interval < 0:
        raise ValueError("durations must be positive and join interval nonnegative")
    parsed = urlparse(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("base URL must be an HTTP(S) origin without credentials or a path")
    directory = Path(args.output or f"loadtest/results/{credentials['run_id']}_{int(time.time())}")
    metrics = Metrics(directory)
    metadata = {"base_url": args.base_url, "run_id": credentials["run_id"], "pair_id": credentials["pair_id"],
        "mode": args.mode, "requested_users": required, "started_at": datetime.now(timezone.utc).isoformat(),
        "min_interval_s": args.min_interval, "max_interval_s": args.max_interval,
        "notes": ["Network latency is from this runner, not venue Wi-Fi.",
                  "Each cycle includes a server quote; the current UI can quote locally.",
                  "SSE event age depends on server/runner clock agreement; visibility uses one observer's monotonic clock.",
                  "No borrowing, cross-zero shorts, trigger orders or automatic retries."]}
    try:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=15,
                limits=httpx.Limits(max_connections=650, max_keepalive_connections=200),
                follow_redirects=False, trust_env=False) as client:
            await Runner(args, credentials, metrics, client).run()
    except (ValueError, KeyError, TypeError) as exc:
        metrics.abort(f"preflight/protocol error: {type(exc).__name__}: {exc}")
    except asyncio.CancelledError:
        metrics.abort("interrupted by operator")
        raise
    finally:
        report = metrics.summary(metadata)
        (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
        metrics.samples.close()
        print(f"Report: {directory / 'summary.json'}", flush=True)
    if metrics.reason:
        raise SystemExit(metrics.reason)


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://thccb.secret-sealing.club")
    parser.add_argument("--tokens", required=True, help="private tokens.json from administrator mint")
    parser.add_argument("--mode", choices=["smoke", "full"], default="smoke")
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--amount", default="10", help="gold budget per buy, sell uses the actual held balance")
    parser.add_argument("--min-interval", type=float, default=5)
    parser.add_argument("--max-interval", type=float, default=10)
    parser.add_argument("--join-interval", type=float, default=.3)
    parser.add_argument("--smoke-seconds", type=float, default=60)
    parser.add_argument("--ramp-seconds", type=float, default=60)
    parser.add_argument("--steady-seconds", type=float, default=300)
    parser.add_argument("--burst-observe-seconds", type=float, default=60)
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"fx-loadtest: {exc}\n")


if __name__ == "__main__":
    cli()
