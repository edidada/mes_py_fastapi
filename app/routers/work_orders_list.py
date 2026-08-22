"""``GET /api/v1/work-orders`` 工单进度摘要列表。

不要求认证角色，不分页读取（page=1、pageSize=50 固定），响应为双层信封：
工单数组位于 ``data.data``，分页信息位于 ``data.meta``，通用元数据位于最外层 ``meta``。
"""

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from app.contract import correlation_id, list_envelope, ok
from app.deps import DbSession
from app.errors import MESError

router = APIRouter()


@router.get("", status_code=200)
async def list_work_orders(
    request: Request,
    db: DbSession,
    status: str | None = Query(default=None),
    plantCode: str | None = Query(default=None),
    materialCode: str | None = Query(default=None),
) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))

    sql = (
        "SELECT work_order_number, plant_code, material_code, status, priority, "
        "planned_quantity, completed_quantity, rejected_quantity, wip_quantity "
        "FROM v_work_order_progress"
    )
    conds: list[str] = []
    params: dict[str, str] = {}
    if status is not None:
        conds.append("status = :status")
        params["status"] = status
    if plantCode is not None:
        conds.append("plant_code = :plantCode")
        params["plantCode"] = plantCode
    if materialCode is not None:
        conds.append("material_code = :materialCode")
        params["materialCode"] = materialCode
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY priority DESC, work_order_number ASC"

    try:
        rows = await db.execute(text(sql), params)
    except Exception as exc:  # noqa: BLE001 - 统一映射为数据库错误
        raise MESError(500, "DATABASE_ERROR", "database error") from exc

    items = []
    for r in rows.fetchall():
        planned = r[5] if r[5] is not None else 0
        completed = r[6] if r[6] is not None else 0
        rejected = r[7] if r[7] is not None else 0
        completion_percent = 0.0
        if planned > 0:
            completion_percent = round(100.0 * completed / planned, 2)
        items.append(
            {
                "workOrderNumber": r[0],
                "plantCode": r[1],
                "materialCode": r[2],
                "status": r[3],
                "priority": int(r[4]) if r[4] is not None else 0,
                "plannedQuantity": int(planned),
                "completedQuantity": int(completed),
                "rejectedQuantity": int(rejected),
                "completionPercent": completion_percent,
                "wipQuantity": int(r[8] if r[8] is not None else 0),
            }
        )
    return ok(list_envelope(items, corr, page=1, page_size=50), corr)
