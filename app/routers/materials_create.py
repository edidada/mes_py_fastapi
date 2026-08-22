"""``POST /api/v1/master/materials`` 创建物料主数据。"""

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


def _to_bool(value) -> bool:
    """C++ 布尔转换：boolean 原值；string 仅 true/TRUE；number 非零；其他 false。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value in ("true", "TRUE")
    if isinstance(value, (int, float)):
        return value != 0
    return False


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("", status_code=200)
async def create_material(request: Request, db: DbSession) -> dict:
    require_role(request, _ADMIN, "forbidden")

    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok(
            {"materialCode": cached["resource_code"], "cached": True},
            correlation_id(request.headers.get("X-Correlation-Id")),
        )

    body = await _parse_body(request)
    material_code = body.get("materialCode")
    if not isinstance(material_code, str) or not material_code.strip():
        raise MESError(422, "VALIDATION_ERROR", "invalid materialCode")
    material_name = (
        body.get("materialName") if isinstance(body.get("materialName"), str) else ""
    )
    unit_code = body.get("unitCode")
    if not isinstance(unit_code, str) or not unit_code.strip():
        unit_code = "EA"
    lot_controlled = 1 if _to_bool(body.get("lotControlled")) else 0
    serial_controlled = 1 if _to_bool(body.get("serialControlled")) else 0

    try:
        await db.execute(
            text(
                "INSERT INTO master_materials "
                "(material_code, material_name, unit_code, lot_controlled, serial_controlled) "
                "VALUES (:code, :name, :unit, :lot, :serial)"
            ),
            {
                "code": material_code,
                "name": material_name,
                "unit": unit_code,
                "lot": lot_controlled,
                "serial": serial_controlled,
            },
        )
        await write_audit(
            db,
            action="MATERIAL_CREATE",
            resource_type="MATERIAL",
            resource_id=material_code,
            before={},
            after=body,
            actor_id=get_actor(request),
        )
        await save_idempotent(db, idem_key, "200", material_code, "material created")
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise MESError(409, "CONFLICT", str(exc.orig)) from exc

    return ok(
        {"materialCode": material_code, "materialName": material_name},
        correlation_id(request.headers.get("X-Correlation-Id")),
    )
