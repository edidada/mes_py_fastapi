"""``POST /api/v1/integration/inbox`` 接收外部系统事件并同步投影消费。

不校验角色、不读幂等 Header。``eventId`` 同时作为收件箱消息主键；已存在的
``eventId`` 不再消费投影，直接返回已有状态与 ``cached=true``。按 ``eventType``
分支：``plan.pushed``（无生效路线 → 消息 FAILED）、``equipment.status``、
``wms.inventory``、其他。除“无生效路线”外，业务投影失败均忽略，消息最终更新
状态与错误信息。
"""

import json
import random
import string

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.contract import correlation_id, gen_id, ok, utc_now
from app.deps import DbSession
from app.errors import MESError

router = APIRouter()

_DEFAULT_PLANT = "PLANT-A"


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


def _as_quantity(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


@router.post("/inbox", status_code=202)
async def consume_inbox_message(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    if not isinstance(data, dict):
        data = {}
    body = data

    event_id = _as_str(body.get("eventId"))
    source_system = _as_str(body.get("sourceSystem"))
    event_type = _as_str(body.get("eventType"))
    if not event_id or not source_system or not event_type:
        raise MESError(422, "VALIDATION_ERROR", "eventId/sourceSystem/eventType required")

    payload = body.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    now = utc_now()

    # 查收件箱：已存在 → 返回已有状态 + cached
    try:
        existing = (
            await db.execute(
                text(
                    "SELECT status FROM integration_inbox_messages WHERE message_id = :id"
                ),
                {"id": event_id},
            )
        ).first()
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None
    if existing is not None:
        return ok(
            {"messageId": event_id, "status": existing[0], "cached": True}, corr
        )

    try:
        # 1. 写入收件箱（RECEIVED）
        await db.execute(
            text(
                "INSERT INTO integration_inbox_messages "
                "(message_id, source_system, message_type, status, retry_count, payload, "
                " received_at, processed_at, error_message) "
                "VALUES (:id, :src, :type, 'RECEIVED', 0, :payload, :t, NULL, NULL)"
            ),
            {
                "id": event_id,
                "src": source_system,
                "type": event_type,
                "payload": json.dumps(body, ensure_ascii=False),
                "t": now,
            },
        )

        status = "PROCESSED"
        error_message = ""
        # 2. 分支消费
        if event_type == "erp.plan.pushed" or "plan.pushed" in event_type:
            material_code = _as_str(payload.get("materialCode"))
            route = (
                await db.execute(
                    text(
                        "SELECT routing_code, version, description FROM master_routings "
                        "WHERE material_code = :mc AND status = 'EFFECTIVE' "
                        "ORDER BY routing_code LIMIT 1"
                    ),
                    {"mc": material_code},
                )
            ).first()
            if route is None:
                status = "FAILED"
                error_message = "no effective routing"
            else:
                try:
                    wo_number = (
                        f"WO-{now[2:10]}-"
                        + "".join(random.choices(string.digits, k=6))
                    )
                    plant_code = _as_str(payload.get("plantCode")) or _DEFAULT_PLANT
                    priority = _as_quantity(payload.get("priority"))
                    quantity = _as_quantity(payload.get("quantity"))
                    await db.execute(
                        text(
                            "INSERT INTO production_work_orders "
                            "(work_order_number, plant_code, material_code, routing_code, "
                            " routing_version, priority, status, planned_quantity, created_at, updated_at) "
                            "VALUES (:wo, :p, :mc, :rc, :rv, :pri, 'DRAFT', :q, :t, :t)"
                        ),
                        {
                            "wo": wo_number,
                            "p": plant_code,
                            "mc": material_code,
                            "rc": route[0],
                            "rv": route[1],
                            "pri": int(priority),
                            "q": quantity,
                            "t": now,
                        },
                    )
                    ops = (
                        await db.execute(
                            text(
                                "SELECT sequence, operation_code, work_center_code, quality_gate, "
                                "       allow_skip, standard_cycle_seconds "
                                "FROM master_routing_operations "
                                "WHERE routing_code = :rc AND version = :rv ORDER BY sequence"
                            ),
                            {"rc": route[0], "rv": route[1]},
                        )
                    ).fetchall()
                    for op in ops:
                        await db.execute(
                            text(
                                "INSERT INTO production_work_order_operations "
                                "(work_order_number, sequence, operation_code, work_center_code, "
                                " quality_gate, allow_skip, standard_cycle_seconds, status) "
                                "VALUES (:wo, :seq, :op, :wc, :qg, :ask, :cyc, 'PENDING')"
                            ),
                            {
                                "wo": wo_number,
                                "seq": op[0],
                                "op": op[1],
                                "wc": op[2],
                                "qg": op[3],
                                "ask": op[4],
                                "cyc": op[5],
                            },
                        )
                except Exception:
                    pass
        elif "equipment.status" in event_type:
            equipment_code = _as_str(payload.get("equipmentCode"))
            status_value = _as_str(payload.get("status"))
            if equipment_code and status_value:
                try:
                    plant_code = _as_str(payload.get("plantCode")) or _DEFAULT_PLANT
                    occurred_at = _as_str(payload.get("occurredAt")) or now
                    await db.execute(
                        text(
                            "INSERT INTO asset_equipment_events "
                            "(event_id, equipment_code, status, reason_code, occurred_at, source_event_id) "
                            "VALUES (:id, :c, :s, '', :o, :src)"
                        ),
                        {
                            "id": gen_id("EEQ"),
                            "c": equipment_code,
                            "s": status_value,
                            "o": occurred_at,
                            "src": event_id,
                        },
                    )
                    await db.execute(
                        text(
                            "UPDATE asset_equipment SET current_status = :s, last_heartbeat_at = :t "
                            "WHERE equipment_code = :c"
                        ),
                        {"s": status_value, "t": occurred_at, "c": equipment_code},
                    )
                except Exception:
                    pass
        elif "wms.inventory" in event_type:
            try:
                plant_code = _as_str(payload.get("plantCode")) or _DEFAULT_PLANT
                material_code = _as_str(payload.get("materialCode"))
                lot_number = _as_str(payload.get("lotNumber"))
                quantity = _as_quantity(payload.get("quantity"))
                await db.execute(
                    text(
                        "UPDATE material_inventory_balances SET on_hand_quantity = "
                        "COALESCE(on_hand_quantity, 0) + :q "
                        "WHERE plant_code = :p AND material_code = :mc AND lot_number = :lot"
                    ),
                    {
                        "q": quantity,
                        "p": plant_code,
                        "mc": material_code,
                        "lot": lot_number,
                    },
                )
            except Exception:
                pass

        # 3. 收件箱最终状态
        await db.execute(
            text(
                "UPDATE integration_inbox_messages SET status = :s, processed_at = :t, "
                "error_message = :e WHERE message_id = :id"
            ),
            {"s": status, "t": now, "e": error_message, "id": event_id},
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise MESError(500, "DATABASE_ERROR", "database error") from None

    return ok({"messageId": event_id, "status": status}, corr)
