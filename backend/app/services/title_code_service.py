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


def parse_csv_codes(raw_bytes: bytes) -> List[str]:
    """解析单列 CSV（一行一个 code，可带表头）。整批 reject on any invalid。"""
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV 必须是 UTF-8 编码")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and lines[0].lower() in ("code", "codes", "code_string"):
        lines = lines[1:]
    if len(lines) == 0:
        raise HTTPException(status_code=400, detail="CSV 不含任何 code")
    if len(lines) > CSV_HARDCAP:
        raise HTTPException(
            status_code=400,
            detail=f"CSV 行数 {len(lines)} 超过单批上限 {CSV_HARDCAP}",
        )
    seen = set()
    for idx, code in enumerate(lines, 1):
        if not _CODE_RE.match(code):
            raise HTTPException(
                status_code=400,
                detail=f"第 {idx} 行 code '{code}' 格式不合法（仅 A-Z a-z 0-9 - _，长度 4-64）",
            )
        if code in seen:
            raise HTTPException(
                status_code=400,
                detail=f"第 {idx} 行 code '{code}' 在文件内重复",
            )
        seen.add(code)
    return lines


async def import_codes_to_batch(
    db: AsyncSession, batch_id: int, codes: List[str],
) -> int:
    """整批插入 codes 到 batch。任一与库内已有冲突 → 整批 reject。"""
    b = await db.get(TitleCodeBatch, batch_id)
    if not b:
        raise HTTPException(status_code=404, detail="batch not found")
    existing = list((await db.execute(
        select(TitleCode.code_string).where(TitleCode.code_string.in_(codes))
    )).scalars().all())
    if existing:
        sample = existing[:5]
        raise HTTPException(
            status_code=400,
            detail=f"以下 code 已存在于库中: {sample} 等 {len(existing)} 个",
        )
    for c in codes:
        db.add(TitleCode(batch_id=batch_id, code_string=c, status="available"))
    await db.commit()
    return len(codes)
