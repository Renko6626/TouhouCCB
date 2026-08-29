"""PvE 调度核心（spec §6）。

每轮 tick（scheduler.py 定频调，max_instances=1 保证不重入）：
  开关检查 → 同步账户池 → 建 MarketView → 收集唤醒批次（到点 + 行情推送）
  → 逐个 decide + 护栏 + 串行回环下单 → 死亡判定 → 重排下次唤醒。

所有个体状态（下次行动时间、模板记忆、决策环形日志、当日成交额）都在内存，
重启即弃（spec §2 非目标）；持仓/资金永远从 DB 现读，不做内部账本。
"""
from __future__ import annotations

import logging
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy import select, update

from app.core.database import async_session_maker
from app.models.base import Position, User
from app.models.bot import BotProfile, BOT_STATUS_ACTIVE, BOT_STATUS_DEAD
from app.services import site_config
from app.services.pve import attention
from app.services.pve.client import LoopbackTrader, PveTradeError
from app.services.pve.market_view import build_market_view
from app.services.pve.templates import (
    TEMPLATE_REGISTRY,
    Action,
    BotState,
    BotTemplate,
    MarketView,
)
from app.services.wealth import compute_users_holdings_value
from app.services.pve.attention import TZ as _BJ_TZ

logger = logging.getLogger("thccb.pve")

_ALERT_WINDOW_MIN = 10  # 行情推送观察窗（引擎级统一算一次，个体只差阈值/概率/冷却）
_LOG_DEPTH = 60


@dataclass
class Runtime:
    profile_id: int
    user_id: int
    username: str
    template: BotTemplate
    params: dict
    market_scope: Optional[List[int]]
    next_action_at: datetime
    memory: dict = field(default_factory=dict)
    rng: random.Random = field(default_factory=random.Random)
    alert_cooldown_until: Optional[datetime] = None
    day_key: str = ""
    day_turnover: float = 0.0
    log: deque = field(default_factory=lambda: deque(maxlen=_LOG_DEPTH))


