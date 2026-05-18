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
        cur = await self._conn.execute(
            "SELECT gross_turnover FROM daily_stats WHERE date = ?", (today,)
        )
        row = await cur.fetchone()
        if row is None:
            await self._conn.execute(
                "INSERT INTO daily_stats (date, gross_turnover, net_pnl) VALUES (?, ?, '0')",
                (today, str(amount)),
            )
        else:
            new_val = Decimal(row["gross_turnover"]) + amount
            await self._conn.execute(
                "UPDATE daily_stats SET gross_turnover = ? WHERE date = ?",
                (str(new_val), today),
            )
        await self._conn.commit()

    async def add_pnl(self, amount: Decimal) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        cur = await self._conn.execute(
            "SELECT net_pnl FROM daily_stats WHERE date = ?", (today,)
        )
        row = await cur.fetchone()
        if row is None:
            await self._conn.execute(
                "INSERT INTO daily_stats (date, gross_turnover, net_pnl) VALUES (?, '0', ?)",
                (today, str(amount)),
            )
        else:
            new_val = Decimal(row["net_pnl"]) + amount
            await self._conn.execute(
                "UPDATE daily_stats SET net_pnl = ? WHERE date = ?",
                (str(new_val), today),
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

    async def log_trade(self, *, market_id: int, payload: dict) -> bool:
        """SSE trade event 入 trades 表。INSERT OR IGNORE 防重连重复。

        payload 形如 {"trade": {id, type, outcome_id, username, shares, price,
        gross, fee, post_market_price, market_prices_post, timestamp}}

        返回 True = 新插入（trade_id 第一次见）；False = 已存在（重复，
        例如 backend snapshot+event 双推、或 SSE 重连后 server 重新推一笔）。
        调用方应该用这个返回值决定是否 dispatch 给策略，避免同笔交易让策略
        EMA / 累计统计 等内部状态多更新一次。
        """
        import json as _json
        t = payload["trade"]
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO trades "
            "(trade_id, ts, ingest_ts, market_id, outcome_id, side, shares, "
            " price, gross, fee, username, post_market_price, market_prices_post_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(t["id"]),
                str(t["timestamp"]),
                _utcnow_iso(),
                market_id,
                int(t["outcome_id"]),
                str(t["type"]),
                str(t["shares"]),
                str(t["price"]),
                str(t["gross"]),
                str(t["fee"]),
                t.get("username"),
                str(t["post_market_price"]),
                _json.dumps(t["market_prices_post"]),
            ),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def bulk_insert_partial_trades(self, items: list[dict]) -> int:
        """批量 INSERT OR IGNORE recent-trades 响应，返回实际插入条数。

        item 字段：id, outcome_id, type, shares, price, username, timestamp,
        market_id, market_title, outcome_label
        """
        if not items:
            return 0
        cur = await self._conn.executemany(
            "INSERT OR IGNORE INTO partial_trades "
            "(trade_id, ts, market_id, outcome_id, side, shares, price, "
            " username, market_title, outcome_label) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (int(i["id"]), str(i["timestamp"]), int(i["market_id"]),
                 int(i["outcome_id"]), str(i["type"]), str(i["shares"]),
                 str(i["price"]), i.get("username"),
                 i.get("market_title"), i.get("outcome_label"))
                for i in items
            ],
        )
        await self._conn.commit()
        return cur.rowcount

    async def recent_trades_observed(
        self, *, market_id: int | None = None, limit: int = 50
    ) -> list[dict]:
        if market_id is not None:
            cur = await self._conn.execute(
                "SELECT * FROM trades WHERE market_id = ? ORDER BY id DESC LIMIT ?",
                (market_id, limit),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in await cur.fetchall()]
