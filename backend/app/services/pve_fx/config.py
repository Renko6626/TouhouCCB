"""FX-specific config persistence; caller owns the transaction."""
import json
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.base import SiteConfig
from app.schemas.pve_fx import FxPairBotConfig, FxPveConfig
from app.services import audit_service, site_config

TICK_SECONDS = 5


async def load_fx_config(db: AsyncSession) -> FxPveConfig:
    raw = await site_config.get_many(db, ["fx_pve_enabled", "fx_pve_pair_configs"])
    return FxPveConfig(enabled=raw.get("fx_pve_enabled", "false").lower() in {"true", "1", "yes"},
                       pair_configs=json.loads(raw.get("fx_pve_pair_configs", "{}")))


async def load_pair_config(db: AsyncSession, pair_id: int) -> FxPairBotConfig:
    return (await load_fx_config(db)).pair_configs.get(pair_id, FxPairBotConfig())


async def save_fx_config(db: AsyncSession, config: FxPveConfig, operator_id: int) -> None:
    config = FxPveConfig.model_validate(config.model_dump())
    for key, value, value_type in (
        ("fx_pve_enabled", str(config.enabled).lower(), "bool"),
        ("fx_pve_pair_configs", json.dumps(config.model_dump(mode="json")["pair_configs"]), "string"),
    ):
        row = (await db.execute(select(SiteConfig).where(SiteConfig.key == key))).scalar_one_or_none()
        old = row.value if row else None
        if row is None:
            row = SiteConfig(key=key, value=value, value_type=value_type)
        row.value = value
        row.updated_by = operator_id
        row.updated_at = datetime.now(timezone.utc)
        db.add(row)
        audit_service.record(db, "config_set", operator_user_id=operator_id,
                             payload={"key": key, "old": old, "new": value, "value_type": value_type})
    await db.flush()
    site_config.clear_cache()
