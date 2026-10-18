"""Local administrator CLI for isolated FX load tests; never exposes an HTTP route."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt
from sqlalchemy import delete, func, or_, select, text
from sqlmodel import SQLModel

from app.core.config import settings
from app.core.database import async_session_maker, engine
import app.models.redemption  # noqa: F401: register FK metadata
import app.models.title  # noqa: F401
from app.models.audit import AuditEvent
from app.models.base import BotSuspicion, LiquidationEvent, SiteConfig, User
from app.models.bot import BotProfile
from app.models.credit import LiquidationAction, LiquidationRun
from app.models.fx import (FxCandle, FxExecution, FxMarketDataState, FxPair,
                           FxPoolEvent, FxShortPosition, FxTrade, FxTreasury,
                           FxTriggerOrder, FxWallet)
from app.models.ledger import LedgerEntry
from app.services import audit_service
from app.services.credit.ownership import LOCK_KEY
from app.services.fx.pool_events import append_pool_event


def run_id(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}", value):
        raise ValueError("run ID must be 1..40 letters, digits, '_' or '-'")
    return value


def private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


async def require_stopped(db) -> None:
    # The same key as the running economic owner. Transaction scope releases it
    # on commit/rollback and prevents a second writer starting during maintenance.
    if db.bind.dialect.name == "postgresql":
        acquired = (await db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                     {"key": LOCK_KEY})).scalar_one()
        if not acquired:
            raise ValueError("economic writer is running; stop backend before prepare/cleanup")


async def prepare(db, *, tag: str, users: int, cash: Decimal, source_pair: int,
                  manifest: Path) -> dict:
    run_id(tag)
    if not 1 <= users <= 200 or not cash.is_finite() or not 0 < cash <= Decimal("100000"):
        raise ValueError("users must be 1..200 and cash in (0, 100000]")
    if manifest.exists():
        raise ValueError("manifest already exists; inspect or use a new run ID")
    await require_stopped(db)
    prefix = f"loadtest:{tag}:"
    # startswith escapes LIKE metacharacters in user-supplied run IDs.
    if (await db.execute(select(User.id).where(User.casdoor_id.startswith(prefix, autoescape=True)))).first():
        raise ValueError("this run already has accounts; refusing to reset their assets")
    if (await db.execute(select(User.id).where(User.casdoor_id.startswith("loadtest:", autoescape=True)))).first():
        raise ValueError("another load test exists; clean it before preparing a new one")
    source = await db.get(FxPair, source_pair)
    if source is None or source.archived or source.status != "trading":
        raise ValueError("source pair must be the active, unarchived trading pair")
    active = (await db.execute(select(FxPair).where(FxPair.status == "trading"))).scalars().all()
    if len(active) != 1:
        raise ValueError("expected exactly one trading FX pair")
    enabled = (await db.execute(select(SiteConfig).where(SiteConfig.key == "fx_enabled"))).scalar_one_or_none()
    if enabled is None or enabled.value.lower() not in {"true", "1", "yes"}:
        raise ValueError("FX is disabled; enable it through the existing administrator controls first")
    code = "LT" + hashlib.sha256(tag.encode()).hexdigest()[:12].upper()
    if (await db.execute(select(FxPair.id).where(FxPair.currency_code == code))).first():
        raise ValueError("test currency already exists")
    now = datetime.now(timezone.utc)
    source.status = "paused"
    pair = FxPair(currency_code=code, currency_name=f"[LT] {tag}", status="trading",
                  gold_reserve=source.gold_reserve, foreign_reserve=source.foreign_reserve,
                  initial_price=source.gold_reserve / source.foreign_reserve,
                  buy_fee_rate=source.buy_fee_rate, sell_fee_rate=source.sell_fee_rate,
                  short_lending_limit_foreign=source.short_lending_limit_foreign)
    db.add(pair)
    await db.flush()
    db.add(FxTreasury(pair_id=pair.id, gold_balance=pair.gold_reserve,
                      foreign_balance=pair.foreign_reserve))
    anchor = audit_service.record(db, "fx_fund", ref_table="fx_pair", ref_id=pair.id,
                                  payload={"action": "initial_issuance", "source": "loadtest",
                                           "gold_amount": str(pair.gold_reserve),
                                           "foreign_amount": str(pair.foreign_reserve),
                                           "pool_after": {"gold": str(pair.gold_reserve), "foreign": str(pair.foreign_reserve)},
                                           "treasury_after": {"gold": str(pair.gold_reserve), "foreign": str(pair.foreign_reserve)}})
    await db.flush()
    append_pool_event(db, pair_id=pair.id, kind="initialize", created_at=now,
                      pre_reserves=None, post_reserves=(pair.gold_reserve, pair.foreign_reserve),
                      gold_volume=Decimal("0"), audit_id=anchor.id)
    identities = []
    for i in range(users):
        name = f"lt_{code[2:10]}_{i + 1:03}"
        user = User(casdoor_id=f"{prefix}{i + 1:03}", username=name,
                    email=f"{name}@invalid.local", is_active=True, is_bot=False,
                    is_superuser=False, cash=cash, debt=Decimal("0"), tos_accepted_at=now)
        db.add(user)
        await db.flush()
        audit_service.record(db, "user_register", user_id=user.id,
                             payload={"source": "loadtest", "run_id": tag, "tos_preaccepted_for_test": True},
                             user_after=audit_service.user_snapshot(user))
        identities.append({"uid": user.id, "username": user.username, "casdoor_id": user.casdoor_id})
    configs = (await db.execute(select(SiteConfig).where(SiteConfig.key.startswith("fx_")))).scalars().all()
    data = {"schema_version": 1, "run_id": tag, "created_at": now.isoformat(),
            "pair_id": pair.id, "currency_code": code, "source_pair_id": source.id,
            "restore_status": "trading", "initial_cash": str(cash), "users": identities,
            "pair_baseline": {"gold_reserve": str(pair.gold_reserve), "foreign_reserve": str(pair.foreign_reserve),
                              "buy_fee_rate": str(pair.buy_fee_rate), "sell_fee_rate": str(pair.sell_fee_rate)},
            "fx_config_at_prepare": {row.key: row.value for row in configs}}
    # Write before caller commits: failure writing the manifest rolls back DB work.
    # A failed DB commit may leave this file; inspect it rather than re-preparing.
    private_json(manifest, data)
    return data


async def checked_run(db, data: dict):
    tag = run_id(data["run_id"])
    rows = (await db.execute(select(User).where(
        User.casdoor_id.startswith(f"loadtest:{tag}:", autoescape=True)))).scalars().all()
    expected = {u["uid"]: u["casdoor_id"] for u in data["users"]}
    if {u.id: u.casdoor_id for u in rows} != expected or not rows:
        raise ValueError("manifest accounts do not match database; inspect before proceeding")
    if any(u.is_superuser or u.is_bot or not u.is_active for u in rows):
        raise ValueError("test accounts must be active ordinary users")
    pair = await db.get(FxPair, data["pair_id"])
    if pair is None or pair.currency_code != data["currency_code"] or pair.currency_name != f"[LT] {tag}":
        raise ValueError("manifest test pair does not match database")
    return rows, pair


async def mint(db, data: dict, output: Path) -> None:
    rows, _ = await checked_run(db, data)
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(minutes=60)
    tokens = [{"uid": u.id, "username": u.username, "token": jwt.encode(
        {"sub": str(u.id), "exp": expiry, "iat": now, "iss": "thccb", "type": "access"},
        settings.SECRET_KEY, algorithm="HS256")} for u in rows]
    private_json(output, {"run_id": data["run_id"], "pair_id": data["pair_id"],
                          "currency_code": data["currency_code"],
                          "expires_at": expiry.isoformat(), "users": tokens})


def export_credentials(root: Path, tag: str, public_key: Path) -> Path:
    from scripts.fx_loadtest_credentials import encrypt
    token_file = root / "tokens.json"
    plaintext = token_file.read_bytes()
    if json.loads(plaintext)["run_id"] != tag:
        raise ValueError("token file belongs to another test run")
    output = root / "credentials.enc.json"
    private_json(output, encrypt(plaintext, public_key.read_bytes()))
    token_file.unlink()
    return output


async def cleanup_scope(db, data: dict):
    rows, pair = await checked_run(db, data)
    ids = [u.id for u in rows]
    pid = pair.id
    # Decline foreign participants and test identities used outside this market.
    for model in (FxWallet, FxShortPosition, FxTrade, FxExecution, FxTriggerOrder):
        foreign = or_((model.pair_id == pid) & model.user_id.not_in(ids),
                      model.user_id.in_(ids) & (model.pair_id != pid))
        if (await db.execute(select(model.id).where(foreign).limit(1))).first():
            raise ValueError(f"unrelated participation in {model.__tablename__}; no cleanup performed")
    allowed = {m.__tablename__ for m in (FxWallet, FxShortPosition, FxTrade, FxExecution,
               FxTriggerOrder, LedgerEntry, LiquidationRun, LiquidationAction, LiquidationEvent, BotSuspicion)}
    # Do not delete unexpected use of test identities in redemption, titles, LMSR,
    # bot profiles or admin references. Existing metadata supplies the FK list.
    for table in SQLModel.metadata.tables.values():
        if table.name in allowed:
            continue
        for column in table.c:
            if any(fk.target_fullname == "user.id" for fk in column.foreign_keys):
                if (await db.execute(select(column).where(column.in_(ids)).limit(1))).first():
                    raise ValueError(f"unexpected test-user reference in {table.name}.{column.name}")
    if (await db.execute(select(BotProfile.id).where(BotProfile.fx_pair_id == pid))).first():
        raise ValueError("test market has bot profiles; detach them explicitly before cleanup")
    predicates = []
    for model in (FxTriggerOrder, FxPoolEvent, FxCandle, FxMarketDataState,
                  FxShortPosition, FxWallet, FxTrade, FxExecution, FxTreasury):
        predicates.append((model, model.pair_id == pid))
    for model in (LiquidationAction, LiquidationEvent, LiquidationRun, LedgerEntry, BotSuspicion):
        predicates.append((model, model.user_id.in_(ids)))
    audit_scope = or_(AuditEvent.user_id.in_(ids),
                      (AuditEvent.ref_table == "fx_pair") & (AuditEvent.ref_id == pid))
    # FX audit records may refer to child entities instead of the pair itself.
    for model in (FxTrade, FxExecution, FxShortPosition, FxTriggerOrder, FxTreasury, FxPoolEvent):
        audit_scope = or_(audit_scope, (AuditEvent.ref_table == model.__tablename__) &
                         AuditEvent.ref_id.in_(select(model.id).where(model.pair_id == pid)))
    # Snapshot matching audit IDs before child deletes invalidate the subqueries.
    audit_ids = list((await db.execute(select(AuditEvent.id).where(audit_scope))).scalars())
    counts = {model.__tablename__: (await db.execute(select(func.count()).select_from(model).where(predicate))).scalar_one()
              for model, predicate in predicates}
    counts.update(user=len(ids), fx_pair=1, audit_event=len(audit_ids))
    return ids, predicates, audit_ids, counts


async def cleanup(db, data: dict, *, execute: bool = False) -> dict:
    if execute:
        await require_stopped(db)
    ids, predicates, audit_ids, counts = await cleanup_scope(db, data)
    source = await db.get(FxPair, data["source_pair_id"])
    if source is None or source.status != "paused":
        raise ValueError("original market is no longer paused; resolve its status before cleanup")
    others = (await db.execute(select(FxPair.id).where(FxPair.status == "trading", FxPair.id != data["pair_id"]))).all()
    if others:
        raise ValueError("another market is trading; refusing automatic restoration")
    if execute:
        for model, predicate in predicates:
            await db.execute(delete(model).where(predicate))
        await db.execute(delete(AuditEvent).where(AuditEvent.id.in_(audit_ids)))
        await db.execute(delete(FxPair).where(FxPair.id == data["pair_id"]))
        await db.execute(delete(User).where(User.id.in_(ids)))
        source.status = data["restore_status"]
    return counts


async def main(args) -> None:
    root = Path(args.output_dir).resolve() / run_id(args.run_id)
    manifest = root / "manifest.json"
    if args.action in {"prepare", "cleanup"} and (args.action == "prepare" or args.execute) and not args.confirm_stopped:
        raise ValueError("mutating maintenance requires --confirm-stopped after stopping economic writers")
    async with async_session_maker() as db:
        async with db.begin():
            if args.action == "prepare":
                data = await prepare(db, tag=args.run_id, users=args.users,
                                     cash=Decimal(args.initial_cash), source_pair=args.source_pair,
                                     manifest=manifest)
                print(json.dumps({"run_id": data["run_id"], "pair_id": data["pair_id"],
                                  "users": len(data["users"]), "manifest": str(manifest)}))
            else:
                data = json.loads(manifest.read_text())
                if data["run_id"] != args.run_id:
                    raise ValueError("manifest run ID mismatch")
                if args.action == "export":
                    output = export_credentials(root, args.run_id, Path(args.recipient_public_key))
                    print(f"Encrypted test credentials: {output}; plaintext token file removed.")
                elif args.action == "mint":
                    await mint(db, data, root / "tokens.json")
                    print(f"Ordinary-user credentials written privately to {root / 'tokens.json'}; expire in 60 minutes")
                else:
                    counts = await cleanup(db, data, execute=args.action == "cleanup" and args.execute)
                    print(json.dumps({"run_id": args.run_id, "counts": counts,
                                      "execute": args.action == "cleanup" and args.execute}, indent=2))
    if args.action == "cleanup" and args.execute:
        (root / "tokens.json").unlink(missing_ok=True)
        print("Cleaned test data and restored original market. Manifest retained for the record.")


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version="fx-loadtest-2")
    parser.add_argument("action", choices=["prepare", "mint", "inspect", "cleanup", "export"])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", default="/app/backups/fx-loadtest")
    parser.add_argument("--source-pair", type=int)
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--initial-cash", default="100000")
    parser.add_argument("--confirm-stopped", action="store_true")
    parser.add_argument("--execute", action="store_true", help="cleanup only: delete this run's data")
    parser.add_argument("--recipient-public-key", help="export only: PEM public-key path, never a private key")
    args = parser.parse_args()
    if args.action == "prepare" and args.source_pair is None:
        parser.error("prepare requires --source-pair")
    if args.execute and args.action != "cleanup":
        parser.error("--execute is only for cleanup")
    if args.action == "export" and not args.recipient_public_key:
        parser.error("export requires --recipient-public-key")
    async def run():
        try:
            await main(args)
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    except (ValueError, OSError) as exc:
        parser.exit(1, f"fx-loadtest: {exc}\n")


if __name__ == "__main__":
    cli()
