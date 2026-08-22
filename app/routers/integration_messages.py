"""``GET /api/v1/integration/messages`` 收件箱消息列表。

仅 ``MES_ADMIN`` 可访问（省略角色按 ``MES_OPERATOR``，返回 ``403 MES_ADMIN
required``）。可选 ``status`` 精确过滤——参数存在即启用（``?status=`` 匹配空
状态）。按 ``received_at`` 倒序最多 100 条；``total`` 是返回行数。响应保留
``listEnvelope`` + ``respondOk`` 的双层 ``data/meta`` 结构。
"""

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from app.auth import require_role
from app.contract import correlation_id, list_envelope, ok
from app.deps import DbSession
from app.errors import MESError

router = APIRouter()

_ALLOWED_ROLES = {"MES_ADMIN"}


@router.get("/messages", status_code=200)
async def list_inbox_messages(
    request: Request, db: DbSession, status: str | None = Query(default=None)
) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "MES_ADMIN required")

    sql = (
        "SELECT message_id, source_system, message_type, status, retry_count, "
        "       received_at, processed_at, error_message "
        "FROM integration_inbox_messages"
    )
    params: dict = {}
    if status is not None:
        sql += " WHERE status = :status"
        params["status"] = status
    sql += " ORDER BY received_at DESC LIMIT 100"

    try:
        rows = (await db.execute(text(sql), params)).fetchall()
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None

    items = [
        {
            "messageId": r[0],
            "sourceSystem": r[1],
            "messageType": r[2],
            "status": r[3],
            "retryCount": r[4],
            "receivedAt": r[5],
            "processedAt": r[6] if r[6] is not None else "",
            "errorMessage": r[7] if r[7] is not None else "",
        }
        for r in rows
    ]
    inner = list_envelope(items, corr, page=1, page_size=100)
    return ok(inner, corr)
