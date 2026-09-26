"""离线比较长期、仅加快看盘与活动短线；复用真实模板/唤醒/引擎护栏/LMSR。

不访问数据库、不创建线上账户；交易用内存账本。没有模拟真人、网络延迟、
借贷与结算，因此结果是参数验证，不是现场行情或性能保证。
在 backend 运行：python -m scripts.simulate_pve_event --output /tmp/pve-event.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.services import lmsr
from app.services.pve import attention
from app.services.pve.client import PveTradeError
from app.services.pve.engine import PveEngine, Runtime
from app.services.pve.profiles import EVENT_MIX
from app.services.pve.service import spawn_params
from app.services.pve.templates import BotState, MarketBrief, MarketView, OutcomeView, TradeBrief, TEMPLATE_REGISTRY


def minute_volatility(prices: list[float]) -> dict:
    """固定一分钟采样：价格变化的百分点标准差、对数收益的百分比标准差。"""
    if len(prices) < 3:
        return {"change_std_pp": 0, "log_return_std_pct": 0}
    changes = [(b - a) * 100 for a, b in zip(prices, prices[1:])]
    returns = [math.log(b / a) * 100 for a, b in zip(prices, prices[1:])]
    return {"change_std_pp": statistics.pstdev(changes),
            "log_return_std_pct": statistics.pstdev(returns)}


class MemoryTrader:
    """替换回环 HTTP 的内存成交账本；所有定价仍调用生产 LMSR。"""

    def __init__(self, markets: dict[int, MarketBrief]):
        self.markets = markets
        self.shares = {mid: [0.0] * len(m.outcome_ids) for mid, m in markets.items()}
        self.loc = {oid: (mid, idx) for mid, m in markets.items() for idx, oid in enumerate(m.outcome_ids)}
        self.accounts: dict[int, BotState] = {}
        self.trades: deque[TradeBrief] = deque(maxlen=600)
        self.impacts = {mid: [] for mid in markets}
        self.fees = Decimal("0")
        self.fee_rate = Decimal("0.005")
        self.now = datetime(2026, 9, 28, 1, tzinfo=timezone.utc)

    async def quote(self, user_id, outcome_id, shares, side):
        mid, idx = self.loc[outcome_id]
        bot = self.accounts[user_id]
        if side == "sell" and shares > bot.holding(outcome_id):
            raise PveTradeError(422, "模拟持仓不足")
        before = self.shares[mid]
        after = before.copy()
        after[idx] += float(shares) * (1 if side == "buy" else -1)
        delta = lmsr.calculate_lmsr_cost(after, self.markets[mid].liquidity_b) - lmsr.calculate_lmsr_cost(before, self.markets[mid].liquidity_b)
        gross = lmsr.quantize_cost(abs(delta))
        fee = lmsr.quantize_cost(gross * self.fee_rate) if side == "sell" else Decimal("0")
        return {"gross": float(gross), "net": float(gross - fee),
                "avg_price": float(lmsr.quantize_price(gross / shares)), "fee": fee}

    async def buy(self, user_id, outcome_id, shares, max_slippage_bps):
        return await self._trade(user_id, outcome_id, shares, "buy")

    async def sell(self, user_id, outcome_id, shares, max_slippage_bps):
        return await self._trade(user_id, outcome_id, shares, "sell")

    async def _trade(self, user_id, outcome_id, shares, side):
        quote = await self.quote(user_id, outcome_id, shares, side)
        bot = self.accounts[user_id]
        amount, basis = bot.holdings.get(outcome_id, (Decimal("0"), Decimal("0")))
        mid, idx = self.loc[outcome_id]
        before_prices = lmsr.calculate_lmsr_with_prices(self.shares[mid], self.markets[mid].liquidity_b)[1]
        net = Decimal(str(quote["net"]))
        if side == "buy":
            if bot.cash < net:
                raise PveTradeError(422, "模拟现金不足")
            bot.cash -= net
            bot.holdings[outcome_id] = (amount + shares, basis + net)
            self.shares[mid][idx] += float(shares)
        else:
            bot.cash += net
            bot.holdings[outcome_id] = (amount - shares, basis * (amount - shares) / amount)
            self.shares[mid][idx] -= float(shares)
            self.fees += quote["fee"]
        prices = lmsr.calculate_lmsr_with_prices(self.shares[mid], self.markets[mid].liquidity_b)[1]
        self.impacts[mid].append(max(abs(b - a) * 100 for a, b in zip(before_prices, prices)))
        self.trades.appendleft(TradeBrief(self.now, outcome_id, mid, side, float(shares),
                                        quote["avg_price"], prices, user_id=user_id, is_bot=True,
                                        pre_market_price=before_prices[idx]))
        return quote

    def view(self, now: datetime) -> MarketView:
        self.now = now
        outcomes = {}
        for mid, market in self.markets.items():
            prices = lmsr.calculate_lmsr_with_prices(self.shares[mid], market.liquidity_b)[1]
            for oid, price in zip(market.outcome_ids, prices):
                outcomes[oid] = OutcomeView(oid, mid, f"M{mid}-{oid}", price)
        cutoff = now - timedelta(minutes=60)
        return MarketView(now, outcomes, self.markets, [t for t in self.trades if t.ts >= cutoff])


async def simulate(*, mode: str, hours: float, depths: list[float], outcomes: list[int], seed: int) -> dict:
    if mode not in ("longterm", "cadence_only", "event") or hours <= 0 or len(depths) != len(outcomes):
        raise ValueError("需指定有效模式、时长和等长的市场深度/选项数")
    if not depths or any(b <= 0 for b in depths) or any(n < 2 for n in outcomes):
        raise ValueError("深度需 >0，每市场至少两个选项")
    markets = {}
    oid = 1
    for mid, (depth, count) in enumerate(zip(depths, outcomes), 1):
        markets[mid] = MarketBrief(mid, list(range(oid, oid + count)), depth)
        oid += count
    trader = MemoryTrader(markets)
    engine = PveEngine(trader=trader)
    engine._engine_rng.seed(seed)
    rng = random.Random(seed)
    start = trader.now
    pid = 0
    for template, count in EVENT_MIX.items():
        for _ in range(count):
            pid += 1
            params = spawn_params(template, rng, "event" if mode == "event" else "longterm")
            if mode == "cadence_only":
                params.update(activity_mode="event", active_preset="always", check_interval_sec=240)
            rt_rng = random.Random(pid * 7919 + 17)
            rt = Runtime(pid, pid, f"sim-{pid}", TEMPLATE_REGISTRY[template](), params,
                         [(pid - 1) % len(markets) + 1],
                         start + timedelta(seconds=rt_rng.uniform(0, min(params["check_interval_sec"], 600))),
                         rng=rt_rng)
            engine.runtimes[pid] = rt
            trader.accounts[pid] = BotState(pid, pid, rt.username, params, rt.market_scope,
                                            Decimal("500"), {}, rt.memory, rt.rng)
    cfg = {"max_wakes_per_tick": 20, "orders_per_min": 30, "single_order_cap": 0,
           "daily_cap": 0, "max_slippage_bps": 2500}
    stats = Counter()
    skips = Counter()
    trade_times = {mid: [] for mid in markets}
    material_times = {mid: [] for mid in markets}
    amounts = []
    hours_traded = Counter()
    mins = {o.outcome_id: o.price for o in trader.view(start).outcomes.values()}
    maxs = dict(mins)
    minute_samples = {oid: [] for oid in mins}
    max_15m = dict.fromkeys(markets, 0.0)
    history = deque(maxlen=181)  # 5 秒一轮，180 步 = 15 分钟
    total_steps = int(hours * 3600 / 5)
    for step in range(total_steps + 1):
        now = start + timedelta(seconds=step * 5)
        engine.activity = attention.activity_step(engine.activity, 0.7, engine._engine_rng)
        view = trader.view(now)
        wakees = engine._collect_wakees(view, cfg, now)
        for rt in wakees:
            bot = trader.accounts[rt.user_id]
            stats["wakes"] += 1
            outcome = await engine._act(rt, view, cfg, bot.cash, bot.holdings, now)
            stats[outcome] += 1
            if outcome == "trade":
                trade = trader.trades[0]
                amount = trade.shares * trade.price
                amounts.append(amount)
                mid = trade.market_id
                trade_times[mid].append(step * 5)
                if amount >= 20:
                    material_times[mid].append(step * 5)
                hours_traded[min(int(step * 5 / 3600), int(hours - 1e-9))] += 1
            elif outcome == "skip":
                skips[rt.log[-1]["msg"].split("（")[0].split("，")[0]] += 1
        # 每笔成交后采样，避免同一 tick 内相反成交把振幅掩掉。
        for trade in list(trader.trades):
            if trade.ts != now:
                break
            for oid, price in zip(markets[trade.market_id].outcome_ids, trade.market_prices_post):
                mins[oid], maxs[oid] = min(mins[oid], price), max(maxs[oid], price)
        prices = {oid: ov.price for oid, ov in trader.view(now).outcomes.items()}
        if step % 12 == 0:
            for oid, price in prices.items():
                minute_samples[oid].append(price)
        history.append(prices)
        if len(history) == 181:
            for oid, price in prices.items():
                mid = trader.loc[oid][0]
                max_15m[mid] = max(max_15m[mid], abs(price - history[0][oid]) * 100)
    initial_cost = sum(lmsr.calculate_lmsr_cost([0.0] * len(m.outcome_ids), m.liquidity_b) for m in markets.values())
    final_cost = sum(lmsr.calculate_lmsr_cost(trader.shares[mid], m.liquidity_b) for mid, m in markets.items())
    matching = all(abs(sum(float(bot.holding(oid)) for bot in trader.accounts.values())
                       - trader.shares[mid][idx]) < 1e-5 for oid, (mid, idx) in trader.loc.items())
    reports = []
    for mid, market in markets.items():
        times = trade_times[mid]
        gaps = [b - a for a, b in zip(times, times[1:])]
        impacts = sorted(trader.impacts[mid])
        volatilities = [minute_volatility(minute_samples[oid]) for oid in market.outcome_ids]
        reports.append({"market_id": mid, "depth": market.liquidity_b, "outcomes": len(market.outcome_ids),
                        "trades": len(times), "material_trades_ge_20": len(material_times[mid]),
                        "mean_gap_sec": round(statistics.mean(gaps), 2) if gaps else None,
                        "median_gap_sec": statistics.median(gaps) if gaps else None,
                        "median_trade_move_pp": round(statistics.median(impacts), 4) if impacts else 0,
                        "p90_trade_move_pp": round(impacts[max(0, math.ceil(len(impacts) * 0.9) - 1)], 4) if impacts else 0,
                        "max_minute_change_std_pp": round(max(v["change_std_pp"] for v in volatilities), 4),
                        "max_minute_log_return_std_pct": round(max(v["log_return_std_pct"] for v in volatilities), 4),
                        "max_price_span_pp": round(max(maxs[oid] - mins[oid] for oid in market.outcome_ids) * 100, 3),
                        "max_15m_change_pp": round(max_15m[mid], 3)})
    return {"mode": mode, "seed": seed, "hours": hours, "bots": 50, "initial_cash": 25000,
            "wakes": stats["wakes"], "trades": stats["trade"], "errors": stats["error"],
            "hourly_trades": [hours_traded[i] for i in range(int(hours + 0.999999))],
            "median_trade_amount": round(statistics.median(amounts), 2) if amounts else 0,
            "cash_remaining": float(sum(b.cash for b in trader.accounts.values())),
            "min_cash": float(min(b.cash for b in trader.accounts.values())),
            "market_cost_increase": final_cost - initial_cost, "fees_paid": float(trader.fees),
            "holdings_match_market": matching, "markets": reports, "skip_reasons": dict(skips)}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=10)
    parser.add_argument("--depths", type=float, nargs="+", default=[10000, 13333, 15000])
    parser.add_argument("--outcomes", type=int, nargs="+", default=[2, 3, 4])
    parser.add_argument("--seed", type=int, default=20260928)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    reports = [await simulate(mode=mode, hours=args.hours, depths=args.depths, outcomes=args.outcomes,
                              seed=args.seed) for mode in ("longterm", "cadence_only", "event")]
    result = {"assumptions": "Bots only; no humans/network/loans/settlement; sell fee 0.5%; engine tick 5s; order cap 30/min.",
              "runs": reports}
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    print(encoded)


if __name__ == "__main__":
    asyncio.run(main())
