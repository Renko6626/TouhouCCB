"""aiosqlite 封装：orders / decisions / daily_stats。spec §7。"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import aiosqlite

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    @classmethod
    async def open(cls, db_path: Path) -> "Store":
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        with _SCHEMA_PATH.open() as f:
            await conn.executescript(f.read())
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    async def log_order(
        self,
        *,
        strategy: str,
        outcome_id: int,
        side: str,
        shares: Decimal,
        price: Optional[Decimal] = None,
        cost: Optional[Decimal] = None,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO orders (ts, strategy, outcome_id, side, shares, price, cost, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _utcnow_iso(),
                strategy,
                outcome_id,
                side,
                str(shares),
                str(price) if price is not None else None,
                str(cost) if cost is not None else None,
                status,
                error,
            ),
        )
        await self._conn.commit()

    async def has_recent_duplicate(
        self,
        strategy: str,
        outcome_id: int,
        side: str,
        shares: Decimal,
        *,
        within_sec: int,
        statuses: tuple,
    ) -> bool:
        cutoff = datetime.now(timezone.utc).timestamp() - within_sec
        placeholders = ",".join("?" * len(statuses))
        cur = await self._conn.execute(
            f"SELECT ts FROM orders WHERE strategy = ? AND outcome_id = ? AND side = ? "
            f"AND shares = ? AND status IN ({placeholders})",
            (strategy, outcome_id, side, str(shares), *statuses),
        )
        rows = await cur.fetchall()
        for row in rows:
            ts = datetime.fromisoformat(row["ts"]).timestamp()
            if ts >= cutoff:
                return True
        return False

    async def recent_orders(self, *, strategy: Optional[str] = None, limit: int = 50) -> list[dict]:
        if strategy:
            cur = await self._conn.execute(
                "SELECT * FROM orders WHERE strategy = ? ORDER BY id DESC LIMIT ?",
                (strategy, limit),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in await cur.fetchall()]

    async def log_decision(
        self,
        *,
        strategy: str,
        outcome_id: Optional[int],
        action: str,
        reason: Optional[str] = None,
        snapshot: Optional[dict] = None,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO decisions (ts, strategy, outcome_id, action, reason, snapshot_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                _utcnow_iso(),
                strategy,
                outcome_id,
                action,
                reason,
                json.dumps(snapshot) if snapshot else None,
            ),
        )
        await self._conn.commit()

    async def recent_decisions(self, *, strategy: Optional[str] = None, limit: int = 50) -> list[dict]:
        if strategy:
            cur = await self._conn.execute(
                "SELECT * FROM decisions WHERE strategy = ? ORDER BY id DESC LIMIT ?",
                (strategy, limit),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in await cur.fetchall()]

    async def add_turnover(self, amount: Decimal) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        await self._conn.execute(
            "INSERT INTO daily_stats (date, gross_turnover, net_pnl) VALUES (?, ?, '0') "
            "ON CONFLICT(date) DO UPDATE SET gross_turnover = "
            "CAST((CAST(gross_turnover AS REAL) + ?) AS TEXT)",
            (today, str(amount), float(amount)),
        )
        await self._conn.commit()

    async def add_pnl(self, amount: Decimal) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        await self._conn.execute(
            "INSERT INTO daily_stats (date, gross_turnover, net_pnl) VALUES (?, '0', ?) "
            "ON CONFLICT(date) DO UPDATE SET net_pnl = "
            "CAST((CAST(net_pnl AS REAL) + ?) AS TEXT)",
            (today, str(amount), float(amount)),
        )
        await self._conn.commit()

    async def get_daily_stats(self, date: str) -> dict[str, Any]:
        cur = await self._conn.execute(
            "SELECT * FROM daily_stats WHERE date = ?", (date,)
        )
        row = await cur.fetchone()
        if row:
            return dict(row)
        return {"date": date, "gross_turnover": "0", "net_pnl": "0"}
