"""风控请求日志：非 GET API 请求的客户端指纹。只记录，不拦截。

设计要点：
- 只记录非 GET 的 ``/api/v1/*`` 请求（GET 轮询量太大，且没有操作语义）；
- 跳过 ``is_bot=true`` 的 PvE 机器人（它们通过本机回环 HTTP 下单）；
- 追加写入 ``data/risk/risk-YYYY-MM-DD.jsonl``，按天切分，保留 30 天；
- 任何写日志失败都只打 error 日志，绝不影响业务请求；
- 不记录请求体、Authorization 原文等敏感内容。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Request

logger = logging.getLogger(__name__)

_ENABLED = os.getenv("RISK_LOG_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
_DIR = Path(os.getenv("RISK_LOG_DIR", "data/risk"))
_RETENTION_DAYS = 30

_lock = threading.Lock()
_handler: Optional[tuple[str, Any]] = None

# PvE 机器人 id 缓存：回环 HTTP 与真实玩家走同一套 API，必须按 is_bot 排除。
_BOT_TTL_SEC = 60.0
_bot_ids: set[int] = set()
_bots_at = 0.0

_SCRIPT_UA_MARKERS = (
    "python-requests", "python-httpx", "aiohttp", "curl/", "wget/", "okhttp",
    "go-http-client", "node-fetch", "axios/", "java/", "libwww-perl", "httpclient",
)


def enabled() -> bool:
    return _ENABLED


def _cleanup(today: datetime) -> None:
    cutoff = today - timedelta(days=_RETENTION_DAYS)
    try:
        for path in _DIR.glob("risk-*.jsonl"):
            stamp = path.stem.removeprefix("risk-")
            try:
                day = datetime.strptime(stamp, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if day < cutoff:
                path.unlink(missing_ok=True)
    except Exception:
        logger.exception("risk log: cleanup failed")


def _file_for(day: datetime):
    global _handler
    stamp = day.strftime("%Y-%m-%d")
    if _handler is not None and _handler[0] == stamp:
        return _handler[1]
    if _handler is not None:
        try:
            _handler[1].close()
        except Exception:
            pass
    _DIR.mkdir(parents=True, exist_ok=True)
    handle = open(_DIR / f"risk-{stamp}.jsonl", "a", encoding="utf-8", buffering=1)
    _handler = (stamp, handle)
    _cleanup(day)
    return handle


def _write(record: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        _file_for(now).write(line + "\n")


def client_hint(user_agent: str, headers: dict[str, str]) -> str:
    """粗略标记客户端类型；只给人工复核参考，不用于拦截。"""
    ua = (user_agent or "").lower()
    if not ua:
        return "script"
    if "headless" in ua:
        return "headless"
    if any(marker in ua for marker in _SCRIPT_UA_MARKERS):
        return "script"
    if "firefox/" in ua or ("safari/" in ua and "chrome/" not in ua):
        return "browser"
    if "mozilla/" in ua and not (headers.get("sec-ch-ua") or headers.get("sec-fetch-mode")):
        return "browser_like_missing_headers"
    return "browser"


def _user_id(request: Request) -> Optional[int]:
    from app.core.config import settings  # 延迟导入，避免循环依赖

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        import jwt as pyjwt

        payload = pyjwt.decode(
            token, settings.SECRET_KEY, algorithms=["HS256"], options={"verify_exp": False},
        )
        sub = payload.get("sub")
        return int(sub) if sub is not None else None
    except Exception:
        return None


def _client_ip(request: Request) -> str:
    ali = request.headers.get("ali-real-client-ip")
    if ali:
        return ali.strip()
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


def _client_token_state(request: Request, user_id: Optional[int]) -> str:
    from app.core.config import settings

    if not settings.CLIENT_TOKEN_SECRET:
        return "off"
    token = request.headers.get("x-client-token")
    ts = request.headers.get("x-client-ts")
    if not token or not ts:
        return "missing"
    if user_id is None:
        return "bad"
    from app.services.anti_bot import verify_client_token

    return "valid" if verify_client_token(token, ts, user_id) else "bad"


async def _is_bot(user_id: int) -> bool:
    global _bot_ids, _bots_at

    now = time.monotonic()
    if now - _bots_at >= _BOT_TTL_SEC:
        _bots_at = now  # 先占位，避免并发请求同时打 DB
        try:
            from sqlalchemy import select

            from app.core.database import async_session_maker
            from app.models.base import User

            async with async_session_maker() as db:
                _bot_ids = set((await db.execute(
                    select(User.id).where(User.is_bot == True)  # noqa: E712
                )).scalars().all())
        except Exception:
            logger.exception("risk log: bot id refresh failed")
    return user_id in _bot_ids


async def record(request: Request, status_code: int, duration_ms: float) -> None:
    """记录一笔请求。调用方负责过滤 GET / 非 API 路径。"""
    if not _ENABLED:
        return
    try:
        user_id = _user_id(request)
        if user_id is not None and await _is_bot(user_id):
            return
        headers = request.headers
        _write({
            "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "user_id": user_id,
            "method": request.method,
            "path": request.url.path,
            "status": status_code,
            "duration_ms": round(duration_ms, 1),
            "ip": _client_ip(request),
            "ua": headers.get("user-agent", ""),
            "sec_ch_ua": headers.get("sec-ch-ua", ""),
            "sec_ch_ua_mobile": headers.get("sec-ch-ua-mobile", ""),
            "sec_ch_ua_platform": headers.get("sec-ch-ua-platform", ""),
            "sec_fetch_mode": headers.get("sec-fetch-mode", ""),
            "sec_fetch_site": headers.get("sec-fetch-site", ""),
            "sec_fetch_dest": headers.get("sec-fetch-dest", ""),
            "accept_language": headers.get("accept-language", ""),
            "referer": headers.get("referer", ""),
            "origin": headers.get("origin", ""),
            "client_token": _client_token_state(request, user_id),
            "client_hint": client_hint(headers.get("user-agent", ""), headers),
        })
    except Exception:
        logger.exception("risk log: write failed")
