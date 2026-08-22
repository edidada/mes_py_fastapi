"""``POST /api/v1/work-orders`` 显式创建工单。

从指定的 ``EFFECTIVE`` 路线创建 ``DRAFT`` 工单，复制路线工序快照。
``PLANNER``、``MES_SUPERVISOR``、``MES_ADMIN`` 可调用；角色检查先于幂等查询。
"""

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

_ALLOWED_ROLES = {"PLANNER", "MES_SUPERVISOR", "MES_ADMIN"}


def _to_int(value) -> int:
    """转换为整数：小数截断、整数字符串可转换，无效值默认 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _to_str(value) -> str:
    return value if isinstance(value, str) else ""


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("", status_code=200)
async def create_work_order(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "PLANNER required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"workOrderNumber": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    work_order_number = body.get("workOrderNumber")
    if not isinstance(work_order_number, str) or not work_order_number.strip():
        raise MESError(422, "VALIDATION_ERROR", "workOrderNumber required")

    plant_code = _to_str(body.get("plantCode"))
    if not plant_code:
        plant_code = "PLANT-A"
    material_code = _to_str(body.get("materialCode"))
    routing_code = _to_str(body.get("routingCode"))
    routing_version = _to_str(body.get("routingVersion"))
    bom_code = _to_str(body.get("bomCode"))
    bom_version = _to_str(body.get("bomVersion"))
    priority = _to_int(body.get("priority"))
    planned_quantity = _to_int(body.get("plannedQuantity"))
    planned_start_at = _to_str(body.get("plannedStartAt"))
    planned_end_at = _to_str(body.get("plannedEndAt"))

    if not routing_code or not routing_version:
        raise MESError(422, "VALIDATION_ERROR", "routing code and version required")

    # 工厂必须存在（省略时默认 PLANT-A）
    if not (
        await db.execute(text("SELECT 1 FROM master_plants WHERE plant_code = :c"), {"c": plant_code})
    ).first():
        raise MESError(409, "CONFLICT", "plant not found")

    # 物料必须存在（C++ 依赖外键约束失败 → 409）
    if not (
        await db.execute(text("SELECT 1 FROM master_materials WHERE material_code = :c"), {"c": material_code})
    ).first():
        raise MESError(409, "CONFLICT", "material not found")

    # 计划数量必须为正（C++ 由数据库约束 → 409）
    if planned_quantity <= 0:
        raise MESError(409, "CONFLICT", "CHECK constraint failed: planned_quantity > 0")

    # 路线必须精确匹配且为 EFFECTIVE
    routing = (
        await db.execute(
            text(
                "SELECT 1 FROM master_routings "
                "WHERE routing_code = :rc AND version = :rv AND status = 'EFFECTIVE'"
            ),
            {"rc": routing_code, "rv": routing_version},
        )
    ).first()
    if routing is None:
        raise MESError(422, "VALIDATION_ERROR", "routing version not found or not effective")

    try:
        await db.execute(
            text(
                "INSERT INTO production_work_orders "
                "(work_order_number, plant_code, material_code, routing_code, routing_version, "
                "bom_code, bom_version, priority, status, planned_quantity, "
                "planned_start_at, planned_end_at) "
                "VALUES (:wo, :plant, :mat, :rc, :rv, :bc, :bv, :pri, 'DRAFT', :qty, :start, :end)"
            ),
            {
                "wo": work_order_number,
                "plant": plant_code,
                "mat": material_code,
                "rc": routing_code,
                "rv": routing_version,
                "bc": bom_code,
                "bv": bom_version,
                "pri": priority,
                "qty": planned_quantity,
                "start": planned_start_at,
                "end": planned_end_at,
            },
        )

        # 复制路线工序快照（单条失败被忽略，不终止事务）
        ops = await db.execute(
            text(
                "SELECT sequence, operation_code, work_center_code, quality_gate, "
                "allow_skip, standard_cycle_seconds "
                "FROM master_routing_operations "
                "WHERE routing_code = :rc AND version = :rv ORDER BY sequence"
            ),
            {"rc": routing_code, "rv": routing_version},
        )
        for op in ops.fetchall():
            try:
                await db.execute(
                    text(
                        "INSERT INTO production_work_order_operations "
                        "(work_order_number, sequence, operation_code, work_center_code, "
                        "quality_gate, allow_skip, standard_cycle_seconds, status) "
                        "VALUES (:wo, :seq, :op, :wc, :qg, :skip, :std, 'PENDING')"
                    ),
                    {
                        "wo": work_order_number,
                        "seq": op[0],
                        "op": op[1],
                        "wc": op[2],
                        "qg": op[3],
                        "skip": op[4],
                        "std": op[5],
                    },
                )
            except IntegrityError:
                pass

        await write_audit(
            db,
            action="WORK_ORDER_CREATE",
            resource_type="WORK_ORDER",
            resource_id=work_order_number,
            before={},
            after=body,
            actor_id=get_actor(request),
        )
        await save_idempotent(db, idem_key, "200", work_order_number, "work order created")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        # 消息保留 SQLite 错误文案
        raise MESError(409, "CONFLICT", str(exc.orig)) from exc

    return ok({"workOrderNumber": work_order_number, "status": "DRAFT"}, corr)
