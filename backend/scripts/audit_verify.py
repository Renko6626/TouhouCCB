"""审计事件流自检：从 seq=1 折叠 + 独立增量校验，并与线上 user / position / outcome 表比对。

  python scripts/audit_verify.py            # 全量
  python scripts/audit_verify.py --upto-id 12345

退出码：0 一致；1 有 mismatch（逐条打印）。
增量校验只对「锚定」实体生效（从 user_register / market_create 起有完整记录）；
审计上线前已存在的用户/市场只做快照对齐与线上比对，会在统计里标出。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import async_session_maker, engine  # noqa: E402
from app.services import audit_replay  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upto-id", type=int, default=None)
    ap.add_argument("--no-live", action="store_true", help="不与线上表比对（只做事件流内部自洽）")
    args = ap.parse_args()

    async with async_session_maker() as s:
        evs = await audit_replay.load_events(s, upto_id=args.upto_id)
        snap, mism = audit_replay.fold(evs, check=True)
        live = [] if args.no_live else await audit_replay.compare_with_live(s, snap)

    n_anch_u = sum(1 for u in snap.users.values() if u.anchored)
    n_anch_m = sum(1 for m in snap.markets.values() if m.anchored)
    print(f"events={len(evs)} last_id={snap.last_event_id} "
          f"users={len(snap.users)} (anchored {n_anch_u}) "
          f"markets={len(snap.markets)} (anchored {n_anch_m}) "
          f"positions={sum(1 for v in snap.positions.values() if v != 0)}")
    for m in mism:
        print(f"[replay] event#{m.event_id} {m.event_type} {m.entity}.{m.field}: expected={m.expected} actual={m.actual}")
    for m in live:
        print(f"[live]   after#{m.event_id} {m.entity}.{m.field}: folded={m.expected} live={m.actual}")
    if mism or live:
        print(f"MISMATCH replay={len(mism)} live={len(live)}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        asyncio.run(engine.dispose())
    sys.exit(code)
