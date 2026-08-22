"""``GET /api/v1/material/allocations/suggest`` FIFO 可用库存建议。

只给建议、不写预留、不校验角色。``materialCode`` 必须出现（缺失返回
``422 materialCode required``）；显式空字符串视为已提供，正常返回空数组。
``ORDER BY expiry_at`` 使 NULL 失效日期排最前。
"""

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from app.contract import correlation_id, ok
from app.deps import DbSession
from app.errors import MESError

router = APIRouter()


@router.get("/allocations/suggest", status_code=200)
async def suggest_allocations(
    request: Request, db: DbSession, materialCode: str | None = Query(default=None)
) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    if materialCode is None:
        raise MESError(422, "VALIDATION_ERROR", "materialCode required")

    try:
        rows = (
            await db.execute(
                text(
                    "SELECT lot_number, available_quantity, expiry_at, location_code "
                    "FROM v_available_inventory WHERE material_code = :mc "
                    "ORDER BY expiry_at"
                ),
                {"mc": materialCode},
            )
        ).fetchall()
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None

    data = [
        {
            "lotNumber": r[0] if r[0] is not None else "",
            "availableQuantity": r[1],
            "expiryAt": r[2] if r[2] is not None else "",
            "locationCode": r[3],
            "fifoRank": i + 1,
        }
        for i, r in enumerate(rows)
    ]
    return ok(data, corr)
