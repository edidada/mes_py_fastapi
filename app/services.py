"""Wireup 管理的服务与资源工厂。"""

from typing import Annotated

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from wireup import Inject, service

from app.database import create_db_engine

# 注入名为 database_url 的容器配置参数
DatabaseUrl = Annotated[str, Inject(param="database_url")]


@service
async def build_engine(database_url: DatabaseUrl) -> AsyncEngine:
    """创建数据库连接池（单例）。"""
    return await create_db_engine(database_url)


@service
class DatabaseHealthService:
    """报告数据库连接池健康状态。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def is_ready(self) -> bool:
        """连接池可用返回 True，否则返回 False。"""
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
