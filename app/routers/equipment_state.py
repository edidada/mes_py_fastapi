"""``GET /api/v1/equipment/{equipmentCode}/state`` 查询设备当前状态。

不做角色/认证校验，不写数据库。设备不存在时返回 ``404 NOT_FOUND``，
与 C++ ``queryOne`` 一致，数据库查询错误也映射为同一个 ``404``。
"""

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.contract import correlation_id, ok
from app.deps import DbSession
from app.errors import MESError

router = APIRouter()


@router.get("/{equipmentCode}/state", status_code=200)
async def get_equipment_state(request: Request, equipmentCode: str, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    try:
        row = (
            await db.execute(
                text(
                    "SELECT equipment_code, plant_code, status, last_heartbeat_at, "
                    "       heartbeat_age_seconds, open_maintenance_count "
                    "FROM v_equipment_current_state WHERE equipment_code = :c"
                ),
                {"c": equipmentCode},
            )
        ).first()
    except Exception:
        raise MESError(404, "NOT_FOUND", "equipment not found") from None
    if row is None:
        raise MESError(404, "NOT_FOUND", "equipment not found")

    return ok(
        {
            "equipmentCode": row[0],
            "plantCode": row[1],
            "status": row[2],
            "lastHeartbeatAt": row[3] if row[3] is not None else "",
            "heartbeatAgeSeconds": row[4],
            "openMaintenanceCount": row[5],
        },
        corr,
    )
