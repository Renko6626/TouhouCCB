"""Trader 主入口：asyncio loop + 信号 + kill switch + 策略调度。spec §2/§5.5/§11。"""
import argparse
import asyncio
import signal
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import yaml
from dotenv import dotenv_values

# 触发策略注册副作用
import thccb_quant.strategy.dca  # noqa: F401
import thccb_quant.strategy.grid  # noqa: F401
from thccb_quant.broker.dryrun import DryRunBroker
from thccb_quant.broker.live import LiveBroker
from thccb_quant.broker.risk import RiskConfig, RiskGuard
from thccb_quant.client.auth import TokenManager
from thccb_quant.client.rest import RestClient
from thccb_quant.client.sse import SseClient
from thccb_quant.client.sse_subscriber import SseSubscriber
from thccb_quant.errors import FatalAuthError
from thccb_quant.logging_setup import setup_logging, get_logger
from thccb_quant.state.store import Store
from thccb_quant.strategy.base import StrategyContext
from thccb_quant.strategy.registry import get_strategy_class

QUANT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = QUANT_ROOT / ".env"
CONFIG_PATH = QUANT_ROOT / "config.yaml"
STATE_DIR = QUANT_ROOT / "state"
LOG_DIR = QUANT_ROOT / "logs"
KILL_FILE = STATE_DIR / "KILL"


_stop_event = asyncio.Event()


def _install_signal_handlers():
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop_event.set)


async def _kill_switch_watcher(logger):
    while not _stop_event.is_set():
        if KILL_FILE.exists():
            logger.warning("kill_switch_detected")
            _stop_event.set()
            return
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass


async def _setup_strategy(strategy, ctx: StrategyContext, logger):
    try:
        await strategy.setup(ctx)
    except Exception:
        logger.exception("strategy_setup_failed", strategy=strategy.name)
        raise


async def _run_strategy_loop(strategy, ctx: StrategyContext, logger):
    """已经 setup 过，仅跑 tick 循环 + teardown。"""
    try:
        while not _stop_event.is_set():
            try:
                await strategy.tick()
            except FatalAuthError:
                logger.error("strategy_hit_fatal_auth_error_stopping_trader")
                _stop_event.set()
                raise
            except Exception as e:
                logger.exception("strategy_tick_failed", error=str(e))
            try:
                await asyncio.wait_for(
                    _stop_event.wait(), timeout=strategy.tick_interval_sec
                )
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await strategy.teardown()
        except Exception:
            logger.exception("strategy_teardown_failed")


async def _refresh_token_warner(token_mgr: TokenManager, logger):
    """Refresh token 到期前 1 天打 ERROR 提示手动重登。"""
    while not _stop_event.is_set():
        exp = token_mgr.refresh_exp_ts
        days_left = (exp - datetime.now(timezone.utc).timestamp()) / 86400
        if days_left < 1.0:
            logger.error(
                "refresh_token_expiring_soon",
                days_left=days_left,
                msg="please re-login in browser and update .env",
            )
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"{CONFIG_PATH} not found, copy from config.example.yaml"
        )
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


def _load_env() -> dict:
    if not ENV_PATH.exists():
        raise FileNotFoundError(
            f"{ENV_PATH} not found, copy from .env.example and fill tokens"
        )
    return dotenv_values(ENV_PATH)


