"""应用配置：从环境变量（MES_ 前缀）/ .env / 默认值加载。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///./mes.db"
    reseed_on_start: bool = True
