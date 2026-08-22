"""``POST /api/v1/maintenance-work-orders`` 创建设备维修工单。

C++ 处理器不做任何 Body 字段级校验：``equipmentCode`` 默认空字符串，空/未知设备
外键会使主记录 INSERT 失败，但接口仍返回成功；``maintenanceType`` 默认
``CORRECTIVE``，其他枚举外值会使 INSERT 失败。仅 ``EQUIPMENT_ENGINEER`` 或
``MES_ADMIN`` 可调用。
"""

import json

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.audit import write_audit
from app.auth import get_actor, require_role
from app.contract import correlation_id, gen_id, ok, utc_now
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()

_ALLOWED_ROLES = {"EQUIPMENT_ENGINEER", "MES_ADMIN"}
_ALLOWED_TYPES = {"CORRECTIVE", "PREVENTIVE", "PREDICTIVE"}


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("", status_code=200)
async def create_maintenance_work_order(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "EQUIPMENT_ENGINEER required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"maintenanceWorkOrderId": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    equipment_code = _as_str(body.get("equipmentCode"))
    maintenance_type = _as_str(body.get("maintenanceType")) or "CORRECTIVE"
    description = _as_str(body.get("description"))
    assignee_id = _as_str(body.get("assigneeId"))
    due_at = _as_str(body.get("dueAt"))
    operator = get_actor(request)

    mwo_id = gen_id("MWO")
    try:
        # 1. 维修单主记录：非法维护类型（C++ 的 CHECK 约束）或空/未知设备外键 → INSERT 失败，忽略
        if maintenance_type in _ALLOWED_TYPES:
            try:
                await db.execute(
                    text(
                        "INSERT INTO asset_maintenance_work_orders "
                        "(maintenance_work_order_id, equipment_code, maintenance_type, description, "
                        " assignee_id, status, due_at, completed_at) "
                        "VALUES (:id, :c, :mt, :d, :a, 'OPEN', :due, NULL)"
                    ),
                    {
                        "id": mwo_id,
                        "c": equipment_code,
                        "mt": maintenance_type,
                        "d": description,
                        "a": assignee_id,
                        "due": due_at,
                    },
                )
            except Exception:
                pass
        # 2. 审计 + 幂等
        await write_audit(
            db,
            action="MAINTENANCE_CREATE",
            resource_type="MAINTENANCE_WO",
            resource_id=mwo_id,
            before={},
            after=body,
            actor_id=operator,
        )
        await save_idempotent(db, idem_key, "200", mwo_id, "created")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok({"maintenanceWorkOrderId": mwo_id, "status": "OPEN"}, corr)
