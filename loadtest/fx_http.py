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

ROLE_PATTERN = ("cash", "finance", "cash", "short", "cash", "cash", "finance", "cash", "short", "cash")
HISTORY = {"1m": (60, 3600, 480), "15m": (900, 86400, 7200)}


def covers_stamp(current, required):
    """Same transport coverage rule as tradingSession.ts, without pricing maths."""
    if not current or current["epoch"] != required["epoch"]:
        return False
    local, wanted = int(current["revision"]), int(required["revision"])
    return local > wanted or local == wanted and current["input_hash"] == required["input_hash"]


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
        self.pool_event_seen = {}  # Current FX wire protocol identifies pool events.
        self.visibility = defaultdict(list)
        self.visibility_missing = Counter()
        self.slot_lag = defaultdict(list)
        self.trade_targets = Counter()
        self.phase_started = {}
        self.actions = defaultdict(Counter)
        self.action_latencies = defaultdict(list)
        self.action_targets = Counter()
        self.burst_submissions = []
        self.order_results = {}
        self.coverage = Counter()
        self.stage_metadata = {}

    def record(self, name, category, ms, status=None, detail=None, action=None):
        now = time.monotonic()
        phase = self.phase
        self.counts[(phase, name)][category] += 1
        self.latencies[(phase, name, category)].append(ms)
        self.recent.append((now, name, category, ms))
        self.samples.write(json.dumps({"t_s": round(now-self.started, 3), "phase": phase,
            "name": name, "category": category, "status": status, "ms": round(ms, 3),
            "detail": detail, "action": action}) + "\n")
        if action:
            self.actions[(phase, action)][category] += 1
            self.action_latencies[(phase, action, category)].append(ms)
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
                "requests": rows, "coverage": dict(self.coverage), "scenario_details": self.stage_metadata,
                "actions": [{"phase": phase, "action": action,
                             "planned_attempts": self.action_targets[(phase, action)],
                             "write_results": dict(self.actions[(phase, action)]), "latency_by_result": {
                                 c: percentiles(self.action_latencies[(phase, action, c)]) for c in self.actions[(phase, action)]}}
                            for phase, action in sorted(self.action_targets.keys() | self.actions.keys())],
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
        self.contexts, self.envelopes, self.chart_intervals, self.chart_boundaries = {}, {}, {}, {}
        self.locks = defaultdict(asyncio.Lock)
        self.account_events = defaultdict(asyncio.Event)
        self.target_versions = defaultdict(int)
        self.roles = {u["uid"]: ROLE_PATTERN[i % 10] for i, u in enumerate(credentials.get("users", []))}
        shorts = [u["uid"] for u in credentials.get("users", []) if self.roles[u["uid"]] == "short"]
        self.cross_users = set(shorts[::4])
        self.cross_pending = set()
        self.observations = []
        self.last_status = {}

    async def request(self, user, name, method, path, *, body=None, params=None, action=None,
                      allow_missing=False, chart_meta=False):
        start = time.monotonic()
        headers = {"Authorization": "Bearer " + user["token"]} if user else {}
        try:
            response = await self.client.request(method, path, headers=headers, json=body, params=params)
            self.last_status[(user["uid"] if user else None, name)] = response.status_code
            if response.status_code == 404 and allow_missing:
                self.m.record(name, "expected_missing", (time.monotonic()-start)*1000, 404)
                return None
            try:
                data = response.json()
            except ValueError:
                category = classify(response.status_code) if response.status_code >= 400 else "invalid_json"
                self.m.record(name, category, (time.monotonic()-start)*1000, response.status_code, action=action)
                return None
            detail = data.get("detail") if isinstance(data, dict) else None
            category = classify(response.status_code, detail)
            if category == "ok" and chart_meta and (not response.headers.get("X-FX-History-Version") or
                    not response.headers.get("X-FX-Through-Event-ID", "").isdigit()):
                category = "invalid_chart_metadata"
            if category == "ok" and name == "trade" and (not isinstance(data, dict) or not (
                    data.get("trade_ids") or data.get("trade_id"))):
                category = "invalid_receipt"
            # Only stable string error markers, never arbitrary response bodies.
            marker = detail[:160] if isinstance(detail, str) else None
            self.m.record(name, category, (time.monotonic()-start)*1000, response.status_code, marker, action)
            return data if category == "ok" else None
        except httpx.TimeoutException:
            self.m.record(name, "timeout", (time.monotonic()-start)*1000, action=action)
        except httpx.RequestError:
            self.m.record(name, "network_error", (time.monotonic()-start)*1000, action=action)
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
                        self.m.sse_counts[kind]["payload_bytes"] += sum(len(c.encode()) for c in chunks)
                        data = payload.get("data", payload)
                        if kind == "public":
                            self.envelopes.setdefault(user["uid"], {}).update(data)
                        elif event in {"sync", "pricing"} and user["uid"] in self.contexts:
                            context = self.contexts[user["uid"]]
                            groups = {(g["product"], g["id"]): g for g in context.get("groups", [])}
                            for g in data.get("groups", []):
                                groups[(g["product"], g["id"])] = g
                            for g in data.get("missing_groups", []):
                                groups.pop((g["product"], g["id"]), None)
                            context["groups"] = list(groups.values())
                        elif event == "account_changed":
                            self.target_versions[user["uid"]] = max(self.target_versions[user["uid"]], int(data["economic_version"]))
                            self.account_events[user["uid"]].set()
                        elif event == "fx_order_result":
                            self.m.order_results[int(data["order_id"])] = data["status"]
                            self.m.coverage["order_private_result"] += 1
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
                            for pool_event in data.get("events", []):
                                self.m.pool_event_seen.setdefault(int(pool_event["id"]), now)
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

    async def context(self, user, name="context"):
        result = await self.request(user, name, "GET", "/api/v1/user/trading-context", params=self.group)
        if result:
            self.contexts[user["uid"]] = result
        return result

    async def pause(self, seconds):
        if seconds > 0:
            try:
                await asyncio.wait_for(self.m.stop.wait(), seconds)
            except TimeoutError:
                pass

    def positions(self, user):
        account = self.contexts[user["uid"]]["account"]
        wallet = next((w for w in account["fx_wallets"] if w["pair_id"] == self.pid), {})
        short = next((s for s in account["fx_shorts"] if s["pair_id"] == self.pid), {})
        return account, Decimal(wallet.get("foreign_amount", "0")), Decimal(short.get("principal_foreign", "0")) + Decimal(short.get("interest_foreign", "0"))

    async def history(self, user, *, full=False, interval=None):
        uid = user["uid"]
        interval = interval or self.chart_intervals.get(uid, "1m")
        self.chart_intervals[uid] = interval
        step, segment, lookback = HISTORY[interval]
        now = int(time.time())
        boundary = now - now % segment
        full = full or self.chart_boundaries.get(uid) != boundary
        self.chart_boundaries[uid] = boundary
        envelope = self.envelopes.get(uid, {})
        version = envelope.get("history_version")
        missing = False
        if full and version and envelope.get("history_ready"):
            first = now-lookback*60
            first -= first % segment
            for epoch in range(first, boundary, segment):
                data = await self.request(None, "history_segment", "GET",
                    f"/history/fx/{self.pid}/{version}/{interval}/{epoch}.json", allow_missing=True)
                if data is None:
                    missing = True
                    break
                if data.get("t0") != epoch or data.get("step") != step:
                    raise ValueError("history segment is misaligned")
        tail = envelope.get("history_tail", {}).get(interval, {})
        tail_at = envelope.get("history_tail_at")
        fresh = False
        if tail_at:
            age = time.time() - datetime.fromisoformat(tail_at.replace("Z", "+00:00")).timestamp()
            fresh = (0 <= age < step and tail.get("t0") == boundary and
                     not envelope.get("history_invalidated") and envelope.get("history_ready") and
                     isinstance(envelope.get("history_tail_through_event_id"), int))
        if fresh and not missing:
            self.m.coverage["chart_sse_tail_reused"] += 1
            return
        from_s = now-lookback*60 if missing or not version else boundary
        result = await self.request(None, "chart_full" if full and (missing or not version) else "chart_tail",
            "GET", f"/api/v1/fx/pairs/{self.pid}/chart", chart_meta=True,
            params={"interval": interval, "from": datetime.fromtimestamp(from_s, timezone.utc).isoformat(),
                    "to": datetime.fromtimestamp(now, timezone.utc).isoformat()})
        if result is None:
            raise ValueError("chart preflight/refresh failed")

    async def refresh_page(self, user):
        # These are the three reads made by refreshAll after a real page trade.
        await asyncio.gather(self.context(user, "context_after_trade"),
            self.request(user, "snapshot_after_trade", "GET", f"/api/v1/fx/pairs/{self.pid}/snapshot"),
            self.request(user, "my_trades_after_trade", "GET", f"/api/v1/fx/pairs/{self.pid}/my-trades", params={"limit": 50}))
        await self.history(user)

    async def prepare_order(self, user, *, override=None):
        if user["uid"] not in self.contexts and not await self.context(user):
            return None
        account, held, debt = self.positions(user)
        role = self.roles.get(user["uid"], "cash")
        budget = Decimal(self.args.amount) * Decimal(str(random.uniform(1, 5)))
        budget = budget.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        root = f"/api/v1/fx/pairs/{self.pid}"
        if debt and override is None:
            action = "short_cover"
            self.m.action_targets[(self.m.phase, action)] += 1
            quote = await self.request(user, "quote_short_cover", "POST", root+"/short/quote",
                body={"action": "cover", "cover_all": True})
            if not quote or not quote.get("executable") or not quote.get("input_amount"):
                self.m.record("cycle", "quote_blocked", 0, detail="short cover unavailable")
                return None
            body = {"cover_all": True, "max_gold_in": str((Decimal(quote["input_amount"])*Decimal("1.05")).quantize(
                Decimal("0.000001"), rounding=ROUND_DOWN)), "idempotency_key": f"lt:{uuid4().hex}"}
            return action, root+"/short/cover", body
        cross = False
        side = override or ("sell" if held > 0 else "buy")
        borrow = Decimal("0")
        amount = held if side == "sell" else min(budget, Decimal(account["cash"]))
        if override is None and role == "short":
            price = Decimal(self.envelopes[user["uid"]]["price"])
            if held > 0 and user["uid"] in self.cross_pending:
                side, amount, cross = "sell", held+budget/price, True
            elif held == 0 and user["uid"] in self.cross_users:
                side, amount = "buy", budget
            elif held == 0:
                side, amount = "sell", budget/price
        if override is None and role == "finance" and side == "buy":
            amount, borrow = budget, budget/2
        amount = amount.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        borrow = borrow.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        action = ("cross_zero" if cross else "short_open" if side == "sell" and amount > held else
                  "financed_buy" if borrow else "cash_buy" if side == "buy" else
                  "sell_repay" if Decimal(account["debt"]) else "cash_sell")
        self.m.action_targets[(self.m.phase, action)] += 1
        if amount <= 0:
            self.m.record("cycle", "no_funds", 0)
            return None
        body = {"side": side, "amount": str(amount), "borrow_amount": str(borrow)}
        quote = await self.request(user, "quote_"+action, "POST", root+"/net/quote", body=body)
        if not quote or not quote.get("executable"):
            self.m.record("cycle", "quote_blocked", 0, detail=quote.get("blocked_reason") if quote else None)
            return None
        body.update(min_out=str((Decimal(quote["output_amount"])*Decimal("0.95")).quantize(
            Decimal("0.000001"), rounding=ROUND_DOWN)), expected_economic_version=quote["economic_version"],
            allow_growth=side == "buy" or amount > held, idempotency_key=f"lt:{uuid4().hex}")
        return action, root+"/net/trades", body

    async def execute_order(self, user, order):
        if order is None:
            return None
        action, path, body = order
        start, phase = time.monotonic(), self.m.phase
        if phase == "burst":
            self.m.burst_submissions.append(start)
        result = await self.request(user, "trade", "POST", path, body=body, action=action)
        if result:
            ids = result.get("trade_ids") or [result["trade_id"]]
            self.m.coverage[action] += 1
            if action == "cross_zero" and len(ids) != 2:
                raise ValueError("cross-zero execution did not produce two real legs")
            if user["uid"] in self.cross_users:
                if action == "cash_buy":
                    self.cross_pending.add(user["uid"])
                elif action == "cross_zero":
                    self.cross_pending.discard(user["uid"])
            self.m.visibility[phase]
            observation = (phase, start, ids)
            self.observations.append(observation)
            await self.refresh_page(user)
            return observation
        # The real page refreshes stale state after an error; the write is not retried.
        await self.context(user, "context_after_rejection")
        return None

    async def trade(self, user, *, force_buy=False):
        async with self.locks[user["uid"]]:
            return await self.execute_order(user, await self.prepare_order(user, override="buy" if force_buy else None))

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
            result = await self.request(user, "versions", "GET", "/api/v1/user/trading-versions", params=self.group)
            if result:
                context = self.contexts.get(user["uid"], {})
                if int(result["economic_version"]) > int(context.get("account", {}).get("economic_version", -1)):
                    self.target_versions[user["uid"]] = int(result["economic_version"])
                    self.account_events[user["uid"]].set()
                known = {(g["product"], g["id"]): g["stamp"] for g in context.get("groups", [])}
                required = [g for g in result["group_stamps"] if not covers_stamp(known.get((g["product"], g["id"])), g["stamp"])]
                if required or result.get("missing_groups"):
                    prices = await self.request(user, "prices_repair", "GET", "/api/v1/user/trading-prices",
                        params={**self.group, "key": [f"{g['product']}:{g['id']}" for g in required]})
                    if prices and context:
                        context["groups"] = prices["groups"]
            await asyncio.sleep(min(15, max(0, deadline-time.monotonic())))

    async def account_refreshes(self, user):
        uid = user["uid"]
        while not self.m.stop.is_set():
            await self.account_events[uid].wait()
            await asyncio.sleep(.2)
            self.account_events[uid].clear()
            async with self.locks[uid]:
                cached = self.contexts.get(uid, {}).get("account", {}).get("economic_version", -1)
                if int(cached) < self.target_versions[uid]:
                    await self.context(user, "context_account_event")

    def activity(self, users, profile):
        n = len(users)
        fast, slow, intervals = {
            "normal": (round(n*.4), round(n*.3), ((15, 25), (45, 75))),
            "active": (round(n*.7), round(n*.2), ((8, 12), (30, 50))),
            "stress": (n, 0, ((3, 5), (3, 5))),
            "readonly": (0, 0, ((1, 1), (1, 1))),
        }[profile]
        fast = min(n, max(1, fast)) if profile != "readonly" else 0
        result = {}
        for i, user in enumerate(users):
            if i < fast:
                result[user["uid"]] = intervals[0]
            elif i < fast+slow:
                result[user["uid"]] = intervals[1]
        return result

    async def stage(self, name, users, seconds, *, burst=False, activity=None, orders=None):
        self.m.phase = name
        start = time.monotonic()
        self.m.phase_started[name] = start
        deadline = start+seconds
        self.m.recent.clear()
        observations = []
        completed = set()
        sse_start = {kind: dict(self.m.sse_counts[kind]) for kind in ("public", "private")}
        async def worker(user, i):
            interval = activity.get(user["uid"]) if activity is not None else (self.args.min_interval, self.args.max_interval)
            if interval is None and not burst:
                return
            slot = start + (2*i/max(1, len(users)-1) if burst else random.uniform(0, min(sum(interval)/2, seconds/2)))
            while slot < deadline and not self.m.stop.is_set():
                await self.pause(max(0, slot-time.monotonic()))
                if self.m.stop.is_set() or time.monotonic() >= deadline:
                    break
                actual_start = time.monotonic()
                self.m.slot_lag[name].append((actual_start-slot)*1000)
                self.m.trade_targets[name] += 1
                if orders is not None:
                    async with self.locks[user["uid"]]:
                        self.m.action_targets[(name, orders[user["uid"]][0])] += 1
                        result = await self.execute_order(user, orders[user["uid"]])
                else:
                    result = await self.trade(user, force_buy=burst)
                completed.add(user["uid"])
                if result:
                    observations.append(result)
                if burst:
                    break
                # Rebase on this cycle's actual start (no accumulated catch-up
                # backlog), but preserve an overdue next slot so its delay is
                # measured rather than erased when the server is slow.
                slot = actual_start + random.uniform(*interval)
        phase_stop = asyncio.Event()
        traders = [asyncio.create_task(worker(u, i)) for i, u in enumerate(users)]
        # Keep normal version probes during burst recovery, after dispatch too.
        readers = [asyncio.create_task(self.versions(u, deadline, phase_stop)) for u in users]
        try:
            # Keep observing for the full stage (including the burst recovery window).
            while time.monotonic() < deadline and not self.m.stop.is_set():
                await asyncio.sleep(min(1, max(0, deadline-time.monotonic())))
                if any(t.done() and not t.cancelled() and t.exception() for t in traders):
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
            row.update(self.m.stage_metadata.get(name, {}))
            if burst and self.m.burst_submissions:
                row["client_dispatch_window_s"] = round(max(self.m.burst_submissions)-min(self.m.burst_submissions), 3)
            if activity is not None:
                row["nominal_trade_cycles_per_s"] = round(sum(2/sum(v) for v in activity.values()), 3)
                row["scheduled_trading_users"] = len(activity)
            row["sse_received"] = {kind: {key: self.m.sse_counts[kind][key]-sse_start[kind].get(key, 0)
                                          for key in ("frames", "payload_bytes")} for kind in sse_start}
            completed_requests = sum(sum(counts.values()) for (phase, endpoint), counts in self.m.counts.items()
                                     if phase == name and endpoint != "cycle" and not endpoint.startswith("sse_"))
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
            if self.m.phase not in {"readonly", "normal", "active", "stress"} or now-self.m.phase_started.get(self.m.phase, now) < 30:
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

    async def connect_user(self, user, streams):
        context = await self.context(user, "initial_context")
        if not context or context["account"]["user_id"] != user["uid"] or context["account"]["is_bot"]:
            raise ValueError("test identity/context preflight failed")
        for name, path, params in (
            ("initial_pairs", "/api/v1/fx/pairs", None),
            ("initial_announcement", "/api/v1/fx/announcements/latest", None),
            ("initial_snapshot", f"/api/v1/fx/pairs/{self.pid}/snapshot", None),
            ("initial_my_trades", f"/api/v1/fx/pairs/{self.pid}/my-trades", {"limit": 50}),
            ("initial_orders_waiting", "/api/v1/fx/orders", {"status": "waiting", "limit": 100}),
            ("initial_orders_history", "/api/v1/fx/orders", {"status": "succeeded", "limit": 20}),
            ("initial_order_pairs", "/api/v1/fx/pairs", None),
        ):
            before = sum(self.m.counts[(self.m.phase, name)].values())
            await self.request(user, name, "GET", path, params=params)
            if self.m.counts[(self.m.phase, name)]["ok"] <= before:
                raise ValueError(f"entry preflight failed: {name}")
        ready = [asyncio.Event(), asyncio.Event()]
        streams.extend(asyncio.create_task(self.stream(user, kind, signal))
                       for kind, signal in zip(("public", "private"), ready))
        try:
            await asyncio.wait_for(asyncio.gather(*(r.wait() for r in ready)), 20)
        except TimeoutError:
            raise ValueError("SSE preflight did not receive both valid first frames") from None
        self.m.ready_users.add(user["uid"])
        streams.append(asyncio.create_task(self.account_refreshes(user)))
        await self.history(user, full=True)

    async def prepare_roles(self, users):
        self.m.phase = "role_prepare"
        for user in users:
            role = self.roles[user["uid"]]
            if role == "cash":
                continue
            rules = self.contexts[user["uid"]]["rules"]
            if not rules["loan_enabled"] or (role == "short" and not rules["fx_short_enabled"]):
                raise ValueError("mixed scenario needs loan_enabled and fx_short_enabled; use --cash-only explicitly for a spot baseline")
            # A repeated test may leave inventory. Establish the role's starting
            # position through ordinary reducing writes, never fake DB balances.
            _, held, debt = self.positions(user)
            if debt and not await self.trade(user):
                raise ValueError("role preparation could not cover existing short")
            _, held, _ = self.positions(user)
            if held:
                async with self.locks[user["uid"]]:
                    if not await self.execute_order(user, await self.prepare_order(user, override="sell")):
                        raise ValueError("role preparation could not sell existing long")
            # Real API writes prove each role is admitted before the measured phases.
            steps = 3 if user["uid"] in self.cross_users else 2
            for _ in range(steps):
                if self.m.stop.is_set():
                    return
                if not await self.trade(user):
                    raise ValueError(f"role preparation failed: {role}; no automatic retries")
        expected = {"financed_buy", "sell_repay"} if any(self.roles[u["uid"]] == "finance" for u in users) else set()
        if any(self.roles[u["uid"]] == "short" for u in users):
            expected.add("short_cover")
        if any(u["uid"] in self.cross_users for u in users):
            expected.add("cross_zero")
        missing = sorted(a for a in expected if not self.m.coverage[a])
        if missing:
            raise ValueError("role preparation did not exercise: "+", ".join(missing))

    async def burst_orders(self, users):
        self.m.phase = "burst_prepare"
        # Cash-side burst: close shorts first and ensure sell-side accounts own
        # real inventory. These real warmup writes are outside the two-second window.
        for i, user in enumerate(users):
            async with self.locks[user["uid"]]:
                _, held, debt = self.positions(user)
                if debt:
                    if not await self.execute_order(user, await self.prepare_order(user)):
                        raise ValueError("burst preparation could not cover short")
                _, held, _ = self.positions(user)
                if i % 2 and held <= 0:
                    if not await self.execute_order(user, await self.prepare_order(user, override="buy")):
                        raise ValueError("burst sell-side inventory preparation failed")
        semaphore = asyncio.Semaphore(8)
        async def quote(user, i):
            async with semaphore, self.locks[user["uid"]]:
                result = await self.prepare_order(user, override="sell" if i % 2 else "buy")
                if result is None:
                    raise ValueError("burst quote preparation failed")
                return user["uid"], result
        pairs = await asyncio.gather(*(quote(u, i) for i, u in enumerate(users)))
        return dict(pairs)

    async def orders_scenario(self, users):
        self.m.phase = "orders_prepare"
        selected = [u for u in users if self.roles[u["uid"]] == "cash"][:20]
        cancel_count = (len(selected)+1)//2
        tracked = []
        for i, user in enumerate(selected):
            if self.m.stop.is_set():
                return
            snapshot = await self.request(user, "order_snapshot", "GET", f"/api/v1/fx/pairs/{self.pid}/snapshot")
            if snapshot is None:
                raise ValueError("order preparation snapshot failed")
            trigger = (Decimal(snapshot["price"])*10).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
            order = await self.request(user, "order_create", "POST", "/api/v1/fx/orders", body={
                "pair_id": self.pid, "action": "buy", "amount": self.args.amount,
                "comparison": "gte" if i < cancel_count else "lte", "trigger_price": str(trigger),
                "auto_borrow": False, "borrow_limit": "0"})
            if order is None:
                raise ValueError("order creation failed")
            self.m.coverage["order_create"] += 1
            tracked.append((user, order["id"], i < cancel_count))
            if i < cancel_count:
                result = await self.request(user, "order_cancel", "POST", f"/api/v1/fx/orders/{order['id']}/cancel", body={})
                if result:
                    self.m.coverage["order_cancel_"+result["status"]] += 1
        await self.stage("orders_observe", users, self.args.orders_seconds, activity={})
        self.m.phase = "orders_results"
        states = Counter()
        for user, oid, requested_cancel in tracked:
            rows = await self.request(user, "order_results", "GET", "/api/v1/fx/orders", params={"limit": 100})
            if rows is None:
                continue
            order = next((row for row in rows if row["id"] == oid), None)
            if order:
                states[order["status"]] += 1
                if order["status"] == "succeeded":
                    self.m.coverage["order_executed"] += 1
                    await self.refresh_page(user)
                elif order["status"] == "waiting" and not requested_cancel:
                    self.m.coverage["order_trigger_not_observed"] += 1
            await self.request(user, "orders_waiting_refresh", "GET", "/api/v1/fx/orders", params={"status": "waiting", "limit": 100})
            await self.request(user, "order_pairs_refresh", "GET", "/api/v1/fx/pairs")
        self.m.stage_metadata["orders_results"] = {"states": dict(states),
            "private_results_received": sum(oid in self.m.order_results for _, oid, _ in tracked),
            "created": len(tracked)}
        if tracked and not states["succeeded"]:
            self.m.coverage["order_execution_not_covered"] += 1

    async def run(self):
        snap = await self.request(None, "snapshot", "GET", f"/api/v1/fx/pairs/{self.pid}/snapshot")
        if not snap or snap.get("pair", {}).get("currency_code") != self.credentials["currency_code"] or not self.credentials["currency_code"].startswith("LT"):
            raise ValueError("target must match the dedicated test market in the token file")
        if snap["pair"]["status"] != "trading" or not snap.get("history_ready"):
            raise ValueError("test market must be trading with history ready")
        users = self.credentials["users"][:1 if self.args.mode == "smoke" else self.args.users]
        if self.args.cash_only:
            self.roles = {u["uid"]: "cash" for u in users}
            self.cross_users.clear()
        self.m.phase = "connect"
        streams, monitor = [], asyncio.create_task(self.monitor())
        try:
            for i, user in enumerate(users):
                if self.m.stop.is_set():
                    break
                await self.connect_user(user, streams)
                if i == 0:
                    await self.stage("smoke", users[:1], self.args.smoke_seconds)
                    if not self.m.coverage["cash_buy"] or not self.m.coverage["cash_sell"]:
                        raise ValueError("smoke needs a real buy and sell; do not increase load")
                    errors = sum(v for (phase, _), counts in self.m.counts.items() if phase == "smoke"
                                 for category, v in counts.items() if category not in {"ok", "expected_missing"})
                    if errors or self.m.stop.is_set():
                        raise ValueError("smoke reported failures; do not increase load")
                    self.m.phase = "connect"
                if i+1 < len(users):
                    await asyncio.sleep(self.args.join_interval)
            if self.args.mode == "full" and not self.m.stop.is_set():
                await self.prepare_roles(users)
                for name, seconds in (("readonly", self.args.readonly_seconds), ("normal", self.args.normal_seconds),
                        ("active", self.args.active_seconds), ("stress", self.args.stress_seconds)):
                    if self.m.stop.is_set():
                        break
                    if name == "active":
                        self.m.phase = "chart_switch"
                        for user in users[::10]:
                            await self.history(user, full=True, interval="15m")
                    await self.stage(name, users, seconds, activity=self.activity(users, name))
                if not self.m.stop.is_set():
                    orders = await self.burst_orders(users)
                    await self.stage("burst", users, self.args.burst_observe_seconds, burst=True, orders=orders)
                if not self.m.stop.is_set() and self.args.orders_seconds:
                    await self.orders_scenario(users)
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
    if min(args.smoke_seconds, args.readonly_seconds, args.normal_seconds, args.active_seconds, args.stress_seconds, args.burst_observe_seconds) <= 0 or args.join_interval < 0 or args.orders_seconds < 0:
        raise ValueError("durations must be positive and join interval nonnegative")
    parsed = urlparse(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("base URL must be an HTTP(S) origin without credentials or a path")
    directory = Path(args.output or f"loadtest/results/{credentials['run_id']}_{int(time.time())}")
    metrics = Metrics(directory)
    metadata = {"base_url": args.base_url, "run_id": credentials["run_id"], "pair_id": credentials["pair_id"],
        "mode": args.mode, "requested_users": required, "started_at": datetime.now(timezone.utc).isoformat(),
        "buy_budget_range_gold": [args.amount, str(Decimal(args.amount)*5)],
        "orders_enabled": args.mode == "full" and args.orders_seconds > 0,
        "configured_seconds": {name: getattr(args, name+"_seconds") for name in
                               ("smoke", "readonly", "normal", "active", "stress", "burst_observe", "orders")},
        "min_interval_s": args.min_interval, "max_interval_s": args.max_interval, "cash_only": args.cash_only,
        "roles": dict(Counter(ROLE_PATTERN[i % 10] if not args.cash_only else "cash" for i in range(required))),
        "notes": ["Network latency is from this runner, not venue Wi-Fi.",
                  "Each cycle includes a server quote; the current UI can quote locally.",
                  "SSE event age depends on server/runner clock agreement; visibility uses one observer's monotonic clock.",
                  "60/20/20 cash, financing and short roles unless --cash-only; all preparation writes reported separately.",
                  "No automatic write retries; new test-market history is shallow."]}
    try:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=15,
                limits=httpx.Limits(max_connections=650, max_keepalive_connections=200),
                follow_redirects=False, trust_env=False) as client:
            await Runner(args, credentials, metrics, client).run()
    except (ValueError, KeyError, TypeError) as exc:
        metrics.abort(f"preflight/protocol error: {type(exc).__name__}: {exc}")
    except Exception as exc:
        metrics.abort(f"driver error: {type(exc).__name__}; inspect local logs before another run")
        raise
    except asyncio.CancelledError:
        metrics.abort("interrupted by operator")
        raise
    finally:
        report = metrics.summary(metadata)
        (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
        lines = ["# FX load-test report", "", f"Target: {args.base_url}",
                 f"Abort: {metrics.reason or 'none'}", "",
                 "| Stage | Active users | Successful trades/s | HTTP/s |", "| --- | ---: | ---: | ---: |"]
        lines.extend(f"| {s['name']} | {s['active_users']} | {s['successful_trades_per_s']} | {s['completed_requests_per_s']} |" for s in metrics.stages)
        lines += ["", "| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |",
                  "| --- | --- | ---: | ---: |"]
        for row in report["actions"]:
            latency = row["latency_by_result"].get("ok", {})
            lines.append(f"| {row['phase']} / {row['action']} | {dict(row['write_results'])} | {latency.get('p95_ms', '—')} | {latency.get('p99_ms', '—')} |")
        lines += ["", "Coverage: `"+json.dumps(report["coverage"])+"`", "", "See summary.json and requests.jsonl for errors and sampling boundaries."]
        (directory / "summary.md").write_text("\n".join(lines)+"\n")
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
    parser.add_argument("--min-interval", type=float, default=5, help="smoke cadence only")
    parser.add_argument("--max-interval", type=float, default=10)
    parser.add_argument("--join-interval", type=float, default=.6)
    parser.add_argument("--smoke-seconds", type=float, default=60)
    parser.add_argument("--readonly-seconds", type=float, default=120)
    parser.add_argument("--normal-seconds", type=float, default=300)
    parser.add_argument("--active-seconds", type=float, default=300)
    parser.add_argument("--stress-seconds", type=float, default=120)
    parser.add_argument("--orders-seconds", type=float, default=120, help="0 explicitly skips the trigger-order scenario")
    parser.add_argument("--cash-only", action="store_true", help="explicitly omit financed and short roles")
    parser.add_argument("--burst-observe-seconds", type=float, default=60)
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"fx-loadtest: {exc}\n")


if __name__ == "__main__":
    cli()
