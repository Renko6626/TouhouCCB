"""The operator's site access switch; economic background work is unaffected."""
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import site_config

KEY = "site_access_disabled"


async def is_disabled(db: AsyncSession) -> bool:
    # Reuse the existing process cache and its immediate invalidation on config writes.
    return await site_config.get_bool_or(db, KEY, False)
