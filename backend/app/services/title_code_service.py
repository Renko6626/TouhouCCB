"""Title 激活码 + batch 业务逻辑。

包含 batch 创建 / 列表 / CSV 解析校验 / 兑换 — Task 7-9 逐步填充。
"""
import re
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from fastapi import HTTPException

from app.models.title import Title, TitleCodeBatch, TitleCode

_CODE_RE = re.compile(r"^[A-Za-z0-9\-_]{4,64}$")
CSV_HARDCAP = 5000


async def create_batch(
    db: AsyncSession, title_id: int, name: str, description: str, admin_id: int,
) -> TitleCodeBatch:
    t = await db.get(Title, title_id)
    if not t:
        raise HTTPException(status_code=404, detail="title not found")
    if not t.is_active:
        raise HTTPException(status_code=400, detail="该称号已软删，不能新建批次")
    b = TitleCodeBatch(
        title_id=title_id, name=name, description=description,
        created_by_admin_id=admin_id,
    )
    db.add(b)
    await db.commit()
    await db.refresh(b)
    return b


async def list_batches_with_counts(db: AsyncSession) -> List[dict]:
    """列出全部 batch，每个 batch 附 used/total + title_name。"""
    batches = list((await db.execute(
        select(TitleCodeBatch, Title.name)
        .join(Title, TitleCodeBatch.title_id == Title.id)
        .order_by(TitleCodeBatch.id.desc())
    )).all())
    rows = []
    for b, title_name in batches:
        total = (await db.execute(
            select(func.count()).select_from(TitleCode).where(TitleCode.batch_id == b.id)
        )).scalar_one()
        used = (await db.execute(
            select(func.count()).select_from(TitleCode).where(
                TitleCode.batch_id == b.id, TitleCode.status == "used",
            )
        )).scalar_one()
        rows.append({
            "id": b.id, "title_id": b.title_id, "title_name": title_name,
            "name": b.name, "description": b.description,
            "total": int(total), "used": int(used),
            "created_at": b.created_at,
        })
    return rows
