"""``GET /api/v1/andons`` 安灯列表。

``status`` 查询参数省略时默认 ``ACTIVE``：只查 ``v_active_andons``（按严重级别
排序、无 LIMIT）。任何其他值（包括空字符串）读取 ``trace_andon_events`` 按
``raised_at`` 降序限 100 条。返回 C++ 双层 envelope。
"""

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from app.contract import correlation_id, list_envelope, ok
from app.deps import DbSession
from app.errors import MESError

router = APIRouter()

_ACTIVE_COLUMNS = (
    "andon_id, plant_code, severity, category, resource_type, resource_code, "
    "message, raised_at, acknowledged_at, acknowledged_by, related_work_order_number"
)


def _duration_sql(closed_expr: str) -> str:
    """C++ 语义：当前/关闭时间减提升时间（秒），NULL 转 0。"""
    return (
        f"CAST(COALESCE({closed_expr}, strftime('%s','now')) "
        f"- strftime('%s', raised_at) AS INTEGER)"
    )


@router.get("", status_code=200)
async def list_andons(
    request: Request, db: DbSession, status: str | None = Query(default=None)
) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    mode = "ACTIVE" if status is None or status == "ACTIVE" else "ALL"

    try:
        if mode == "ACTIVE":
            sql = (
                "SELECT "
                + _ACTIVE_COLUMNS
                + ", "
                + _duration_sql("NULL")
                + " FROM v_active_andons"
            )
            result = await db.execute(text(sql))
        else:
            sql = (
                "SELECT "
                + _ACTIVE_COLUMNS
                + ", "
                + _duration_sql("strftime('%s', closed_at)")
                + " FROM trace_andon_events ORDER BY raised_at DESC LIMIT 100"
            )
            result = await db.execute(text(sql))
        rows = result.fetchall()
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None

    items = [
        {
            "andonId": r[0],
            "plantCode": r[1],
            "severity": r[2],
            "category": r[3],
            "resourceType": r[4] if r[4] is not None else "",
            "resourceCode": r[5] if r[5] is not None else "",
            "message": r[6],
            "raisedAt": r[7],
            "acknowledgedAt": r[8] if r[8] is not None else "",
            "acknowledgedBy": r[9] if r[9] is not None else "",
            "durationSeconds": r[11] if r[11] is not None else 0,
            "relatedWorkOrderNumber": r[10] if r[10] is not None else "",
        }
        for r in rows
    ]
    inner = list_envelope(items, corr, page=1, page_size=100)
    return ok(inner, corr)
