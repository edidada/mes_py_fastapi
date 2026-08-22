"""FastAPI 应用入口。

路由与 Wireup 集成：路由处理器通过 ``Injected[T]`` 注入容器中的服务。
数据库会话通过 FastAPI 原生依赖（从容器解析会话工厂）按请求注入。
"""

from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

import wireup.integration.fastapi
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from wireup import Injected

from app.config import Settings
from app.container import create_container
from app.contract import ok
from app.database import close_db_engine, init_db
from app.errors import register_error_handlers
from app.metrics import install_metrics
from app.routers import equipment_create, execution_context, executions_complete, executions_rework, executions_scrap, executions_start, materials_create, plants_create, plants_list, production_plans_import, routings_create, stations_sessions, work_orders_create, work_orders_dispatch, work_orders_get, work_orders_list, work_orders_state, workers_create
from app.services import DatabaseHealthService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    container = create_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动阶段：连接池、建表、建视图、装种子；失败则服务不开始监听（fail-fast）
        engine = await container.get(AsyncEngine)
        await init_db(engine, reseed_on_start=settings.reseed_on_start)
        app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
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

    app.include_router(plants_create.router, prefix="/api/v1/master/plants")
    app.include_router(plants_list.router, prefix="/api/v1/master/plants")
    app.include_router(materials_create.router, prefix="/api/v1/master/materials")
    app.include_router(routings_create.router, prefix="/api/v1/master/routings")
    app.include_router(equipment_create.router, prefix="/api/v1/master/equipment")
    app.include_router(workers_create.router, prefix="/api/v1/master/workers")
    app.include_router(stations_sessions.router, prefix="/api/v1/stations")
    app.include_router(execution_context.router, prefix="/api/v1/execution-context")
    app.include_router(executions_start.router, prefix="/api/v1/executions")
    app.include_router(executions_complete.router, prefix="/api/v1/executions")
    app.include_router(executions_rework.router, prefix="/api/v1/executions")
    app.include_router(executions_scrap.router, prefix="/api/v1/executions")
    app.include_router(production_plans_import.router, prefix="/api/v1/production-plans")
    app.include_router(work_orders_create.router, prefix="/api/v1/work-orders")
    app.include_router(work_orders_dispatch.router, prefix="/api/v1/work-orders")
    app.include_router(work_orders_get.router, prefix="/api/v1/work-orders")
    app.include_router(work_orders_state.router, prefix="/api/v1/work-orders")
    app.include_router(work_orders_list.router, prefix="/api/v1/work-orders")

    # 路由注册完成后初始化 Wireup 集成
    wireup.integration.fastapi.setup(container, app)
    return app


app = create_app()
