"""Fixed-policy business reset. Source reads never depend on historical models.

Private manifests contain account and title-code data. Only summary() is safe
for logs. Import is one transaction, including the real baseline migration.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import BigInteger, Integer, MetaData, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.schema import BASELINE_REVISION, classify_schema, require_schema_current
from app.services.config_seed import DEFAULT_CONFIGS, resolve_credit_config

FORMAT = "business-reset-v1"
TITLES = ("title", "user_title", "title_code_batch", "title_code")
FIELDS = {
    "user": "id casdoor_id username email is_active is_superuser is_bot cash debt debt_last_accrued_at tos_accepted_at last_liquidated_at economic_version credit_frozen equipped_title_id".split(),
    "title": "id name description color icon sort_order is_active created_at".split(),
    "user_title": "id user_id title_id granted_at granted_by_admin_id source".split(),
    "title_code_batch": "id title_id name description created_at created_by_admin_id".split(),
    "title_code": "id batch_id code_string status used_by_user_id used_at".split(),
    "siteconfig": "id key value value_type updated_at updated_by".split(),
}
FK_FIELDS = {
    "user": {("equipped_title_id", "title", "id")},
    "title": set(),
    "user_title": {("user_id", "user", "id"), ("title_id", "title", "id"), ("granted_by_admin_id", "user", "id")},
    "title_code_batch": {("title_id", "title", "id"), ("created_by_admin_id", "user", "id")},
    "title_code": {("batch_id", "title_code_batch", "id"), ("used_by_user_id", "user", "id")},
    "siteconfig": {("updated_by", "user", "id")},
}
GATES = ("fx_enabled", "fx_short_enabled", "loan_enabled", "liquidation_enabled", "pve_enabled")
POLICY = {"cash": "source_initial_balance", "debt": "0", "version": "old+1", "titles": list(TITLES), "closed_gates": list(GATES), "credit_new_risk_frozen": True}
MAX_VERSION = 2**31 - 1


def _pack(value):
    if isinstance(value, Decimal):
        return {"$decimal": str(value.normalize())}
    if isinstance(value, datetime):
        return {"$datetime": value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()}
    if isinstance(value, dict):
        return {k: _pack(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_pack(v) for v in value]
    return value


def _unpack(value):
    if isinstance(value, dict):
        if set(value) == {"$decimal"}:
            return Decimal(value["$decimal"])
        if set(value) == {"$datetime"}:
            return datetime.fromisoformat(value["$datetime"])
        return {k: _unpack(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unpack(v) for v in value]
    return value


def _digest(manifest):
    return hashlib.sha256(json.dumps({k: v for k, v in manifest.items() if k != "digest"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _check_manifest(m):
    if m.get("format") != FORMAT or m.get("policy") != POLICY or m.get("digest") != _digest(m):
        raise ValueError("Invalid reset manifest")


def _metadata(conn):
    md = MetaData()
    md.reflect(conn)
    return md


def _rows(conn, table):
    return [dict(r) for r in conn.execute(select(table)).mappings()]


def _amount(value):
    value = Decimal(str(value))
    if not value.is_finite() or value < 0 or value >= Decimal("10000000000") or value != value.quantize(Decimal("0.000001")):
        raise ValueError("Invalid account amount")
    return value


def _configs(rows):
    # Use the current API's declared keys and validation rather than copying old keys.
    from app.api.v1.site_config import _WHITELIST, _validate
    from app.services.site_config import FX_CONFIG_RULES

    defaults = {k: (v, t) for k, v, t in DEFAULT_CONFIGS}
    allowed = {**{k: t for k, (_, t) in defaults.items()}, **_WHITELIST, "pve_enabled": "bool"}
    raw = {r["key"]: r for r in rows}
    if "initial_balance" not in raw:
        raise ValueError("Source initial_balance is required")
    resolve_credit_config({k: r["value"] for k, r in raw.items()})
    values = {k: {"key": k, "value": v, "value_type": t} for k, (v, t) in defaults.items()}
    for k, r in raw.items():
        if k not in allowed:
            continue
        v, t = r["value"], r["value_type"]
        if t != allowed[k] or not isinstance(v, str):
            raise ValueError("Invalid current configuration type")
        if k in _WHITELIST:
            try:
                _validate(k, v)
            except Exception as exc:
                raise ValueError("Invalid current configuration") from exc
        elif k in FX_CONFIG_RULES:
            if not FX_CONFIG_RULES[k](v):
                raise ValueError("Invalid FX configuration")
        elif t == "bool" and v.lower() not in ("true", "false"):
            raise ValueError("Invalid boolean configuration")
        elif t == "int":
            int(v)
        elif t == "decimal" and not Decimal(v).is_finite():
            raise ValueError("Invalid decimal configuration")
        values[k] = {"key": k, "value": v, "value_type": t}
    balance = _amount(raw["initial_balance"]["value"])
    for k in GATES:
        values[k] = {"key": k, "value": "false", "value_type": "bool"}
    values["credit_new_risk_frozen"] = {"key": "credit_new_risk_frozen", "value": "true", "value_type": "bool"}
    return balance, [values[k] for k in sorted(values)]


def _highwaters(conn, md):
    values = {}
    for name, table in md.tables.items():
        pk = list(table.primary_key.columns)
        if len(pk) != 1 or pk[0].name != "id" or not isinstance(pk[0].type, Integer):
            continue
        quote = conn.dialect.identifier_preparer.quote
        maximum = conn.execute(text(f"SELECT MAX({quote(pk[0].name)}) FROM {quote(name)}")).scalar() or 0
        if conn.dialect.name == "sqlite" and inspect(conn).has_table("sqlite_sequence"):
            old = conn.execute(text("SELECT seq FROM sqlite_sequence WHERE name=:n"), {"n": name}).scalar() or 0
            maximum = max(maximum, old)
        elif conn.dialect.name == "postgresql":
            seq = conn.execute(text("SELECT pg_get_serial_sequence(:t,:c)"), {"t": name, "c": pk[0].name}).scalar()
            if seq:
                last, called = conn.execute(text(f"SELECT last_value, is_called FROM {seq}")).one()
                maximum = max(maximum, last if called else last - 1)
        limit = 2**63 - 1 if isinstance(pk[0].type, BigInteger) or conn.dialect.name == "sqlite" else MAX_VERSION
        if not isinstance(maximum, int) or maximum < 0 or maximum >= limit:
            raise ValueError("Invalid or exhausted ID watermark")
        values[name] = maximum
    return values


def _export(conn, expected):
    md = _metadata(conn)
    for name, fields in FIELDS.items():
        if name not in md.tables or set(md.tables[name].columns.keys()) != set(fields):
            raise ValueError("Unknown or missing retained fields")
        actual_fks = {(fk.parent.name, fk.column.table.name, fk.column.name) for fk in md.tables[name].foreign_keys}
        if actual_fks != FK_FIELDS[name]:
            raise ValueError("Unknown or missing retained foreign key")
    rows = {n: _rows(conn, md.tables[n]) for n in FIELDS}
    balance, configs = _configs(rows.pop("siteconfig"))
    if expected is not None and balance != _amount(expected):
        raise ValueError("Source initial_balance changed")
    users = {u["id"]: u for u in rows["user"]}
    if any(type(u["is_bot"]) is not bool for u in users.values()):
        raise ValueError("Invalid account bot flag")
    needed = {u["id"] for u in users.values() if u["is_bot"] is False}
    for name in TITLES:
        for row in rows[name]:
            for fk in md.tables[name].foreign_keys:
                if fk.column.table.name == "user" and row[fk.parent.name] is not None:
                    needed.add(row[fk.parent.name])
    if not needed <= users.keys():
        raise ValueError("Missing title dependency identity")
    rows["user"] = [users[i].copy() for i in sorted(needed)]
    for u in rows["user"]:
        if any(type(u[k]) is not bool for k in ("is_active", "is_superuser", "credit_frozen")):
            raise ValueError("Invalid account status")
        _amount(u["cash"])
        _amount(u["debt"])
        v = u["economic_version"]
        if type(v) is not int or v < 0 or v >= MAX_VERSION:
            raise ValueError("Invalid or exhausted economic version")
        if type(u["is_bot"]) is not bool:
            raise ValueError("Invalid account bot flag")
        u.update(cash=Decimal(0) if u["is_bot"] else balance, debt=Decimal(0), debt_last_accrued_at=None, last_liquidated_at=None, credit_frozen=False, economic_version=v + 1)
        if u["is_bot"]:
            u.update(is_active=False, equipped_title_id=None)
    # Validate every retained FK before any target is created.
    for name, records in rows.items():
        for row in records:
            for fk in md.tables[name].foreign_keys:
                value = row[fk.parent.name]
                if value is not None and value not in {r[fk.column.name] for r in rows[fk.column.table.name]}:
                    raise ValueError("Missing retained foreign key")
    counts = {n: conn.execute(select(func.count()).select_from(t)).scalar_one() for n, t in md.tables.items() if n != "alembic_version"}
    revision = _rows(conn, md.tables["alembic_version"]) if "alembic_version" in md.tables else []
    manifest = _pack({"format": FORMAT, "policy": POLICY, "initial_balance": balance, "source_revision": revision, "rows": rows, "configs": configs, "highwaters": _highwaters(conn, md), "source_counts": counts, "created_at": datetime.now(timezone.utc)})
    manifest["digest"] = _digest(manifest)
    return manifest


async def export_database(source_url, expected_initial_balance=None):
    engine = create_async_engine(source_url)
    try:
        async with engine.connect() as conn:
            if engine.dialect.name == "postgresql":
                await conn.execute(text("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            else:
                await conn.execute(text("PRAGMA query_only=ON"))
                await conn.execute(text("BEGIN"))
            try:
                return await conn.run_sync(lambda c: _export(c, expected_initial_balance))
            finally:
                await conn.rollback()
    finally:
        await engine.dispose()


def _restore_watermarks(conn, md, waters):
    if not set(waters) <= set(md.tables):
        raise ValueError("Unknown source integer-ID table")
    for name, value in waters.items():
        table = md.tables[name]
        pk = list(table.primary_key.columns)
        if len(pk) != 1 or not isinstance(pk[0].type, Integer):
            raise ValueError("Unknown source integer-ID mapping")
        if conn.dialect.name == "sqlite":
            conn.execute(text("DELETE FROM sqlite_sequence WHERE name=:n"), {"n": name})
            conn.execute(text("INSERT INTO sqlite_sequence(name,seq) VALUES(:n,:v)"), {"n": name, "v": value})
        else:
            seq = conn.execute(text("SELECT pg_get_serial_sequence(:t,:c)"), {"t": name, "c": pk[0].name}).scalar()
            if not seq:
                raise ValueError("Missing target sequence")
            conn.execute(text("SELECT setval(CAST(:s AS regclass), :v, :called)"), {"s": seq, "v": max(value, 1), "called": value > 0})


def _audit_rows(m):
    audits = []
    for u in m["rows"]["user"]:
        audits.append({"event_type": "user_register", "user_id": u["id"], "payload": {"source": "business_reset", "manifest_digest": m["digest"], "initial_balance": str(u["cash"]), "economic_version": u["economic_version"], "is_bot": u["is_bot"]}, "user_after": {"cash": str(u["cash"].normalize()), "debt": "0", "debt_last_accrued_at": None}})
    for c in m["configs"]:
        audits.append({"event_type": "config_set", "user_id": None, "payload": {"key": c["key"], "old": None, "new": c["value"], "value_type": c["value_type"], "source": "business_reset", "manifest_digest": m["digest"]}, "user_after": None})
    return audits


def _migration_config():
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    return cfg


def _verify(conn, manifest):
    _check_manifest(manifest)
    classify_schema(conn)
    installed = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if installed != ScriptDirectory.from_config(_migration_config()).get_heads():
        raise ValueError("Target schema is not current")
    m = _unpack(manifest)
    md = _metadata(conn)
    for name, expected in m["rows"].items():
        actual = _rows(conn, md.tables[name])
        sort = lambda records: sorted((_pack(r) for r in records), key=lambda r: r["id"])
        if sort(actual) != sort(expected):
            raise ValueError("Retained data verification failed")
    configs = sorted(({k: r[k] for k in ("key", "value", "value_type")} for r in _rows(conn, md.tables["siteconfig"])), key=lambda r: r["key"])
    if configs != m["configs"]:
        raise ValueError("Configuration verification failed")
    actual_audits = sorted(_rows(conn, md.tables["audit_event"]), key=lambda r: r["id"])
    expected_audits = _audit_rows(m)
    if len(actual_audits) != len(expected_audits):
        raise ValueError("Audit count verification failed")
    for a, e in zip(actual_audits, expected_audits):
        if any(a[k] != v for k, v in e.items()) or a["id"] <= m["highwaters"].get("audit_event", 0) or any(a[k] is not None for k in ("market_id", "outcome_id", "operator_user_id", "ref_table", "ref_id", "position_after", "market_after")):
            raise ValueError("Audit anchor verification failed")
    retained = set(FIELDS) | {"audit_event", "alembic_version"}
    for name, table in md.tables.items():
        if name not in retained and conn.execute(select(table).limit(1)).first() is not None:
            raise ValueError("Old business data verification failed")
    waters = _highwaters(conn, md)
    for name, value in m["highwaters"].items():
        expected = value + (len(expected_audits) if name == "audit_event" else len(m["configs"]) if name == "siteconfig" else 0)
        if waters[name] != expected:
            raise ValueError("ID watermark verification failed")
    return summary(manifest)


def _import(conn, manifest):
    if inspect(conn).get_table_names():
        raise ValueError("Target database is not empty")
    cfg = _migration_config()
    cfg.attributes["connection"] = conn
    command.upgrade(cfg, "head")
    md = _metadata(conn)
    m = _unpack(manifest)
    _restore_watermarks(conn, md, m["highwaters"])
    users = m["rows"]["user"]
    if users:
        conn.execute(md.tables["user"].insert(), [{**u, "equipped_title_id": None} for u in users])
    for name in TITLES:
        if m["rows"][name]:
            conn.execute(md.tables[name].insert(), m["rows"][name])
    for u in users:
        if u["equipped_title_id"] is not None:
            conn.execute(md.tables["user"].update().where(md.tables["user"].c.id == u["id"]).values(equipped_title_id=u["equipped_title_id"]))
    now = datetime.now(timezone.utc)
    conn.execute(md.tables["siteconfig"].insert(), [{**c, "updated_at": now, "updated_by": None} for c in m["configs"]])
    for a in _audit_rows(m):
        conn.execute(md.tables["audit_event"].insert().values(**a, ts=now))
    return _verify(conn, manifest)


def _same_database(a, b):
    a, b = make_url(a), make_url(b)
    if a.get_backend_name() == b.get_backend_name() == "sqlite":
        return Path(a.database).resolve() == Path(b.database).resolve()
    return (a.get_backend_name(), a.host, a.port or 5432, a.database) == (b.get_backend_name(), b.host, b.port or 5432, b.database)


async def import_database(source_url, target_url, manifest):
    _check_manifest(manifest)
    if _same_database(source_url, target_url):
        raise ValueError("Source and target must differ")
    current = await export_database(source_url, _unpack(manifest)["initial_balance"])
    # A timestamp changes each export; retained policy, data, counts and waters must not.
    for key in ("rows", "configs", "highwaters", "source_counts", "source_revision"):
        if current[key] != manifest[key]:
            raise ValueError("Source changed after export")
    engine = create_async_engine(target_url)
    try:
        async with engine.begin() as conn:
            if engine.dialect.name == "sqlite":
                await conn.execute(text("PRAGMA foreign_keys=ON"))
                await conn.execute(text("BEGIN IMMEDIATE"))
            result = await conn.run_sync(lambda c: _import(c, manifest))
        await require_schema_current(engine)
        return result
    finally:
        await engine.dispose()


async def verify_database(target_url, manifest):
    engine = create_async_engine(target_url)
    try:
        await require_schema_current(engine)
        async with engine.connect() as conn:
            if engine.dialect.name == "postgresql":
                await conn.execute(text("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            else:
                await conn.execute(text("PRAGMA query_only=ON"))
                await conn.execute(text("BEGIN"))
            try:
                return await conn.run_sync(lambda c: _verify(c, manifest))
            finally:
                await conn.rollback()
    finally:
        await engine.dispose()


def summary(manifest):
    m = _unpack(manifest)
    return {"format": FORMAT, "initial_balance": str(m["initial_balance"]), "humans": sum(not u["is_bot"] for u in m["rows"]["user"]), "dependency_accounts": sum(u["is_bot"] for u in m["rows"]["user"]), "title_counts": {n: len(m["rows"][n]) for n in TITLES}, "source_counts": m["source_counts"], "source_revision": m["source_revision"], "manifest_digest": m["digest"]}


def save_manifest(directory, manifest):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("Reset directory must be private (0700)")
    path = directory / "manifest.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(manifest, stream, sort_keys=True)
    return path


def load_manifest(directory):
    path = Path(directory) / "manifest.json"
    if path.is_symlink() or path.stat().st_mode & 0o077 or Path(directory).stat().st_mode & 0o077:
        raise ValueError("Reset manifest must be private")
    manifest = json.loads(path.read_text())
    _check_manifest(manifest)
    return manifest


def production_urls(run_id):
    if not re.fullmatch(r"[0-9]{1,20}", run_id):
        raise ValueError("Invalid run ID")
    from app.core.config import settings
    url = make_url(settings.DATABASE_URL)
    if url.get_backend_name() != "postgresql" or url.host != "postgres" or (url.port or 5432) != 5432 or url.database != "thccb":
        raise ValueError("Business reset requires fixed Compose PostgreSQL")
    url = url.set(drivername="postgresql+asyncpg")
    return url, url.set(database=f"thccb_reset_{run_id}"), url.set(database=f"thccb_before_{run_id}")


async def _main(args):
    source, target, before = production_urls(args.run_id)
    if args.stage == "config":
        print(json.dumps({"source_database": "thccb", "target_database": target.database, "before_database": before.database, "baseline_revision": BASELINE_REVISION, "policy": POLICY}))
        return
    if args.stage in ("preview", "export"):
        manifest = await export_database(source, args.expected_initial_balance)
        if args.stage == "export":
            save_manifest(args.directory, manifest)
        result = summary(manifest)
    else:
        manifest = load_manifest(args.directory)
        if args.expected_initial_balance is not None and _amount(args.expected_initial_balance) != _unpack(manifest)["initial_balance"]:
            raise ValueError("Expected initial_balance differs from export")
        if args.stage == "import":
            if args.after_cutover:
                raise ValueError("after-cutover is only valid for verify")
            result = await import_database(source, target, manifest)
        else:
            result = await verify_database(source if args.after_cutover else target, manifest)
    print(json.dumps(result, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("config", "preview", "export", "import", "verify"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--expected-initial-balance")
    parser.add_argument("--after-cutover", action="store_true")
    args = parser.parse_args()
    try:
        if args.after_cutover and args.stage != "verify":
            raise ValueError("after-cutover is only valid for verify")
        asyncio.run(_main(args))
    except Exception:
        # Driver exceptions can include SQL parameters and credentials.
        parser.exit(1, "Business reset refused or failed; private data was not printed.\n")


if __name__ == "__main__":
    main()
