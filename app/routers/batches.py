"""批次查询与状态类端点。

实现文档中的：
- ``GET /api/v1/batches`` 批次列表（2535）
- ``GET /api/v1/batches/{id}`` 批次详情（2545）

列表按 ``v_batch_progress`` 视图返回，支持可选 ``status`` 过滤；
详情返回该视图的整行，状态码遵循 C++ 固定枚举（上限见 ``_STATUS_CAP``）。
"""

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.contract import correlation_id, ok
from app.deps import DbSession
from app.errors import MESError

router = APIRouter()

# production_batches.status 固定枚举上限，与 C++ 一致截断超长值
_STATUS_CAP = (
    "CREATED",
    "RELEASED",
    "IN_PROGRESS",
    "WAITING_QA",
    "QA_PASSED",
    "QA_FAILED",
    "COMPLETED",
    "CLOSED",
    "CANCELLED",
)


def _cap_status(value: str | None) -> str | None:
    if value is None:
        return None
    return value if value in _STATUS_CAP else _STATUS_CAP[-1]


@router.get("/batches", status_code=200)
async def list_batches(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    status = request.query_params.get("status")
    params: dict = {}
    sql = (
        "SELECT plant_code, batch_number, work_order_number, material_code, "
        "status, current_qty, target_qty, qa_status, uom, created_at, updated_at "
        "FROM v_batch_progress"
    )
    if status:
        sql += " WHERE status = :st"
        params["st"] = status
    sql += " ORDER BY updated_at DESC"
    rows = (await db.execute(text(sql), params)).fetchall()
    items = [
        {
            "plantCode": r[0],
            "batchNumber": r[1],
            "workOrderNumber": r[2],
            "materialCode": r[3],
            "status": _cap_status(r[4]),
            "currentQty": r[5],
            "targetQty": r[6],
            "qaStatus": r[7],
            "uom": r[8],
            "createdAt": r[9],
            "updatedAt": r[10],
        }
        for r in rows
    ]
    return ok({"batches": items, "total": len(items)}, corr)


@router.get("/batches/{batch_id}", status_code=200)
async def get_batch(batch_id: str, request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    row = (
        await db.execute(
            text(
                "SELECT plant_code, batch_number, work_order_number, material_code, "
                "status, current_qty, target_qty, qa_status, uom, created_at, updated_at "
                "FROM v_batch_progress WHERE batch_number = :bid"
            ),
            {"bid": batch_id},
        )
    ).first()
    if row is None:
        raise MESError(404, "NOT_FOUND", "batch not found")
    return ok(
        {
            "plantCode": row[0],
            "batchNumber": row[1],
            "workOrderNumber": row[2],
            "materialCode": row[3],
            "status": _cap_status(row[4]),
            "currentQty": row[5],
            "targetQty": row[6],
            "qaStatus": row[7],
            "uom": row[8],
            "createdAt": row[9],
            "updatedAt": row[10],
        },
        corr,
    )