class PveEngine:
    def __init__(self, trader: Optional[LoopbackTrader] = None):
        self.trader = trader or LoopbackTrader()
        self.runtimes: Dict[int, Runtime] = {}
        # 环形日志与 Runtime 分开持有：机器人被暂停/死亡移出调度后日志仍可查
        self._logs: Dict[int, deque] = {}
        self._order_ts: deque = deque(maxlen=512)  # 全局限速滑窗
        self._ticking = False
        self.last_tick_result: dict = {}
        # 全局活跃度（潮汐）：每 tick OU 演化，作用于全体 next_wake 的 pace；
        # 加上放行数随机化，避免「每 N 秒冲进来一批」的固定节律。重启归位 1。
        self.activity: float = 1.0
        self._engine_rng = random.Random()

    # ── 对外观测（admin API 用）────────────────────────────────────────

    def get_log(self, profile_id: int) -> List[dict]:
        return list(reversed(self._logs.get(profile_id, ())))

    def snapshot(self) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "scheduled_bots": len(self.runtimes),
            "orders_last_min": self._orders_in_window(now, 60),
            "activity": round(self.activity, 3),
            "last_tick": self.last_tick_result,
        }

    # ── 主循环 ────────────────────────────────────────────────────────

    async def tick(self) -> dict:
        if self._ticking:  # 双保险；APScheduler max_instances=1 已挡一层
            return {"skipped": "reentry"}
        self._ticking = True
        try:
            result = await self._tick_inner()
            self.last_tick_result = {"at": datetime.now(timezone.utc).isoformat(), **result}
            return result
        finally:
            self._ticking = False

    async def _tick_inner(self) -> dict:
        now = datetime.now(timezone.utc)
        async with async_session_maker() as db:
            if not await site_config.get_bool_or(db, "pve_enabled", False):
                return {"enabled": False}
            cfg = await self._load_cfg(db)
            self.activity = attention.activity_step(
                self.activity, cfg["activity_wave"], self._engine_rng
            )
            await self._sync_runtimes(db, now)
            if not self.runtimes:
                return {"enabled": True, "bots": 0}
            view = await build_market_view(db)
            wakees = self._collect_wakees(view, cfg, now)
            if not wakees:
                return {"enabled": True, "bots": len(self.runtimes), "woke": 0}

            uids = [rt.user_id for rt in wakees]
            cash_map: Dict[int, Decimal] = {
                u.id: u.cash
                for u in (await db.execute(select(User).where(User.id.in_(uids)))).scalars()
            }
            holdings_map: Dict[int, Dict[int, tuple]] = {}
            pos_rows = (
                await db.execute(
                    select(Position).where(Position.user_id.in_(uids), Position.amount > 0)
                )
            ).scalars()
            for p in pos_rows:
                holdings_map.setdefault(p.user_id, {})[p.outcome_id] = (p.amount, p.cost_basis)
            lcv = await compute_users_holdings_value(db, uids)

            # 死亡判定：总资产（cash + LCV 清算口径）低于水位 → dead，移出调度
            dead = 0
            for rt in wakees[:]:
                total = cash_map.get(rt.user_id, Decimal("0")) + lcv.get(rt.user_id, Decimal("0"))
                if total < cfg["death_floor"]:
                    await db.execute(
                        update(BotProfile)
                        .where(BotProfile.id == rt.profile_id)
                        .values(status=BOT_STATUS_DEAD)
                    )
                    self._log(rt, "lifecycle", f"死亡：总资产 {total:.2f} < 水位 {cfg['death_floor']}")
                    wakees.remove(rt)
                    self.runtimes.pop(rt.profile_id, None)
                    dead += 1
            if dead:
                await db.commit()

        # session 已关；串行执行交易（回环请求走服务端自己的 session）
        stats = {"trade": 0, "skip": 0, "error": 0}
        traded_pids: List[int] = []
        for rt in wakees:
            outcome = await self._act(
                rt, view, cfg,
                cash_map.get(rt.user_id, Decimal("0")),
                holdings_map.get(rt.user_id, {}),
                now,
            )
            stats[outcome] += 1
            if outcome == "trade":
                traded_pids.append(rt.profile_id)

        if traded_pids:
            async with async_session_maker() as db:
                await db.execute(
                    update(BotProfile)
                    .where(BotProfile.id.in_(traded_pids))
                    .values(last_trade_at=now)
                )
                await db.commit()

        return {
            "enabled": True,
            "bots": len(self.runtimes),
            "woke": len(wakees),
            "dead": dead,
            **stats,
        }

    # ── 配置与账户池同步 ─────────────────────────────────────────────

    async def _load_cfg(self, db) -> dict:
        return {
            "orders_per_min": await site_config.get_int_or(db, "pve_orders_per_min_cap", 30),
            "single_order_cap": float(
                await site_config.get_decimal_or(db, "pve_single_order_cap_cny", Decimal("200"))
            ),
            "daily_cap": float(
                await site_config.get_decimal_or(db, "pve_daily_turnover_cap_cny", Decimal("2000"))
            ),
            "max_slippage_bps": await site_config.get_int_or(db, "pve_max_slippage_bps", 800),
            "death_floor": await site_config.get_decimal_or(
                db, "pve_death_floor_cny", Decimal("3")
            ),
            "max_wakes_per_tick": await site_config.get_int_or(db, "pve_max_wakes_per_tick", 20),
            "activity_wave": float(
                await site_config.get_decimal_or(db, "pve_activity_wave", Decimal("0.7"))
            ),
        }

    def _merge_params(self, profile: BotProfile) -> dict:
        merged = dict(attention.ATTENTION_DEFAULTS)
        merged.update(TEMPLATE_REGISTRY[profile.template].default_params)
        merged.update(profile.params or {})
        return merged

    async def _sync_runtimes(self, db, now: datetime) -> None:
        rows = (
            await db.execute(
                select(BotProfile, User.username)
                .join(User, User.id == BotProfile.user_id)
                .where(BotProfile.status == BOT_STATUS_ACTIVE)
            )
        ).all()
        seen = set()
        for profile, username in rows:
            if profile.template not in TEMPLATE_REGISTRY:
                logger.warning("pve_unknown_template profile=%s template=%s", profile.id, profile.template)
                continue
            seen.add(profile.id)
            rt = self.runtimes.get(profile.id)
            merged = self._merge_params(profile)
            if rt is None:
                rng = random.Random(profile.id * 7919 + 17)  # 稳定个体种子=稳定人格
                first_delay = rng.uniform(0, min(float(merged["check_interval_sec"]), 600))
                rt = Runtime(
                    profile_id=profile.id,
                    user_id=profile.user_id,
                    username=username,
                    template=TEMPLATE_REGISTRY[profile.template](),
                    params=merged,
                    market_scope=profile.market_scope,
                    next_action_at=now + timedelta(seconds=first_delay),
                    rng=rng,
                    log=self._logs.setdefault(profile.id, deque(maxlen=_LOG_DEPTH)),
                )
                self.runtimes[profile.id] = rt
                self._log(rt, "lifecycle", f"进入调度（{profile.template}）")
            else:
                # 管理员在线改参/改范围/换模板即时生效；模板记忆保留
                rt.params = merged
                rt.market_scope = profile.market_scope
                rt.username = username
                if rt.template.name != profile.template:
                    rt.template = TEMPLATE_REGISTRY[profile.template]()
                    rt.memory.clear()
                    self._log(rt, "lifecycle", f"切换模板 → {profile.template}")
        for pid in list(self.runtimes):
            if pid not in seen:
                self._log(self.runtimes[pid], "lifecycle", "移出调度（暂停/死亡/删除）")
                del self.runtimes[pid]

    # ── 唤醒批次 ─────────────────────────────────────────────────────

    def _collect_wakees(self, view: MarketView, cfg: dict, now: datetime) -> List[Runtime]:
        # 行情推送：每 outcome 的 10min 涨跌幅引擎级算一次，个体只差阈值/概率/冷却
        changes = {oid: abs(view.window_change(oid, _ALERT_WINDOW_MIN)) for oid in view.outcomes}
        for rt in self.runtimes.values():
            if rt.next_action_at <= now:
                continue
            eligible = [
                oid
                for oid, ov in view.outcomes.items()
                if rt.market_scope is None or ov.market_id in rt.market_scope
            ]
            max_change = max((changes[o] for o in eligible), default=0.0)
            verdict = attention.should_alert(
                rt.params, max_change, now, rt.alert_cooldown_until, rt.rng
            )
            if verdict == "none":
                continue
            rt.alert_cooldown_until = now + timedelta(
                seconds=float(rt.params.get("alert_cooldown_sec", 1800))
            )
            if verdict == "wake":
                delay = attention.alert_delay(rt.params, rt.rng)
                rt.next_action_at = min(rt.next_action_at, now + delay)
                self._log(rt, "alert", f"行情推送 Δ{max_change:.3f}，{delay.seconds // 60} 分钟后来看盘")
            else:
                self._log(rt, "alert", f"行情推送 Δ{max_change:.3f}，没理会")
        due = sorted(
            (rt for rt in self.runtimes.values() if rt.next_action_at <= now),
            key=lambda rt: rt.next_action_at,
        )
        # 放行数随机化：积压时若每 tick 精确放行 cap 个，成交会呈固定节律脉冲
        cap = cfg["max_wakes_per_tick"]
        return due[: self._engine_rng.randint((cap + 1) // 2, cap)]

    # ── 单机器人：决策 + 护栏 + 执行 ─────────────────────────────────

    async def _act(
        self,
        rt: Runtime,
        view: MarketView,
        cfg: dict,
        cash: Decimal,
        holdings: Dict[int, tuple],
        now: datetime,
    ) -> str:
        rt.next_action_at = attention.next_wake(now, rt.params, rt.rng, pace=self.activity)
        bot = BotState(
            user_id=rt.user_id,
            profile_id=rt.profile_id,
            username=rt.username,
            params=rt.params,
            market_scope=rt.market_scope,
            cash=cash,
            holdings=holdings,
            memory=rt.memory,
            rng=rt.rng,
        )
        try:
            action = rt.template.decide(bot, view)
        except Exception as e:
            logger.exception("pve_decide_failed profile=%s", rt.profile_id)
            self._log(rt, "error", f"decide 异常：{e!r}")
            return "error"
        if action is None:
            self._log(rt, "skip", "看了盘，无操作")
            return "skip"
        return await self._execute(rt, action, view, cfg, cash, now)

    def _orders_in_window(self, now: datetime, seconds: int) -> int:
        cutoff = now - timedelta(seconds=seconds)
        return sum(1 for t in self._order_ts if t >= cutoff)

    async def _execute(
        self, rt: Runtime, action: Action, view: MarketView, cfg: dict, cash: Decimal, now: datetime
    ) -> str:
        if action.shares <= 0 or action.outcome_id not in view.outcomes:
            self._log(rt, "skip", f"无效动作被拦：{action}")
            return "skip"
        if self._orders_in_window(now, 60) >= cfg["orders_per_min"]:
            self._log(rt, "skip", f"全局每分钟下单上限（{cfg['orders_per_min']}）已满，放弃")
            return "skip"
        day = datetime.now(_BJ_TZ).strftime("%Y-%m-%d")
        if rt.day_key != day:
            rt.day_key, rt.day_turnover = day, 0.0

        try:
            q = await self.trader.quote(rt.user_id, action.outcome_id, action.shares, action.side)
        except PveTradeError as e:
            self._log(rt, "error", f"报价被拒：{e.detail}")
            return "error"
        except Exception as e:
            self._log(rt, "error", f"报价失败：{e!r}")
            return "error"

        gross, net, avg = float(q["gross"]), float(q["net"]), float(q["avg_price"])
        cur = view.outcomes[action.outcome_id].price
        slip_bps = abs(avg - cur) / max(cur, 1e-6) * 10000
        if slip_bps > cfg["max_slippage_bps"]:
            self._log(rt, "skip", f"滑点 {slip_bps:.0f}bps 超限（{cfg['max_slippage_bps']}），放弃：{action.reason}")
            return "skip"
        if gross > cfg["single_order_cap"]:
            self._log(rt, "skip", f"单笔 {gross:.1f} 超上限 {cfg['single_order_cap']}，放弃")
            return "skip"
        if rt.day_turnover + gross > cfg["daily_cap"]:
            self._log(rt, "skip", f"当日成交额将超上限 {cfg['daily_cap']}，放弃")
            return "skip"
        if action.side == "buy" and Decimal(str(net)) > cash:
            self._log(rt, "skip", f"现金不足（需 {net:.2f}，有 {cash}），放弃")
            return "skip"

        try:
            if action.side == "buy":
                await self.trader.buy(
                    rt.user_id, action.outcome_id, action.shares, cfg["max_slippage_bps"]
                )
            else:
                await self.trader.sell(
                    rt.user_id, action.outcome_id, action.shares, cfg["max_slippage_bps"]
                )
        except PveTradeError as e:
            self._log(rt, "error", f"下单被拒：{e.detail}（{action.side} {action.shares}）")
            return "error"
        except Exception as e:
            logger.exception("pve_order_failed profile=%s", rt.profile_id)
            self._log(rt, "error", f"下单失败：{e!r}")
            return "error"

        self._order_ts.append(now)
        rt.day_turnover += gross
        self._log(
            rt, "trade",
            f"{action.side} {action.shares} 份 outcome#{action.outcome_id} "
            f"≈{gross:.2f}（均价 {avg:.4f}）：{action.reason}",
        )
        return "trade"

    # ── 日志 ─────────────────────────────────────────────────────────

    def _log(self, rt: Runtime, event: str, msg: str) -> None:
        rt.log.append(
            {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, "msg": msg}
        )


ENGINE = PveEngine()
