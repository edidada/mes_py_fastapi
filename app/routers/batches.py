"""批次查询与状态类端点。

实现文档中的：
- ``GET /api/v1/batches`` 批次列表（2535）
- ``GET /api/v1/batches/{id}`` 批次详情（2545）

列表支持可选 ``status`` 过滤；详情返回 ``production_batches`` 整行，
字段名映射遵循文档契约（``currentQty`` → ``actual_quantity``，
``targetQty`` → ``planned_quantity``，``qaStatus`` → ``quality_disposition``，
``materialCode`` → ``product_material_code``，``uom`` → ``unit_code``）。
状态码遵循 C++ 固定枚举（上限见 ``_STATUS_CAP``）。
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


def _row_to_dict(r) -> dict:
    return {
        "plantCode": r[0],
        "batchNumber": r[1],
        "workOrderNumber": None,
        "materialCode": r[2],
        "status": _cap_status(r[3]),
        "currentQty": r[4],
        "targetQty": r[5],
        "qaStatus": r[6],
        "uom": r[7],
        "createdAt": r[8],
        "updatedAt": r[9],
    }


_SQL_COLS = (
    "plant_code, batch_number, product_material_code, status, "
    "actual_quantity, planned_quantity, quality_disposition, unit_code, "
    "created_at, updated_at"
)


@router.get("/batches", status_code=200)
async def list_batches(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    status = request.query_params.get("status")
    params: dict = {}
    sql = f"SELECT {_SQL_COLS} FROM production_batches"
    if status:
        sql += " WHERE status = :st"
        params["st"] = status
    sql += " ORDER BY updated_at DESC"
    rows = (await db.execute(text(sql), params)).fetchall()
    items = [_row_to_dict(r) for r in rows]
    return ok({"batches": items, "total": len(items)}, corr)


@router.get("/batches/{batch_id}", status_code=200)
async def get_batch(batch_id: str, request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    row = (
        await db.execute(
            text(f"SELECT {_SQL_COLS} FROM production_batches WHERE batch_number = :bid"),
            {"bid": batch_id},
        )
    ).first()
    if row is None:
        raise MESError(404, "NOT_FOUND", "batch not found")
    return ok(_row_to_dict(row), corr)
