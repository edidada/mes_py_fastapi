"""FastAPI 应用入口。

路由与 Wireup 集成：路由处理器通过 ``Injected[T]`` 注入容器中的服务。
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import wireup.integration.fastapi
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from wireup import Injected

from app.config import Settings
from app.container import create_container
from app.database import close_db_engine
from app.services import DatabaseHealthService


def _utc_now() -> str:
    """UTC RFC 3339，精度到秒。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    container = create_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动阶段验证数据库连接，失败则服务不开始监听（fail-fast）
        await container.get(AsyncEngine)
        yield
        engine = await container.get(AsyncEngine)
        await close_db_engine(engine)

    app = FastAPI(
        title="MES HTTP API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.container = container
    app.state.settings = settings

    @app.get("/health")
    async def health(db: Injected[DatabaseHealthService]) -> dict:
        db_ok = await db.is_ready()
        return {
            "data": {
                "status": "UP",
                "db": "OK" if db_ok else "DOWN",
                "version": "1.0.0",
            },
            "meta": {
                "correlationId": "health",
                "generatedAt": _utc_now(),
            },
        }

    # 路由注册完成后初始化 Wireup 集成
    wireup.integration.fastapi.setup(container, app)
    return app


app = create_app()
