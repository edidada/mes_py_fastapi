"""FastAPI 应用入口。

路由与 Wireup 集成：路由处理器通过 ``Injected[T]`` 注入容器中的服务。
数据库会话通过 FastAPI 原生依赖（从容器解析会话工厂）按请求注入。
"""

from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

import wireup.integration.fastapi
from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from wireup import Injected

from app.config import Settings
from app.container import create_container
from app.contract import ok
from app.database import close_db_engine, init_db
from app.errors import register_error_handlers
from app.metrics import install_metrics
from app.services import DatabaseHealthService


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每次请求从容器解析会话工厂并创建一个会话。"""
    container = request.app.state.container
    factory: async_sessionmaker[AsyncSession] = await container.get(async_sessionmaker[AsyncSession])
    async with factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_session)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    container = create_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动阶段：连接池、建表、建视图、装种子；失败则服务不开始监听（fail-fast）
        engine = await container.get(AsyncEngine)
        await init_db(engine, reseed_on_start=settings.reseed_on_start)
        yield
        await close_db_engine(engine)

    app = FastAPI(
        title="MES HTTP API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.container = container
    app.state.settings = settings

    register_error_handlers(app)
    install_metrics(app)

    @app.middleware("http")
    async def set_correlation_id(request: Request, call_next):
        request.state.correlation_id = (
            request.headers.get("X-Correlation-Id") or "no-correlation"
        )
        response = await call_next(request)
        return response

    @app.get("/health")
    async def health(db: Injected[DatabaseHealthService]) -> dict:
        db_ok = await db.is_ready()
        return ok(
            {"status": "UP", "db": "OK" if db_ok else "DOWN", "version": "1.0.0"},
            "health",
        )

    @app.get("/ready")
    async def ready(db: Injected[DatabaseHealthService]) -> dict:
        return ok({"ready": await db.is_ready()}, "ready")

    # 路由注册完成后初始化 Wireup 集成
    wireup.integration.fastapi.setup(container, app)
    return app


app = create_app()
