"""``POST /api/v1/master/equipment`` 创建设备主数据。"""

import json

from fastapi import APIRouter, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.audit import write_audit
from app.auth import get_actor, require_role
from app.contract import correlation_id, ok
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()

_ADMIN = {"MES_ADMIN"}


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("", status_code=200)
async def create_equipment(request: Request, db: DbSession) -> dict:
    require_role(request, _ADMIN, "forbidden")

    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok(
            {"equipmentCode": cached["resource_code"], "cached": True},
            correlation_id(request.headers.get("X-Correlation-Id")),
        )

    body = await _parse_body(request)
    equipment_code = body.get("equipmentCode")
    if not isinstance(equipment_code, str) or not equipment_code.strip():
        raise MESError(422, "VALIDATION_ERROR", "invalid equipmentCode")

    plant_code = body.get("plantCode")
    if not isinstance(plant_code, str) or not plant_code.strip():
        plant_code = "PLANT-A"
    work_center = body.get("workCenterCode")
    if not isinstance(work_center, str):
        work_center = ""
    equipment_name = (
        body.get("equipmentName") if isinstance(body.get("equipmentName"), str) else ""
    )
    criticality = body.get("criticality")
    if not isinstance(criticality, str) or not criticality.strip():
        criticality = "NORMAL"

    try:
        await db.execute(
            text(
                "INSERT INTO asset_equipment "
                "(equipment_code, plant_code, work_center_code, equipment_name, "
                "criticality, current_status, last_heartbeat_at) "
                "VALUES (:code, :plant, :wc, :name, :crit, 'OFFLINE', NULL)"
            ),
            {
                "code": equipment_code,
                "plant": plant_code,
                "wc": work_center,
                "name": equipment_name,
                "crit": criticality,
            },
        )
        await write_audit(
            db,
            action="EQUIPMENT_CREATE",
            resource_type="EQUIPMENT",
            resource_id=equipment_code,
            before={},
            after=body,
            actor_id=get_actor(request),
        )
        await save_idempotent(db, idem_key, "200", equipment_code, "equipment created")
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise MESError(409, "CONFLICT", str(exc.orig)) from exc

    return ok(
        {"equipmentCode": equipment_code, "currentStatus": "OFFLINE"},
        correlation_id(request.headers.get("X-Correlation-Id")),
    )
