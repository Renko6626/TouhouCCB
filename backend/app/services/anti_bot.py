"""Anti-bot L2 HMAC client_token 验证 + L3 白名单 csv 解析。

设计：纯函数模块，无 DB / 无 IO（除 settings）。verify_client_token 用 HMAC-SHA256
+ 30s 时间窗 + hmac.compare_digest 防 timing attack。
"""
from __future__ import annotations
import hashlib
import hmac
import time

from app.core.config import settings


def verify_client_token(token: str, ts: str, uid: int) -> bool:
    """验证前端 HMAC token。失败返 False 不抛异常，调用方决定 403。"""
    if not settings.CLIENT_TOKEN_SECRET:
        return False
    try:
        ts_int = int(ts)
    except (ValueError, TypeError):
        return False
    # 时间窗 30s 容差客户端时钟漂移
    if abs(time.time() - ts_int) > 30:
        return False
    expected = hmac.new(
        settings.CLIENT_TOKEN_SECRET.encode(),
        f"{ts}|{uid}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, token)


def parse_whitelist(csv_str: str) -> set[int]:
    """解析 csv 白名单 '1, 5, abc, 12' → {1, 5, 12}。容错非数字 + 空格。"""
    if not csv_str:
        return set()
    return {
        int(x.strip()) for x in csv_str.split(",")
        if x.strip().isdigit()
    }


def generate_client_token_for_test(secret: str, uid: int, ts: int) -> str:
    """测试辅助。生产前端用 SubtleCrypto；这是相同算法的服务端版本。"""
    return hmac.new(
        secret.encode(),
        f"{ts}|{uid}".encode(),
        hashlib.sha256,
    ).hexdigest()
