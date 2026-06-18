"""
数据库配置模块
使用 Builder 模式构建数据库配置；连接信息优先从环境变量读取。
"""
import os
from typing import Optional


def _env(name: str, default: str) -> str:
    return (os.environ.get(name) or default).strip()


class DatabaseConfigBuilder:
    """数据库配置构建器，使用 Builder 模式"""

    def __init__(self):
        self._host = _env("DB_HOST", "localhost")
        self._port = int(_env("DB_PORT", "5432"))
        self._database = _env("DB_NAME", "recruitment")
        self._user = _env("DB_USER", "postgres")
        self._password = _env("DB_PASSWORD", "changeme")
        self._pool_size = 10
        self._max_overflow = 20
        self._pool_timeout = 30
        self._pool_recycle = 3600

    def host(self, host: str) -> "DatabaseConfigBuilder":
        self._host = host
        return self

    def port(self, port: int) -> "DatabaseConfigBuilder":
        self._port = port
        return self

    def database(self, database: str) -> "DatabaseConfigBuilder":
        self._database = database
        return self

    def user(self, user: str) -> "DatabaseConfigBuilder":
        self._user = user
        return self

    def password(self, password: str) -> "DatabaseConfigBuilder":
        self._password = password
        return self

    def pool_size(self, size: int) -> "DatabaseConfigBuilder":
        self._pool_size = size
        return self

    def max_overflow(self, overflow: int) -> "DatabaseConfigBuilder":
        self._max_overflow = overflow
        return self

    def build_connection_string(self) -> str:
        return f"postgresql://{self._user}:{self._password}@{self._host}:{self._port}/{self._database}"

    def build_async_connection_string(self) -> str:
        return (
            f"postgresql+asyncpg://{self._user}:{self._password}"
            f"@{self._host}:{self._port}/{self._database}"
        )

    @property
    def connection_params(self) -> dict:
        return {
            "host": self._host,
            "port": self._port,
            "database": self._database,
            "user": self._user,
            "password": self._password,
        }


db_config = DatabaseConfigBuilder()
