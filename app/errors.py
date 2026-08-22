"""MES 业务错误与 FastAPI 异常处理器。

错误响应结构（与 C++ 一致）：
``{"error": {"code": str, "message": str}, "meta": {"correlationId": str}}``
错误 ``meta`` 不含 ``generatedAt``。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class MESError(Exception):
    """携带 HTTP 状态码与错误码/消息的业务异常。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(MESError)
    async def mes_error_handler(request: Request, exc: MESError):
        correlation_id = request.state.correlation_id
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {"code": exc.code, "message": exc.message},
                "meta": {"correlationId": correlation_id},
            },
        )
