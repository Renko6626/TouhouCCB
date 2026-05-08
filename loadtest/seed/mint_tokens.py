#!/usr/bin/env python3
"""mint_tokens.py — 给所有 loadtest_* 用户离线签发本站 JWT，写入 tokens.txt。

不走 Casdoor。直接复用 backend 的 create_access_token / SECRET_KEY。
设计上必须在 backend 容器内执行（容器有 /app/.env 和已安装的 jwt 模块）：

    docker compose exec -T backend python /app/loadtest_mint.py > loadtest/tokens.txt

或者把本文件 cp 进容器后再 exec。两种用法见 loadtest/seed/mint_tokens.sh。

输出格式（每行一条，TAB 分隔）：
    <user_id>\\t<username>\\t<token>

token 默认 1 小时过期（与 ACCESS_TOKEN_EXPIRE_MINUTES 一致）；想压更久就改 .env。
"""
from __future__ import annotations

import os
import sys
import asyncio
from typing import List, Tuple

# 容器内启动时，CWD=/app，可直接 import app.*
sys.path.insert(0, "/app")
sys.path.insert(0, ".")

from app.core.users import create_access_token  # noqa: E402
from app.core.database import async_session_maker  # noqa: E402
from sqlalchemy import select  # noqa: E402
from app.models.base import User  # noqa: E402


async def fetch_loadtest_users() -> List[Tuple[int, str]]:
    async with async_session_maker() as session:
        stmt = (
            select(User.id, User.username)
            .where(User.casdoor_id.like("loadtest:%"))
            .order_by(User.id)
        )
        rows = (await session.execute(stmt)).all()
        return [(int(r[0]), str(r[1])) for r in rows]


async def main() -> int:
    users = await fetch_loadtest_users()
    if not users:
        print("ERR: 没有 loadtest_* 用户，先跑 seed_users.sh", file=sys.stderr)
        return 1

    print(f"# minted at user count = {len(users)}", file=sys.stderr)
    for uid, uname in users:
        token = create_access_token(uid)
        print(f"{uid}\t{uname}\t{token}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
