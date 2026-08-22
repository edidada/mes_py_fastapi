"""Prometheus 文本度量中间件。

每个应用 Router 持有独立原子计数器：
- ``mes_http_requests_total``：已完成响应的请求总数。
- ``mes_http_client_errors_total``：其中 4xx 数量。
- ``mes_http_server_errors_total``：其中 5xx 数量（不重复计入 4xx）。
中间件在处理器返回后更新计数，因此 ``/metrics`` 响应不包含自身请求。
"""

import asyncio

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


class MetricsStore:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.requests_total = 0
        self.client_errors_total = 0
        self.server_errors_total = 0

    async def record(self, status_code: int) -> None:
        async with self.lock:
            self.requests_total += 1
            if 400 <= status_code < 500:
                self.client_errors_total += 1
            elif status_code >= 500:
                self.server_errors_total += 1

    def render(self) -> str:
        return (
            "# TYPE mes_http_requests_total counter\n"
            f"mes_http_requests_total {self.requests_total}\n"
            "# TYPE mes_http_client_errors_total counter\n"
            f"mes_http_client_errors_total {self.client_errors_total}\n"
            "# TYPE mes_http_server_errors_total counter\n"
            f"mes_http_server_errors_total {self.server_errors_total}\n"
        )


class MetricsMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, store: MetricsStore) -> None:
        super().__init__(app)
        self._store = store

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        await self._store.record(response.status_code)
        return response


def install_metrics(app: FastAPI) -> MetricsStore:
    store = MetricsStore()
    app.add_middleware(MetricsMiddleware, store=store)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(
            content=store.render(),
            media_type="text/plain; version=0.0.4",
        )

    return store