async def main_async(dry_run: bool) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(LOG_DIR, "system")
    logger = get_logger("trader")

    if KILL_FILE.exists():
        logger.error("startup_blocked_by_kill_switch", path=str(KILL_FILE))
        return 1

    try:
        env = _load_env()
        config = _load_config()
    except FileNotFoundError as e:
        logger.error("missing_file", error=str(e))
        return 1

    try:
        import setproctitle
        setproctitle.setproctitle("thccb-quant" + (" [dryrun]" if dry_run else ""))
    except Exception:
        pass

    base_url = env["THCCB_BASE_URL"]

    raw_client = httpx.AsyncClient(base_url=base_url, timeout=10.0)
    api_client = httpx.AsyncClient(base_url=base_url, timeout=10.0)

    token_mgr = TokenManager(
        base_url=base_url,
        access_token=env["THCCB_ACCESS_TOKEN"],
        refresh_token=env["THCCB_REFRESH_TOKEN"],
        env_path=ENV_PATH,
        raw_client=raw_client,
    )

    risk = RiskGuard(
        RiskConfig(
            single_order_cap_cny=Decimal(str(config["risk"]["single_order_cap_cny"])),
            daily_loss_cap_cny=Decimal(str(config["risk"]["daily_loss_cap_cny"])),
            daily_turnover_cap_cny=Decimal(str(config["risk"]["daily_turnover_cap_cny"])),
            max_slippage_bps=int(config["risk"]["max_slippage_bps"]),
            min_seconds_between_orders=int(config["risk"]["min_seconds_between_orders"]),
        ),
        STATE_DIR,
    )

    rest = RestClient(
        client=api_client,
        token_manager=token_mgr,
        rate_limit_per_sec=float(config["client"]["rate_limit_per_sec"]),
    )

    store = await Store.open(STATE_DIR / "quant.db")

    broker = (
        DryRunBroker(rest=rest, risk=risk, store=store)
        if dry_run else
        LiveBroker(rest=rest, risk=risk, store=store)
    )

    logger.info("startup",
                base_url=base_url, dry_run=dry_run,
                strategies_count=len(config["strategies"]))

    _install_signal_handlers()

    tasks = [
        asyncio.create_task(_kill_switch_watcher(logger)),
        asyncio.create_task(_refresh_token_warner(token_mgr, logger)),
    ]

    # 实例化所有 enabled 策略
    enabled_pairs = []  # [(strategy, ctx, strategy_logger), ...]
    for s_cfg in config["strategies"]:
        if not s_cfg.get("enabled", False):
            continue
        cls = get_strategy_class(s_cfg["type"])
        strat = cls(name=s_cfg["name"], config=s_cfg)
        s_logger = get_logger(s_cfg["name"], strategy=s_cfg["name"])
        ctx = StrategyContext(
            rest=rest, broker=broker, store=store, logger=s_logger,
            config={**config["risk"], **s_cfg},
        )
        enabled_pairs.append((strat, ctx, s_logger))

    # 先 setup 所有策略（让 market_id 落定）
    for strat, ctx, s_logger in enabled_pairs:
        await _setup_strategy(strat, ctx, s_logger)

    # 计算 SSE 需要订阅的 market_ids
    market_ids = {s.market_id for s, _, _ in enabled_pairs if s.market_id is not None}
    sse_raw = None
    if market_ids:
        sse_raw = httpx.AsyncClient(base_url=base_url, timeout=None)
        sse_client = SseClient(
            base_url=base_url, token_manager=token_mgr, raw_client=sse_raw,
        )
        subscriber = SseSubscriber(
            rest=rest, store=store, sse_client=sse_client,
            strategies=[s for s, _, _ in enabled_pairs],
            market_ids=market_ids,
            logger=get_logger("sse"),
        )
        tasks.append(asyncio.create_task(subscriber.run()))

    # 起策略 tick 循环
    for strat, ctx, s_logger in enabled_pairs:
        tasks.append(asyncio.create_task(_run_strategy_loop(strat, ctx, s_logger)))

    await _stop_event.wait()
    logger.info("shutting_down")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    await store.close()
    await api_client.aclose()
    await raw_client.aclose()
    if sse_raw is not None:
        await sse_raw.aclose()
    logger.info("stopped_clean")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="thccb-quant")
    parser.add_argument("--dry-run", action="store_true",
                        help="走通所有路径但不真下单（DryRunBroker）")
    args = parser.parse_args()
    return asyncio.run(main_async(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
