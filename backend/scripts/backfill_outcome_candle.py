"""回填 outcome_candle 表的独立 CLI。
仿 backend/scripts/backfill_market_prices_post.py 的风格。

用法：
    cd backend
    python -m scripts.backfill_outcome_candle                # 全量
    python -m scripts.backfill_outcome_candle --dry-run      # 统计不写
    python -m scripts.backfill_outcome_candle --market-id 23 # 单市场
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import List

from sqlalchemy import select, delete

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import engine, async_session_maker  # noqa: E402
from app.models.base import Market, OutcomeCandle, Outcome  # noqa: E402
from app.services.candle_writer import backfill_one_market  # noqa: E402


async def _list_markets() -> List[int]:
    async with async_session_maker() as s:
        ids = (await s.execute(select(Market.id).order_by(Market.id))).all()
        return [r[0] for r in ids]


async def _clear_candles_for_market(market_id: int) -> int:
    async with async_session_maker() as s:
        oids = (await s.execute(
            select(Outcome.id).where(Outcome.market_id == market_id)
        )).all()
        oid_list = [r[0] for r in oids]
        if not oid_list:
            return 0
        res = await s.execute(
            delete(OutcomeCandle).where(OutcomeCandle.outcome_id.in_(oid_list))
        )
        await s.commit()
        return res.rowcount or 0


async def run(market_ids: List[int], dry_run: bool) -> None:
    print(f"找到 {len(market_ids)} 个市场要回填")
    total_processed = 0
    for mid in market_ids:
        if dry_run:
            async with async_session_maker() as s:
                from app.models.base import Transaction, TransactionType
                outcomes = (await s.execute(
                    select(Outcome.id).where(Outcome.market_id == mid)
                )).all()
                oid_list = [r[0] for r in outcomes]
                if not oid_list:
                    continue
                cnt = (await s.execute(
                    select(Transaction.id).where(
                        Transaction.outcome_id.in_(oid_list),
                        Transaction.type.in_([TransactionType.BUY, TransactionType.SELL]),
                    )
                )).all()
                print(f"  market_id={mid}: {len(cnt)} 笔可回填（dry-run）")
                total_processed += len(cnt)
            continue

        cleared = await _clear_candles_for_market(mid)
        async with async_session_maker() as s:
            n = await backfill_one_market(s, mid)
            await s.commit()
        print(f"  market_id={mid}: 清空 {cleared} 旧行, 回填 {n} 笔 Transaction")
        total_processed += n

    print(f"总计：{total_processed} 笔（{'dry-run 未实写' if dry_run else '已写入'}）")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计不写")
    parser.add_argument("--market-id", type=int, help="只处理指定 market")
    args = parser.parse_args()

    if args.market_id:
        market_ids = [args.market_id]
    else:
        market_ids = await _list_markets()
    await run(market_ids, args.dry_run)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
