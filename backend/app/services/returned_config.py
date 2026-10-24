"""Private admin configuration. Only a digest of the incoming key is stored."""
import hashlib
import hmac
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from app.core.database import managed_transaction
from app.models.base import SiteConfig
from app.models.returned import ReturnedRound
from app.services import audit_service
from app.services.credit.ownership import OWNERSHIP
from app.services.returned import _start_lock

# Keep stored keys so existing settings and secrets survive the product rename.
KEYS = ('wavelength_enabled', 'wavelength_secret_hash', 'wavelength_url')


def secret_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def matches_secret(credential, config):
    return bool(config['secret_hash']) and hmac.compare_digest(
        secret_digest(credential), config['secret_hash'])


async def read_config(db):
    rows = (await db.execute(select(SiteConfig).where(SiteConfig.key.in_(KEYS)))).scalars().all()
    values = {row.key: row.value for row in rows}
    return {'enabled': values.get(KEYS[0]) == 'true',
            'secret_hash': values.get(KEYS[1], ''), 'returned_url': values.get(KEYS[2], '')}


async def public_config(db, config=None):
    config = config if config is not None else await read_config(db)
    active = (await db.execute(select(ReturnedRound.round_key).where(
        ReturnedRound.status.in_(['open', 'closed'])).limit(1))).scalar_one_or_none()
    return {'enabled': config['enabled'], 'returned_url': config['returned_url'],
            'secret_configured': bool(config['secret_hash']), 'active_round_key': active}


async def save_config(db, req, admin_id):
    # Same start lock ensures a new round cannot appear midway through a settings change.
    async with _start_lock:
        async with managed_transaction(db):
            OWNERSHIP.require_writes()
            old = await read_config(db)
            digest = secret_digest(req.secret) if req.secret else old['secret_hash']
            if req.enabled and not digest:
                raise HTTPException(422, '启用前请设置联动密钥')
            new = {'enabled': req.enabled, 'secret_hash': digest, 'returned_url': req.returned_url}
            before = await public_config(db, old)
            if new == old:
                return before
            if before['active_round_key']:
                raise HTTPException(409, '请先结算或取消当前竞猜，再修改联动配置')
            now = datetime.now(timezone.utc)
            for key, value in zip(KEYS, ('true' if req.enabled else 'false', digest, req.returned_url)):
                row = (await db.execute(select(SiteConfig).where(SiteConfig.key == key))).scalar_one_or_none()
                if row is None:
                    row = SiteConfig(key=key, value=value, value_type='bool' if key == KEYS[0] else 'string')
                row.value, row.updated_at, row.updated_by = value, now, admin_id
                db.add(row)
            after = {'enabled': new['enabled'], 'returned_url': new['returned_url'],
                     'secret_configured': bool(digest), 'active_round_key': None}
            audit_service.record(db, 'config_set', operator_user_id=admin_id,
                payload={'key': 'wavelength_integration', 'old': before, 'new': after,
                         'secret_changed': old['secret_hash'] != digest})
        return after
