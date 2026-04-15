# app/core/config.py

import logging
import secrets

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, model_validator
from urllib.parse import quote_plus
from pathlib import Path

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    PROJECT_NAME: str = "东方炒炒币：饭纲丸龙的野望"

    # 运行环境："development" | "production"
    APP_ENV: str = "development"

    # 数据库后端：默认 SQLite（开箱即用）
    DB_BACKEND: str = "sqlite"  # "sqlite" | "mysql"
    SQLITE_PATH: str = "data/thccb.db"

    # MySQL 配置（当 DB_BACKEND=mysql 时生效）
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "thccb"
    MYSQL_PASSWORD: str = "change_me"
    MYSQL_DB: str = "thccb"

    # 驱动：asyncmy 或 aiomysql（二选一）
    MYSQL_DRIVER: str = "asyncmy"  # "asyncmy" | "aiomysql"

    # 连接池（建议保留默认，后续可调优）
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE: int = 3600  # MySQL 常见断连问题：回收连接
    DB_ECHO: bool = False

    # 也允许你直接用 DATABASE_URL 覆盖（优先级最高）
    DATABASE_URL: str | None = Field(default=None)

    SECRET_KEY: str = Field(default="")
    INITIAL_BALANCE: float = 100.0
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60   # 1 小时
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7      # 7 天

    # CORS（生产环境从 .env 读取）
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://127.0.0.1:5174"

    # 管理后台
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD_HASH: str = ""  # bcrypt hash，从 .env 读取
    ADMIN_SECRET_KEY: str = Field(default="")

    # Casdoor SSO
    CASDOOR_ENDPOINT: str = ""            # e.g. https://auth.example.com
    CASDOOR_CLIENT_ID: str = ""
    CASDOOR_CLIENT_SECRET: str = ""
    CASDOOR_CERTIFICATE: str = ""         # Casdoor 应用的 JWT 公钥证书（PEM 字符串）
    CASDOOR_ORG_NAME: str = ""
    CASDOOR_APP_NAME: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() == "production"

    @model_validator(mode="after")
    def _fill_secrets(self):
        _prod = self.APP_ENV.lower() == "production"
        if not self.SECRET_KEY:
            if _prod:
                raise ValueError(
                    "SECRET_KEY 未配置！生产环境必须在 .env 中显式设置 SECRET_KEY（至少 32 字符）。"
                )
            self.SECRET_KEY = secrets.token_urlsafe(48)
            logger.warning("SECRET_KEY 未配置，已自动生成随机值（仅限开发环境）。")
        if len(self.SECRET_KEY) < 32:
            if _prod:
                raise ValueError("SECRET_KEY 长度不足 32 字符，不满足生产环境安全要求！")
            logger.warning("SECRET_KEY 长度不足 32 字符，安全性不足！")
        # ADMIN_SECRET_KEY 同理
        if not self.ADMIN_SECRET_KEY:
            if _prod:
                raise ValueError(
                    "ADMIN_SECRET_KEY 未配置！生产环境必须在 .env 中显式设置。"
                )
            self.ADMIN_SECRET_KEY = secrets.token_urlsafe(32)
        # Casdoor 配置完整性校验
        casdoor_fields = [
            self.CASDOOR_ENDPOINT, self.CASDOOR_CLIENT_ID,
            self.CASDOOR_CLIENT_SECRET, self.CASDOOR_CERTIFICATE,
            self.CASDOOR_ORG_NAME, self.CASDOOR_APP_NAME,
        ]
        has_any = any(casdoor_fields)
        has_all = all(casdoor_fields)
        if has_any and not has_all:
            missing = [
                name for name, val in [
                    ("CASDOOR_ENDPOINT", self.CASDOOR_ENDPOINT),
                    ("CASDOOR_CLIENT_ID", self.CASDOOR_CLIENT_ID),
                    ("CASDOOR_CLIENT_SECRET", self.CASDOOR_CLIENT_SECRET),
                    ("CASDOOR_CERTIFICATE", self.CASDOOR_CERTIFICATE),
                    ("CASDOOR_ORG_NAME", self.CASDOOR_ORG_NAME),
                    ("CASDOOR_APP_NAME", self.CASDOOR_APP_NAME),
                ] if not val
            ]
            raise ValueError(f"Casdoor 配置不完整，缺少: {', '.join(missing)}")
        if _prod and not has_all:
            raise ValueError("生产环境必须完整配置 Casdoor SSO（所有 CASDOOR_* 字段均需填写）。")
        if not has_any:
            logger.warning("Casdoor SSO 未配置，OAuth 登录不可用。")
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    def build_db_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL

        if self.DB_BACKEND.lower() == "sqlite":
            db_path = Path(self.SQLITE_PATH).expanduser()
            if not db_path.is_absolute():
                project_root = Path(__file__).resolve().parents[2]
                db_path = (project_root / db_path).resolve()

            db_path.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite+aiosqlite:///{db_path.as_posix()}"

        pwd = quote_plus(self.MYSQL_PASSWORD)
        # SQLAlchemy Async URL:
        # mysql+asyncmy://user:pass@host:port/db?charset=utf8mb4
        return (
            f"mysql+{self.MYSQL_DRIVER}://{self.MYSQL_USER}:{pwd}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DB}"
            f"?charset=utf8mb4"
        )


settings = Settings()
