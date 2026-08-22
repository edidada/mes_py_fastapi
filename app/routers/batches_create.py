"""``POST /api/v1/batches`` 创建批次。

权限 ``PLANNER``/``MES_SUPERVISOR``/``PROCESS_ENGINEER``（不含 ``MES_ADMIN``），
先于幂等。依次校验配方存在→生效状态→生效期→设备存在→设备归属工厂。事务中
创建 ``DRAFT`` 批次，成品物料/单位从配方复制，实际数量默认 0；审计与幂等辅助
写失败不改变成功语义。
"""

import json

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.audit import write_audit
from app.auth import get_actor, require_role
from app.contract import correlation_id, ok, utc_now
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()

_ALLOWED_ROLES = {"PLANNER", "MES_SUPERVISOR", "PROCESS_ENGINEER"}


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


def _as_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("", status_code=200)
async def create_batch(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "PLANNER, MES_SUPERVISOR or PROCESS_ENGINEER required")
    idem_key = await require_idempotency_key(request)
    try:
        cached = await find_idempotent(db, idem_key)
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None
    if cached is not None:
        return ok({"batchNumber": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    batch_number = _as_str(body.get("batchNumber"))
    plant_code = _as_str(body.get("plantCode"))
    recipe_code = _as_str(body.get("recipeCode"))
    recipe_version = _as_str(body.get("recipeVersion"))
    equipment_code = _as_str(body.get("equipmentCode"))
    planned_quantity = _as_number(body.get("plannedQuantity"))
    operator = get_actor(request)

    if not batch_number or not plant_code or not recipe_code or not recipe_version or not equipment_code:
        raise MESError(422, "VALIDATION_ERROR", "batchNumber, plantCode, recipeCode, recipeVersion, equipmentCode required")
    if planned_quantity is None or planned_quantity <= 0:
        raise MESError(422, "VALIDATION_ERROR", "plannedQuantity must be positive number")

    # 1. 配方版本
    try:
        recipe = (
            await db.execute(
                text(
                    "SELECT product_material_code, unit_code, status, effective_from, effective_to "
                    "FROM master_recipes WHERE recipe_code = :rc AND version = :v"
                ),
                {"rc": recipe_code, "v": recipe_version},
            )
        ).first()
    except Exception:
        raise MESError(404, "NOT_FOUND", "recipe version not found") from None
    if recipe is None:
        raise MESError(404, "NOT_FOUND", "recipe version not found")
    if recipe[2] != "EFFECTIVE":
        raise MESError(409, "CONFLICT", "recipe version is not effective")
    now = utc_now()
    effective_from = recipe[3] if recipe[3] else ""
    effective_to = recipe[4] if recipe[4] else ""
    if now < effective_from or (effective_to and now >= effective_to):
        raise MESError(409, "CONFLICT", "recipe version is outside its effective period")

    # 2. 设备
    try:
        equip = (
            await db.execute(
                text("SELECT plant_code FROM asset_equipment WHERE equipment_code = :c"),
                {"c": equipment_code},
            )
        ).first()
    except Exception:
        raise MESError(404, "NOT_FOUND", "equipment not found") from None
    if equip is None:
        raise MESError(404, "NOT_FOUND", "equipment not found")
    if equip[0] != plant_code:
        raise MESError(422, "VALIDATION_ERROR", "equipment does not belong to plant")

    product_material_code = recipe[0]
    unit_code = recipe[1]

    try:
        await db.execute(
            text(
                "INSERT INTO production_batches "
                "(batch_number, plant_code, product_material_code, recipe_code, recipe_version, "
                " equipment_code, planned_quantity, actual_quantity, unit_code, status, "
                " quality_disposition, hold_reason, started_at, completed_at, version, updated_at) "
                "VALUES (:bn, :pc, :pmc, :rc, :rv, :ec, :pq, 0, :uc, 'DRAFT', "
                "        NULL, NULL, NULL, NULL, 0, :t)"
            ),
            {
                "bn": batch_number,
                "pc": plant_code,
                "pmc": product_material_code,
                "rc": recipe_code,
                "rv": recipe_version,
                "ec": equipment_code,
                "pq": planned_quantity,
                "uc": unit_code,
                "t": now,
            },
        )
        try:
            await write_audit(
                db,
                action="BATCH_CREATE",
                resource_type="BATCH",
                resource_id=batch_number,
                before={},
                after=body,
                actor_id=operator,
            )
        except Exception:
            pass
        try:
            await save_idempotent(db, idem_key, "200", batch_number, "batch created")
        except Exception:
            pass
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise MESError(409, "CONFLICT", "batch creation failed") from None

    return ok(
        {
            "batchNumber": batch_number,
            "status": "DRAFT",
            "recipeCode": recipe_code,
            "recipeVersion": recipe_version,
            "plannedQuantity": planned_quantity,
        },
        corr,
    )
