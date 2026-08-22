"""``POST /api/v1/equipment/{equipmentCode}/events`` 上报设备状态事件。

保持 C++ 宽松语义：事件 INSERT 失败（未知状态 / sourceEventId 重复）返回 409；
设备状态更新、计数器、FAULT 附带的维修单/安灯/汇总失败均不影响请求成功。
幂等命中发生在 JSON 解析、路径设备与状态校验之前。无角色校验。
"""

import json

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from app.audit import write_audit
from app.contract import correlation_id, gen_id, ok, utc_now
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()

_ALLOWED_STATUSES = {"RUNNING", "IDLE", "DOWN", "FAULT", "MAINTENANCE", "OFFLINE"}


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/{equipmentCode}/events", status_code=202)
async def report_equipment_event(
    request: Request, equipmentCode: str, response: Response, db: DbSession
) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    idem_key = await require_idempotency_key(request)
    try:
        cached = await find_idempotent(db, idem_key)
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None
    if cached is not None:
        response.status_code = 200
        return ok({"eventId": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    status = body.get("status")
    if not isinstance(status, str) or status == "":
        raise MESError(422, "VALIDATION_ERROR", "status required")

    reason_code = _as_str(body.get("reasonCode"))
    occurred_at = _as_str(body.get("occurredAt")) or utc_now()
    source_event_id = _as_str(body.get("sourceEventId")) or idem_key

    # 计数器：counterCode 非空或 counterValue 字段存在即触发
    counter_code = _as_str(body.get("counterCode"))
    counter_triggered = bool(counter_code) or "counterValue" in body
    counter_value = 0.0
    if counter_triggered:
        cv = body.get("counterValue")
        if isinstance(cv, (int, float)) and not isinstance(cv, bool):
            counter_value = float(cv)
        if not counter_code:
            counter_code = "OUT"

    # 查询设备（工厂、当前状态）→ 不存在 404
    try:
        eq = (
            await db.execute(
                text(
                    "SELECT plant_code, current_status FROM asset_equipment "
                    "WHERE equipment_code = :c"
                ),
                {"c": equipmentCode},
            )
        ).first()
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None
    if eq is None:
        raise MESError(404, "NOT_FOUND", "equipment not found")
    # C++ 中未知状态由 INSERT 时 CHECK 约束触发 409；SQLite 无该约束 → 显式校验
    if status not in _ALLOWED_STATUSES:
        raise MESError(409, "CONFLICT", "unknown status value")
    plant_code = eq[0] or "PLANT-A"

    event_id = gen_id("EEQ")
    evt_id = gen_id("EVT")
    now = utc_now()
    try:
        # 1. 事件（唯一约束失败 → 409）
        try:
            await db.execute(
                text(
                    "INSERT INTO asset_equipment_events "
                    "(event_id, equipment_code, status, reason_code, occurred_at, source_event_id) "
                    "VALUES (:id, :c, :s, :r, :o, :src)"
                ),
                {
                    "id": event_id,
                    "c": equipmentCode,
                    "s": status,
                    "r": reason_code,
                    "o": occurred_at,
                    "src": source_event_id,
                },
            )
        except Exception as exc:
            await db.rollback()
            msg = str(exc)
            if "source_event_id" in msg:
                raise MESError(409, "CONFLICT", "duplicate sourceEventId") from None
            raise MESError(409, "CONFLICT", "unknown status value") from None

        # 2. 更新设备状态
        await db.execute(
            text(
                "UPDATE asset_equipment SET current_status = :s, last_heartbeat_at = :t "
                "WHERE equipment_code = :c"
            ),
            {"s": status, "t": now, "c": equipmentCode},
        )

        # 3. 计数器
        if counter_triggered:
            await db.execute(
                text(
                    "INSERT INTO asset_equipment_counters "
                    "(equipment_code, counter_code, value, recorded_at) "
                    "VALUES (:c, :cc, :v, :t)"
                ),
                {"c": equipmentCode, "cc": counter_code, "v": counter_value, "t": now},
            )
            hour_bucket = f"{occurred_at[:13]}:00:00"
            await db.execute(
                text(
                    "UPDATE reporting_equipment_oee_hourly SET actual_output = "
                    "COALESCE(actual_output, 0) + :v "
                    "WHERE equipment_code = :c AND hour_bucket = :h"
                ),
                {"v": counter_value, "c": equipmentCode, "h": hour_bucket},
            )

        # 4. FAULT 附带维修单 + 安灯 + 汇总
        if status == "FAULT":
            await db.execute(
                text(
                    "INSERT INTO asset_maintenance_work_orders "
                    "(maintenance_work_order_id, equipment_code, maintenance_type, description, "
                    " assignee_id, status, due_at, completed_at) "
                    "VALUES (:id, :c, 'CORRECTIVE', 'equipment fault', '', 'OPEN', '', NULL)"
                ),
                {"id": gen_id("MWO"), "c": equipmentCode},
            )
            await db.execute(
                text(
                    "INSERT INTO trace_andon_events "
                    "(andon_id, plant_code, severity, category, resource_type, resource_code, "
                    " related_work_order_number, related_serial_number, message, raised_at) "
                    "VALUES (:id, :p, 'CRITICAL', 'EQUIPMENT', 'EQUIPMENT', :c, '', '', "
                    "        'equipment fault', :t)"
                ),
                {"id": gen_id("AND"), "p": plant_code, "c": equipmentCode, "t": now},
            )
            await db.execute(
                text(
                    "UPDATE reporting_andon_summary SET raised_count = raised_count + 1 "
                    "WHERE plant_code = :p AND summary_date = :d AND severity = 'CRITICAL'"
                ),
                {"p": plant_code, "d": now[:10]},
            )

        # 5. 审计 + 幂等
        await write_audit(
            db,
            action="EQUIPMENT_EVENT",
            resource_type="EQUIPMENT",
            resource_id=equipmentCode,
            before={"from": eq[1] if eq[1] is not None else ""},
            after=body,
            actor_id=request.headers.get("X-Actor-Id") or "system",
        )
        await save_idempotent(db, idem_key, "202", evt_id, "accepted")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok({"accepted": True}, corr)
