"""``POST /api/v1/material/consumptions`` 按序列号记录物料消耗。

不校验角色。幂等命中发生在 JSON 与 Body 字段校验之前。库存校验先于事务：按
``material_code + lot_number`` 查询一条余额，无记录或在手量小于 quantity 时返回
``409 insufficient on-hand``。事务内各 SQL 失败被忽略，可能部分写入（例如负
quantity 通过库存比较，但消耗主记录因 ``quantity > 0`` 约束不落库）。
"""

import json

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.audit import write_audit
from app.auth import get_actor
from app.contract import correlation_id, gen_id, ok, utc_now
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


def _as_quantity(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 1.0


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/consumptions", status_code=200)
async def create_material_consumption(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    idem_key = await require_idempotency_key(request)
    try:
        cached = await find_idempotent(db, idem_key)
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None
    if cached is not None:
        return ok({"consumptionId": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    serial_number = _as_str(body.get("serialNumber"))
    material_code = _as_str(body.get("materialCode"))
    lot_number = _as_str(body.get("lotNumber"))
    if not serial_number or not material_code or not lot_number:
        raise MESError(
            422, "VALIDATION_ERROR", "serialNumber, materialCode and lotNumber are required"
        )
    work_order_number = _as_str(body.get("workOrderNumber"))
    quantity = _as_quantity(body.get("quantity"))
    operator = get_actor(request)
    now = utc_now()

    # 库存校验：无记录或在手量不足 → 409（查询失败同样 409）
    try:
        row = (
            await db.execute(
                text(
                    "SELECT on_hand_quantity FROM material_inventory_balances "
                    "WHERE material_code = :mc AND lot_number = :lot LIMIT 1"
                ),
                {"mc": material_code, "lot": lot_number},
            )
        ).first()
    except Exception:
        raise MESError(409, "CONFLICT", "insufficient on-hand") from None
    if row is None or (row[0] is not None and float(row[0]) < quantity):
        raise MESError(409, "CONFLICT", "insufficient on-hand")

    consumption_id = gen_id("MC")
    try:
        # 1. 消耗主记录（未知工单/非正数量 → 约束失败，忽略）
        try:
            await db.execute(
                text(
                    "INSERT INTO production_material_consumptions "
                    "(consumption_id, serial_number, work_order_number, operation_sequence, "
                    " material_code, lot_number, quantity, unit_code, consumed_by, consumed_at) "
                    "VALUES (:id, :sn, :wo, 0, :mc, :lot, :q, 'EA', :op, :t)"
                ),
                {
                    "id": consumption_id,
                    "sn": serial_number,
                    "wo": work_order_number,
                    "mc": material_code,
                    "lot": lot_number,
                    "q": quantity,
                    "op": operator,
                    "t": now,
                },
            )
        except Exception:
            pass
        # 2. 扣减在手量（所有匹配余额行）；仅 reserved > 数量时预留同时减少，相等时预留不变
        await db.execute(
            text(
                "UPDATE material_inventory_balances SET "
                "on_hand_quantity = COALESCE(on_hand_quantity, 0) - :q, "
                "reserved_quantity = CASE WHEN COALESCE(reserved_quantity, 0) > :q "
                "                         THEN COALESCE(reserved_quantity, 0) - :q "
                "                         ELSE COALESCE(reserved_quantity, 0) END "
                "WHERE material_code = :mc AND lot_number = :lot"
            ),
            {"q": quantity, "mc": material_code, "lot": lot_number},
        )
        # 3. 物料事务（不提供 transaction_id → SQLite 拒绝，忽略）
        try:
            await db.execute(
                text(
                    "INSERT INTO material_material_transactions "
                    "(material_code, lot_number, transaction_type, quantity, plant_code, "
                    " location_code, reference_type, reference_id, transaction_at, actor_id) "
                    "VALUES (:mc, :lot, 'CONSUMPTION', :q, 'PLANT-A', 'RAW-STORE', "
                    "        'SERIAL_NUMBER', :sn, :t, :op)"
                ),
                {
                    "mc": material_code,
                    "lot": lot_number,
                    "q": quantity,
                    "sn": serial_number,
                    "t": now,
                    "op": operator,
                },
            )
        except Exception:
            pass
        # 4. 追溯 + 审计 + 幂等
        try:
            await db.execute(
                text(
                    "INSERT INTO trace_events "
                    "(event_id, event_type, resource_type, resource_code, related_work_order_number, "
                    " related_serial_number, message, operator_id, occurred_at, correlation_id, plant_code) "
                    "VALUES (:id, 'MATERIAL_CONSUMED', 'SERIAL_NUMBER', :sn, :w, :sn, "
                    "        'material consumed', :op, :t, :corr, 'PLANT-A')"
                ),
                {
                    "id": gen_id("EVT"),
                    "sn": serial_number,
                    "w": work_order_number,
                    "op": operator,
                    "t": now,
                    "corr": corr,
                },
            )
        except Exception:
            pass
        await write_audit(
            db,
            action="MATERIAL_CONSUME",
            resource_type="MATERIAL",
            resource_id=material_code,
            before={},
            after=body,
            actor_id=operator,
        )
        await save_idempotent(db, idem_key, "200", consumption_id, "consumed")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok({"consumptionId": consumption_id}, corr)
