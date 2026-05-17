"""structlog JSON Lines 日志配置。spec §9。"""
import logging
from pathlib import Path
from typing import Any

import structlog


def setup_logging(log_dir: Path, channel: str = "system") -> None:
    """初始化 structlog，写入 <log_dir>/<channel>.jsonl。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{channel}.jsonl"

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str, **bind: Any) -> structlog.BoundLogger:
    return structlog.get_logger(name).bind(**bind)
