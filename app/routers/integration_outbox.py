"""``GET /api/v1/integration/outbox`` 出站消息列表。

仅 ``MES_ADMIN`` 可访问。可选 ``status`` 精确过滤（参数存在即启用）。消息表
LEFT JOIN 投递尝试与死信表按 ``outbox_id`` 聚合：``retryCount`` 用尝试表行数
（非死信表 ``retry_count``），``lastAttemptAt`` 用 ``MAX(attempted_at)``。按
``created_at`` 倒序最多 100 条，双层 ``data/meta`` 信封。
"""

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from app.auth import require_role
from app.contract import correlation_id, list_envelope, ok
from app.deps import DbSession
from app.errors import MESError

router = APIRouter()

_ALLOWED_ROLES = {"MES_ADMIN"}


@router.get("/outbox", status_code=200)
async def list_outbox_messages(
    request: Request, db: DbSession, status: str | None = Query(default=None)
) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "MES_ADMIN required")

    sql = (
        "SELECT m.outbox_id, m.aggregate_type, m.aggregate_id, m.event_type, "
        "       m.target_system, m.status, m.created_at, m.published_at, "
        "       COUNT(a.attempt_id) AS retry_count, MAX(a.attempted_at) AS last_attempt_at, "
        "       d.last_error, d.failed_at, d.replayed_at "
        "FROM integration_outbox_messages m "
        "LEFT JOIN integration_outbox_delivery_attempts a ON a.outbox_id = m.outbox_id "
        "LEFT JOIN integration_outbox_dead_letters d ON d.outbox_id = m.outbox_id"
    )
    params: dict = {}
    if status is not None:
        sql += " WHERE m.status = :status"
        params["status"] = status
    sql += " GROUP BY m.outbox_id ORDER BY m.created_at DESC LIMIT 100"

    try:
        rows = (await db.execute(text(sql), params)).fetchall()
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None

    items = [
        {
            "outboxId": r[0],
            "aggregateType": r[1],
            "aggregateId": r[2],
            "eventType": r[3],
            "targetSystem": r[4],
            "status": r[5],
            "retryCount": r[8],
            "createdAt": r[6],
            "publishedAt": r[7] if r[7] is not None else "",
            "lastAttemptAt": r[9] if r[9] is not None else "",
            "lastError": r[10] if r[10] is not None else "",
            "failedAt": r[11] if r[11] is not None else "",
            "replayedAt": r[12] if r[12] is not None else "",
        }
        for r in rows
    ]
    inner = list_envelope(items, corr, page=1, page_size=100)
    return ok(inner, corr)
