"""Wireup 容器装配。"""

import wireup

from app import services
from app.config import Settings


def create_container(settings: Settings) -> wireup.AsyncContainer:
    """按配置创建异步注入容器。"""
    return wireup.create_async_container(
        services=[
            services.build_engine,
            services.DatabaseHealthService,
        ],
        parameters=settings.model_dump(),
    )
