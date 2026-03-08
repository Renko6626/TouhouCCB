# app/core/config.py

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from urllib.parse import quote_plus


class Settings(BaseSettings):
    PROJECT_NAME: str = "东方炒炒币：饭纲丸龙的野望"

    # ✅ 推荐：用拆分字段，更不容易在密码/特殊字符上翻车
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

    SECRET_KEY: str = "GENSOKYO_SECRET_REIMU_IS_POOR_999"
    INITIAL_BALANCE: float = 100.0
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def build_db_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL

        pwd = quote_plus(self.MYSQL_PASSWORD)
        # SQLAlchemy Async URL:
        # mysql+asyncmy://user:pass@host:port/db?charset=utf8mb4
        return (
            f"mysql+{self.MYSQL_DRIVER}://{self.MYSQL_USER}:{pwd}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DB}"
            f"?charset=utf8mb4"
        )


settings = Settings()
