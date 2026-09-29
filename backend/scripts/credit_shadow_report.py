"""统一信贷影子对账（**只读**）：旧 LCV vs 新组聚合 / MTM / E / 维持线。

用法（在 backend/ 下，DATABASE_URL 指向隔离数据集；生产请用只读副本）：

    python scripts/credit_shadow_report.py --limit 100 --out shadow-before.md
    python scripts/credit_shadow_report.py --user-id 1,2,3 --format json
    python scripts/credit_shadow_report.py --daily-rate 0.001 --out -

输出列：旧 ``compute_users_holdings_value``（逐持仓独立求和，LMSR-only）、
新 ``value_users_batch`` 的 LMSR / FX 组聚合与合计、差值、占比、两套 MTM、
``display_equity``、``D_effective``、``liquidation_equity`` 与是否越维持线。

脚本只发 SELECT：PostgreSQL 上先 ``SET TRANSACTION READ ONLY``；不 commit、
不写审计、不推进 ``debt_last_accrued_at``。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select, text  # noqa: E402

from app.core.database import async_session_maker, engine  # noqa: E402
from app.models.base import Position, User  # noqa: E402
from app.models.fx import FxWallet  # noqa: E402
from app.services import site_config  # noqa: E402
from app.services.credit import flags as credit_flags  # noqa: E402
from app.services.credit.thresholds import derive_thresholds  # noqa: E402
from app.services.credit.valuation import value_users_batch  # noqa: E402
from app.services.wealth import compute_users_holdings_value  # noqa: E402

ZERO = Decimal("0")
Q4 = Decimal("0.0001")
CONFIG_KEYS = [
    credit_flags.KEY_CREDIT_LEVERAGE,
    credit_flags.KEY_CREDIT_MAINTENANCE_RATIO,
    "loan_daily_rate",
    "sell_fee_rate",
    "loan_leverage_k",
    "liquidation_hard_threshold",
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="统一信贷影子对账（只读）")
    parser.add_argument("--user-id", default=None,
                        help="逗号/空格分隔的 user_id；默认自动选有债/持仓/钱包的用户")
    parser.add_argument("--limit", type=int, default=100, help="自动选样上限（默认 100）")
    parser.add_argument("--daily-rate", default=None, help="覆盖 loan_daily_rate 的日利率")
    parser.add_argument("--out", default="-", help="输出文件（默认 - 即 stdout）")
    parser.add_argument("--format", choices=("md", "json"), default="md")
    return parser


def _parse_user_ids(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    items = [part for part in raw.replace(",", " ").split() if part]
    try:
        return [int(item) for item in items]
    except ValueError as exc:
        raise SystemExit(f"--user-id 必须是整数列表: {raw!r}") from exc


def _dec(raw: str | None) -> Decimal | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _resolve_thresholds(raw: dict[str, str]) -> tuple[Decimal, Decimal, Decimal] | None:
    leverage = _dec(raw.get(credit_flags.KEY_CREDIT_LEVERAGE))
    if leverage is None:
        legacy_k = _dec(raw.get("loan_leverage_k"))
        leverage = legacy_k + Decimal(1) if legacy_k is not None else None
    maintenance = _dec(raw.get(credit_flags.KEY_CREDIT_MAINTENANCE_RATIO))
    if maintenance is None:
        maintenance = _dec(raw.get("liquidation_hard_threshold"))
    if leverage is None or maintenance is None:
        return None
    try:
        thresholds = derive_thresholds(leverage, maintenance)
    except ValueError:
        return None
    return thresholds.leverage, thresholds.r_initial, thresholds.r_maintenance


async def _select_user_ids(session, limit: int) -> list[int]:
    ids: set[int] = set()
    ids.update((await session.execute(
        select(User.id).where(User.debt > ZERO)
    )).scalars().all())
    ids.update((await session.execute(
        select(Position.user_id).where(Position.amount > ZERO).distinct()
    )).scalars().all())
    ids.update((await session.execute(
        select(FxWallet.user_id).where(FxWallet.foreign_amount > ZERO).distinct()
    )).scalars().all())
    ordered = sorted(int(uid) for uid in ids)
    return ordered[:limit] if limit > 0 else ordered


async def _collect(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return await _collect_in_session(args)
    finally:
        # aiosqlite 连接线程非 daemon：不 dispose 会在解释器退出时挂住。
        await engine.dispose()


async def _collect_in_session(args: argparse.Namespace) -> dict[str, Any]:
    async with async_session_maker() as session:
        bind = session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            await session.execute(text("SET TRANSACTION READ ONLY"))

        raw_config = await site_config.get_many(session, list(CONFIG_KEYS))
        thresholds = _resolve_thresholds(raw_config)

        user_ids = _parse_user_ids(args.user_id)
        if user_ids is None:
            user_ids = await _select_user_ids(session, args.limit)

        daily_rate = _dec(args.daily_rate)
        if daily_rate is None:
            daily_rate = _dec(raw_config.get("loan_daily_rate")) or ZERO

        legacy = await compute_users_holdings_value(session, user_ids=user_ids)
        valuations = await value_users_batch(
            session, user_ids, daily_rate=daily_rate,
        )

        rows: list[dict[str, Any]] = []
        blocked_histogram: dict[str, int] = {}
        for uid in user_ids:
            valuation = valuations.get(uid)
            if valuation is None:
                continue
            lmsr_value = sum(
                (g.value for g in valuation.groups if g.key.product == "lmsr"), ZERO,
            )
            fx_value = sum(
                (g.value for g in valuation.groups if g.key.product == "fx"), ZERO,
            )
            for group in valuation.groups:
                if group.blocked_reason:
                    key = f"{group.key.product}:{group.blocked_reason}"
                    blocked_histogram[key] = blocked_histogram.get(key, 0) + 1
            old_lcv = legacy.get(uid, ZERO)
            new_total = lmsr_value + fx_value
            diff = new_total - old_lcv
            ratio = (new_total / old_lcv).quantize(Q4) if old_lcv > ZERO else None
            breach = bool(
                thresholds is not None
                and valuation.debt_effective > ZERO
                and valuation.liquidation_equity
                < thresholds[2] * valuation.debt_effective
            )
            rows.append({
                "user_id": uid,
                "old_lcv": old_lcv,
                "new_lmsr": lmsr_value,
                "new_fx": fx_value,
                "new_total": new_total,
                "diff": diff,
                "ratio": ratio,
                "mtm_lmsr": valuation.mtm_lmsr,
                "mtm_fx": valuation.mtm_fx,
                "display_equity": valuation.display_equity,
                "debt_effective": valuation.debt_effective,
                "liquidation_equity": valuation.liquidation_equity,
                "maintenance_breach": breach,
                "blocked_groups": [
                    {"product": g.key.product, "group_id": g.key.group_id,
                     "reason": g.blocked_reason}
                    for g in valuation.groups if g.blocked_reason
                ],
            })

        total_old = sum((row["old_lcv"] for row in rows), ZERO)
        total_new = sum((row["new_total"] for row in rows), ZERO)
        summary = {
            "users": len(rows),
            "old_lcv_total": total_old,
            "new_total": total_new,
            "diff_total": total_new - total_old,
            "users_with_diff": sum(1 for row in rows if row["diff"] != ZERO),
            "users_with_lmsr_diff": sum(
                1 for row in rows if row["new_lmsr"] != row["old_lcv"]
            ),
            "users_with_fx_value": sum(1 for row in rows if row["new_fx"] != ZERO),
            "users_above_maintenance": sum(
                1 for row in rows if row["maintenance_breach"]
            ),
            "blocked_groups": blocked_histogram,
        }
        return {
            "meta": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "database": f"{engine.url.get_backend_name()}:{engine.url.database}",
                "daily_rate": str(daily_rate),
                "leverage": str(thresholds[0]) if thresholds else None,
                "r_initial": str(thresholds[1]) if thresholds else None,
                "r_maintenance": str(thresholds[2]) if thresholds else None,
                "legacy_lcv_user_count": len(legacy),
            },
            "summary": summary,
            "rows": rows,
        }


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, Decimal):
        return f"{value:f}"
    return str(value)


def _render_md(report: dict[str, Any]) -> str:
    meta, summary, rows = report["meta"], report["summary"], report["rows"]
    lines = [
        "# 统一信贷影子对账（before）",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- 数据集：`{meta['database']}`",
        f"- 日利率：{meta['daily_rate']}",
        f"- 门槛：leverage={_fmt(meta['leverage'])} "
        f"r_initial={_fmt(meta['r_initial'])} r_maintenance={_fmt(meta['r_maintenance'])}",
        f"- 用户数：{summary['users']}",
        "",
        "## 逐用户对照",
        "",
        "| user | 旧LCV(LMSR) | 新LMSR组 | 新FX组 | 新合计 | 差值 | 占比 | MTM_lmsr | MTM_fx | display_E | D_eff | liq_E | 越维持线 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['user_id']} | {_fmt(row['old_lcv'])} | {_fmt(row['new_lmsr'])} | "
            f"{_fmt(row['new_fx'])} | {_fmt(row['new_total'])} | {_fmt(row['diff'])} | "
            f"{_fmt(row['ratio'])} | {_fmt(row['mtm_lmsr'])} | {_fmt(row['mtm_fx'])} | "
            f"{_fmt(row['display_equity'])} | {_fmt(row['debt_effective'])} | "
            f"{_fmt(row['liquidation_equity'])} | {'是' if row['maintenance_breach'] else '否'} |"
        )
    lines += [
        "",
        "## 汇总",
        "",
        f"- 旧 LCV 合计：{_fmt(summary['old_lcv_total'])}",
        f"- 新组合计：{_fmt(summary['new_total'])}（差 {_fmt(summary['diff_total'])}）",
        f"- 有差值用户：{summary['users_with_diff']} / {summary['users']}",
        f"- 越维持线用户：{summary['users_above_maintenance']}",
        f"- 阻塞组：{summary['blocked_groups'] or '无'}",
        "",
        "## 差异来源与样本观察",
        "",
        "1. **滚动 q vs 独立相加**：新实现把同一 market 全部 outcome 放在同一滚动 q 副本上"
        "按 outcome_id 升序卖出；旧 LCV 每个持仓在未变化 q 上独立全卖。同市场多腿时两者"
        "必然有差，方向取决于持仓集中在哪一侧、前序腿把 q 拉离原始分布多远"
        "（先卖的腿改变后续腿的成交价）。本样本有 "
        f"{summary['users_with_lmsr_diff']} 个用户存在该差异（`新LMSR组 ≠ 旧LCV`）。",
        "2. **FX 不在旧 LCV 内**：旧 LCV 只含 LMSR；新 `new_fx` 是各 pair 按当前储备 "
        f"`quote_sell` 的净回收（含 per-pair `sell_fee_rate`）。本样本有 "
        f"{summary['users_with_fx_value']} 个用户有 FX 回收。",
        "3. **HALT / paused 不贡献 L**：阻塞组（`lmsr:market_not_open`、"
        "`fx:pair_paused` 等）`L=0`，但其 MTM 仍保留在 `display_E`；因此 `liq_E` 可能"
        "显著低于 `display_E`（见样本中 HALT 持仓用户）。",
        "4. **费率同源**：LMSR 两边都用 `site_config.sell_fee_rate`；FX 用 per-pair "
        "`sell_fee_rate`（`buy_fee_rate` 不参与）。",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = asyncio.run(_collect(args))
    if args.format == "json":
        payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    else:
        payload = _render_md(report)
    if args.out in (None, "-"):
        print(payload)
    else:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload)
            if not payload.endswith("\n"):
                handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
