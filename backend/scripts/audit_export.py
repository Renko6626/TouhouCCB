"""导出审计事件流供离线研究 / 回测。

用法（在 backend/ 下，DATABASE_URL 指向目标库；生产请用只读副本或 pg_dump 恢复的本地库）：

  python scripts/audit_export.py --from 2026-08-25T00:00:00+08:00 --to 2026-08-26T00:00:00+08:00 --out ./export
  python scripts/audit_export.py --at 2026-08-25T12:00:00+08:00            # 打印 T 时刻全量快照（JSON）

输出（--out 目录）：
  events.jsonl        窗口内全部事件，逐行 JSON（payload / *_after 原样）
  user_state.csv      每条带 user_after 的事件展开一行：event_id, ts, type, user_id, cash, debt
  position_state.csv  每条带 position_after 的事件一行：event_id, ts, type, user_id, outcome_id, amount, cost_basis
  market_state.csv    每条带 market_after 的事件一行：event_id, ts, type, market_id, status, b, outcome_id, q, price（每个 outcome 一行）
  snapshot_start.json 窗口起点（--from 之前最后一条事件）折叠出的全量状态——回放窗口的初始状态

时间窗按事件 ts 过滤；起点快照按 id ≤ 窗口前最后一条事件折叠（同实体 id 序 = 提交序）。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select  # noqa: E402

from app.core.database import async_session_maker, engine  # noqa: E402
from app.models.audit import AuditEvent  # noqa: E402
from app.services import audit_replay  # noqa: E402


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise SystemExit(f"时间必须带时区，例如 2026-08-25T00:00:00+08:00（got {s}）")
    return dt


def _iso(dt: datetime) -> str:
    """SQLite 读回的是 naive UTC；PG 是 aware。统一输出带时区。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _ev_dict(e: AuditEvent) -> dict:
    return {
        "id": e.id, "ts": _iso(e.ts), "event_type": e.event_type,
        "user_id": e.user_id, "market_id": e.market_id, "outcome_id": e.outcome_id,
        "operator_user_id": e.operator_user_id, "ref_table": e.ref_table, "ref_id": e.ref_id,
        "payload": e.payload, "user_after": e.user_after,
        "position_after": e.position_after, "market_after": e.market_after,
    }


def _snap_dict(snap: audit_replay.Snapshot) -> dict:
    return {
        "last_event_id": snap.last_event_id,
        "users": {str(k): {"cash": str(v.cash), "debt": str(v.debt), "anchored": v.anchored}
                  for k, v in snap.users.items()},
        "positions": [{"user_id": k[0], "outcome_id": k[1], "amount": str(v)}
                      for k, v in snap.positions.items() if v != 0],
        "markets": {str(k): {"outcome_ids": v.outcome_ids, "q": [str(x) for x in v.q],
                             "b": v.b, "prices": v.prices, "status": v.status, "anchored": v.anchored}
                    for k, v in snap.markets.items()},
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="ts_from", help="窗口起点（含），ISO 带时区")
    ap.add_argument("--to", dest="ts_to", help="窗口终点（含），ISO 带时区")
    ap.add_argument("--at", help="只打印 T 时刻全量快照")
    ap.add_argument("--out", default="./audit_export", help="输出目录")
    args = ap.parse_args()

    async with async_session_maker() as s:
        if args.at:
            evs = await audit_replay.load_events(s, upto_ts=_parse_ts(args.at))
            snap, _ = audit_replay.fold(evs)
            print(json.dumps(_snap_dict(snap), ensure_ascii=False, indent=2))
            return 0

        ts_from = _parse_ts(args.ts_from) if args.ts_from else None
        ts_to = _parse_ts(args.ts_to) if args.ts_to else None
        os.makedirs(args.out, exist_ok=True)

        # 起点快照：窗口前全部事件折叠
        if ts_from is not None:
            before = (await s.execute(
                select(AuditEvent).where(AuditEvent.ts < ts_from).order_by(AuditEvent.id.asc())
            )).scalars().all()
            snap0, _ = audit_replay.fold(before)
        else:
            snap0 = audit_replay.Snapshot()
        with open(os.path.join(args.out, "snapshot_start.json"), "w", encoding="utf-8") as f:
            json.dump(_snap_dict(snap0), f, ensure_ascii=False, indent=2)

        evs = await audit_replay.load_events(s, since_ts=ts_from, upto_ts=ts_to)

    with open(os.path.join(args.out, "events.jsonl"), "w", encoding="utf-8") as f:
        for e in evs:
            f.write(json.dumps(_ev_dict(e), ensure_ascii=False) + "\n")

    with open(os.path.join(args.out, "user_state.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["event_id", "ts", "event_type", "user_id", "cash", "debt"])
        for e in evs:
            if e.user_after is not None and e.user_id is not None:
                w.writerow([e.id, _iso(e.ts), e.event_type, e.user_id,
                            e.user_after.get("cash"), e.user_after.get("debt")])

    with open(os.path.join(args.out, "position_state.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["event_id", "ts", "event_type", "user_id", "outcome_id", "amount", "cost_basis"])
        for e in evs:
            if e.position_after is not None:
                w.writerow([e.id, _iso(e.ts), e.event_type, e.user_id, e.outcome_id,
                            e.position_after.get("amount"), e.position_after.get("cost_basis")])

    with open(os.path.join(args.out, "market_state.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["event_id", "ts", "event_type", "market_id", "status", "b", "outcome_id", "q", "price"])
        for e in evs:
            if e.market_after is not None:
                ma = e.market_after
                for oid, q, p in zip(ma.get("outcome_ids", []), ma.get("q", []), ma.get("prices", [])):
                    w.writerow([e.id, _iso(e.ts), e.event_type, e.market_id,
                                ma.get("status"), ma.get("b"), oid, q, p])

    print(f"exported {len(evs)} events → {args.out}/ (snapshot_start last_event_id={snap0.last_event_id})")
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        asyncio.run(engine.dispose())
    sys.exit(code)
