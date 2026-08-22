"""``POST /api/v1/master/plants`` 创建工厂主数据。"""

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
    """按 C++ 行为解析原始请求体；无效 JSON 抛 422 VALIDATION_ERROR。"""
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("", status_code=200)
async def create_plant(request: Request, db: DbSession) -> dict:
    require_role(request, _ADMIN, "forbidden")

    idem_key = await require_idempotency_key(request)
    body = await _parse_body(request)

    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok(
            {"plantCode": cached["resource_code"], "cached": True},
            correlation_id(request.headers.get("X-Correlation-Id")),
        )

    plant_code = body.get("plantCode")
    if not isinstance(plant_code, str) or not plant_code.strip():
        raise MESError(422, "VALIDATION_ERROR", "invalid plantCode")
    plant_name = body.get("plantName") if isinstance(body.get("plantName"), str) else ""
    timezone = body.get("timezone") if isinstance(body.get("timezone"), str) else ""

    try:
        await db.execute(
            text(
                "INSERT INTO master_plants (plant_code, plant_name, timezone, active) "
                "VALUES (:code, :name, :tz, 1)"
            ),
            {"code": plant_code, "name": plant_name, "tz": timezone},
        )
        await write_audit(
            db,
            action="PLANT_CREATE",
            resource_type="PLANT",
            resource_id=plant_code,
            before={},
            after=body,
            actor_id=get_actor(request),
        )
        await save_idempotent(db, idem_key, "200", plant_code, "plant created")
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise MESError(409, "CONFLICT", str(exc.orig)) from exc

    return ok(
        {"plantCode": plant_code, "plantName": plant_name, "active": True},
        correlation_id(request.headers.get("X-Correlation-Id")),
    )
