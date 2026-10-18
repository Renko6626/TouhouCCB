"""A synchronous clock, sequential trades, and optional scheduled injections."""

import csv
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from random import Random

from .amm import Order, Pool, ZERO, nonnegative
from .bots import Bot, MarketView, PricePoint
from .chart import write_price_chart


@dataclass(frozen=True)
class Injection:
    at_seconds: int
    gold: Decimal = ZERO
    foreign: Decimal = ZERO

    def __post_init__(self) -> None:
        nonnegative(self.gold)
        nonnegative(self.foreign)
        if self.at_seconds < 0 or (self.gold == ZERO and self.foreign == ZERO):
            raise ValueError("injection needs a nonnegative time and positive amount")


@dataclass(frozen=True)
class Record:
    time_seconds: int
    kind: str
    actor: str
    side: str
    input_amount: Decimal
    output_amount: Decimal
    fee_amount: Decimal
    gold_added: Decimal
    foreign_added: Decimal
    price_before: Decimal
    price_after: Decimal
    gold_volume: Decimal
    reason: str = ""


class Simulation:
    def __init__(self, pool: Pool, bots: list[Bot] | None = None, *, seed: int = 1,
                 history_limit: int | None = None):
        if history_limit is not None and history_limit <= 0:
            raise ValueError("history_limit must be positive")
        self.pool = pool
        self.bots = list(bots or [])
        self.rng = Random(seed)
        self.prices: list[PricePoint] = []
        self.records: list[Record] = []
        self.history_limit = history_limit

    def inject(self, time_seconds: int, *, gold: Decimal = ZERO,
               foreign: Decimal = ZERO) -> None:
        """External experiment hook; all recorded injections go through here."""
        if time_seconds < 0:
            raise ValueError("time_seconds must be nonnegative")
        before = self.pool.price
        self.pool.inject(gold=gold, foreign=foreign)
        self.records.append(Record(time_seconds, "injection", "central_bank", "",
                                   ZERO, ZERO, ZERO, gold, foreign,
                                   before, self.pool.price, ZERO))

    def run(self, duration_seconds: int = 21600, step_seconds: int = 60,
            injections: tuple[Injection, ...] = ()) -> None:
        if duration_seconds < 0 or step_seconds <= 0:
            raise ValueError("duration must be nonnegative and step must be positive")
        if self.prices:
            raise ValueError("create a fresh Simulation for each experiment")
        if duration_seconds % step_seconds:
            raise ValueError("duration must be a multiple of step_seconds")
        if any(e.at_seconds > duration_seconds or e.at_seconds % step_seconds
               for e in injections):
            raise ValueError("injections must fall on clock steps within the run")
        events = iter(sorted(injections, key=lambda e: e.at_seconds))
        event = next(events, None)
        past = deque(maxlen=self.history_limit)
        for tick, now in enumerate(range(0, duration_seconds + 1, step_seconds)):
            while event is not None and event.at_seconds == now:
                self.inject(now, gold=event.gold, foreign=event.foreign)
                event = next(events, None)
            history = tuple(past)
            mean_log_cache = {}
            active = list(self.bots)
            self.rng.shuffle(active)
            for bot in active:
                account = bot.account
                view = MarketView(now, self.pool.price, history,
                                  account.gold, account.foreign, tick=tick,
                                  _mean_log_cache=mean_log_cache)
                decision = bot.decide(view, self.rng)
                orders = [] if decision is None else (
                    [decision] if isinstance(decision, Order) else decision)
                for order in orders:
                    before = self.pool.price
                    try:
                        quote = self.pool.trade(account, order)
                    except ValueError as exc:
                        self.records.append(Record(now, "rejected", account.name,
                            order.side, order.amount, ZERO, ZERO, ZERO, ZERO,
                            before, before, ZERO, str(exc)))
                        continue
                    side = "buy" if order.side == "buy_exact_out" else order.side
                    volume = quote.input_amount if side == "buy" else quote.output_amount
                    self.records.append(Record(now, "trade", account.name, side,
                        quote.input_amount, quote.output_amount, quote.fee_amount,
                        ZERO, ZERO, before, self.pool.price, volume))
            point = PricePoint(now, self.pool.price)
            self.prices.append(point)
            past.append(point)

    def write_csv(self, directory: str | Path) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for name, rows, fields in (
            ("prices.csv", self.prices, tuple(PricePoint.__dataclass_fields__)),
            ("events.csv", self.records, tuple(Record.__dataclass_fields__)),
        ):
            with (directory / name).open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(fields)
                for row in rows:
                    writer.writerow([getattr(row, field) for field in fields])

    def write_chart(self, directory: str | Path) -> Path:
        return write_price_chart(self.prices, Path(directory) / "price.svg")
