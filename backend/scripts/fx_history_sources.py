"""Paged offline source projection and chronological reserve-chain validation."""
from datetime import timezone
from decimal import Decimal
from sqlalchemy import select
from app.models.audit import AuditEvent
from app.models.fx import FxPoolEvent, FxTrade

PAGE_SIZE = 1000


def utc(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def reserves(value):
    try:
        result = tuple(Decimal(value[k]) for k in ('gold', 'foreign'))
        if not all(v.is_finite() and v > 0 and v == v.quantize(Decimal('0.000001')) for v in result):
            raise ValueError()
        return result
    except (KeyError, TypeError, ArithmeticError, ValueError):
        raise ValueError('missing or invalid historical pool reserves') from None


async def source_pages(db, pair_id, model, cutoff):
    is_trade = model is FxTrade
    reference = FxPoolEvent.trade_id if is_trade else FxPoolEvent.audit_id
    conditions = [model.pair_id == pair_id] if is_trade else [model.ref_table == 'fx_pair', model.ref_id == pair_id, model.event_type.in_(['fx_fund', 'fx_withdraw'])]
    after = 0
    while True:
        stmt = select(model, FxPoolEvent).outerjoin(FxPoolEvent, reference == model.id).where(*conditions, model.id <= cutoff)
        stmt = stmt.where(model.id > after)
        page = (await db.execute(stmt.order_by(model.id).limit(PAGE_SIZE))).all()
        if not page:
            return
        for source, existing in page:
            at = existing.created_at if existing else (source.created_at if is_trade else source.ts)
            if is_trade:
                pre = (source.pre_gold_reserve, source.pre_foreign_reserve)
                post = (source.post_gold_reserve, source.post_foreign_reserve)
                kind = 'trade'
                volume = source.input_amount if source.side == 'buy' else source.output_amount
            else:
                initial = source.payload.get('action') == 'initial_issuance'
                kind = 'initialize' if initial else ('fund' if source.event_type == 'fx_fund' else 'withdraw')
                pre = None if initial else reserves(source.payload.get('pool_before'))
                post = reserves(source.payload.get('pool_after'))
                volume = Decimal(0)
            data = dict(pair_id=pair_id, created_at=utc(at), kind=kind,
                        pre_gold_reserve=None if pre is None else pre[0], pre_foreign_reserve=None if pre is None else pre[1],
                        post_gold_reserve=post[0], post_foreign_reserve=post[1], gold_volume=volume,
                        trade_id=source.id if is_trade else None, audit_id=None if is_trade else source.id)
            if existing:
                if existing.pair_id != pair_id or any(getattr(existing, k) != v for k, v in data.items() if k != 'created_at'):
                    raise ValueError(f'source/content mismatch for {kind} {source.id}')
            yield data, None if existing is None else existing.id
        source, existing = page[-1]
        after = source.id


async def merged_sources(db, pair_id, trade_cutoff, audit_cutoff):
    # This rare offline path deliberately accepts O(N) memory. DB reads remain
    # indexed ID-keyset pages; cross-source chronological sorting is explicit.
    rows = []
    for model, cutoff in ((FxTrade, trade_cutoff), (AuditEvent, audit_cutoff)):
        async for item in source_pages(db, pair_id, model, cutoff):
            rows.append(item)
    rows.sort(key=lambda item: item[0]['created_at'])
    for item in rows:
        yield item


def order_group(group, current):
    """Equal timestamps require a unique chain; source IDs never break ties."""
    result = []
    pending = list(group)
    while pending:
        candidates = [x for x in pending if ((x[0]['kind'] == 'initialize' and current is None) or
                      (x[0]['kind'] != 'initialize' and (x[0]['pre_gold_reserve'], x[0]['pre_foreign_reserve']) == current))]
        if len(candidates) != 1:
            at = pending[0][0]['created_at']
            raise ValueError(f'incomplete or ambiguous reserve chain at {at}; {len(pending)} unresolved sources')
        item = candidates[0]
        pending.remove(item)
        result.append(item)
        current = (item[0]['post_gold_reserve'], item[0]['post_foreign_reserve'])
    return result, current
