"""SQLAlchemy 异步引擎与连接池管理。"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


async def create_db_engine(database_url: str) -> AsyncEngine:
    """创建异步 SQLite 连接池并验证初始连接。

    若无法建立数据库连接则抛出异常，应用启动失败、HTTP 服务不开始监听，
    与 C++ 服务启动时数据库打开失败即退出的行为一致。
    """
    engine = create_async_engine(database_url)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return engine


async def close_db_engine(engine: AsyncEngine) -> None:
    """关闭连接池。"""
    await engine.dispose()
