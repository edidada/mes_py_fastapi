"""``POST /api/v1/executions/start`` 工序执行开始。

记录 START 事件、把单元置为 ``IN_PROCESS``、按条件启动工单，并同步工序、
派工任务、人工记录、追溯、审计和幂等投影。接口不检查角色。
与 C++ 一致：除事务开始外，第 2 至第 8 步任何 SQL 或 commit 失败
都不影响 HTTP 成功响应。
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


def _parse_sequence(value) -> int:
    """number/string 转为整数；小数截断、整数字符串可转换，无效值默认 0。"""
    if isinstance(value, bool):
        return 0
    try:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            return int(float(value)) if value.strip() else 0
    except (ValueError, TypeError):
        pass
    return 0


def _parse_operation_code(value, sequence: int) -> str:
    """省略、空、null 或非字符串时生成 ``OP-{sequence}``。"""
    if not isinstance(value, str) or value == "":
        return f"OP-{sequence}"
    return value


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/start", status_code=200)
async def start_execution(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"serialNumber": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    serial_number = body.get("serialNumber")
    work_order_number = body.get("workOrderNumber")
    if not isinstance(serial_number, str) or not serial_number:
        raise MESError(422, "VALIDATION_ERROR", "serialNumber and workOrderNumber must be non-empty strings")
    if not isinstance(work_order_number, str) or not work_order_number:
        raise MESError(422, "VALIDATION_ERROR", "serialNumber and workOrderNumber must be non-empty strings")

    sequence = _parse_sequence(body.get("operationSequence"))
    operation_code = _parse_operation_code(body.get("operationCode"), sequence)
    station_code = _as_str(body.get("stationCode"))
    equipment_code = _as_str(body.get("equipmentCode"))
    operator = get_actor(request)
    now = utc_now()

    # 前置序列号必须存在；查询失败按 C++ queryOne 语义表现为 404
    try:
        result = await db.execute(
            text("SELECT serial_number FROM production_product_units WHERE serial_number = :s"),
            {"s": serial_number},
        )
        unit = result.fetchone()
    except Exception as exc:
        raise MESError(404, "NOT_FOUND", "product unit not found") from exc
    if unit is None:
        raise MESError(404, "NOT_FOUND", "product unit not found")

    try:
        await db.execute(
            text(
                "INSERT INTO production_execution_events "
                "(event_id, serial_number, work_order_number, operation_sequence, operation_code, "
                " station_code, equipment_code, operator_id, event_type, occurred_at, plant_code, correlation_id) "
                "VALUES (:id, :s, :w, :seq, :op, :st, :eq, :actor, 'START', :t, 'PLANT-A', :corr)"
            ),
            {
                "id": gen_id("EVT"),
                "s": serial_number,
                "w": work_order_number,
                "seq": sequence,
                "op": operation_code,
                "st": station_code,
                "eq": equipment_code,
                "actor": operator,
                "t": now,
                "corr": corr,
            },
        )
        await db.execute(
            text(
                "UPDATE production_product_units "
                "SET status = 'IN_PROCESS', current_station_code = :st, started_at = :t "
                "WHERE serial_number = :s"
            ),
            {"st": station_code, "t": now, "s": serial_number},
        )
        await db.execute(
            text(
                "UPDATE production_work_orders "
                "SET status = 'IN_PROGRESS', actual_start_at = :t "
                "WHERE work_order_number = :w AND status = 'RELEASED'"
            ),
            {"t": now, "w": work_order_number},
        )
        await db.execute(
            text(
                "UPDATE production_work_order_operations SET status = 'STARTED' "
                "WHERE work_order_number = :w AND sequence = :seq"
            ),
            {"w": work_order_number, "seq": sequence},
        )
        await db.execute(
            text(
                "UPDATE production_operation_tasks SET status = 'STARTED' "
                "WHERE work_order_number = :w AND operation_sequence = :seq"
            ),
            {"w": work_order_number, "seq": sequence},
        )
        await db.execute(
            text(
                "INSERT INTO production_labor_records "
                "(labor_id, serial_number, work_order_number, operation_sequence, worker_id, station_code, start_at, status) "
                "VALUES (:id, :s, :w, :seq, :actor, :st, :t, 'OPEN')"
            ),
            {
                "id": gen_id("LAB"),
                "s": serial_number,
                "w": work_order_number,
                "seq": sequence,
                "actor": operator,
                "st": station_code,
                "t": now,
            },
        )
        await db.execute(
            text(
                "INSERT INTO trace_events "
                "(event_id, event_type, resource_type, resource_code, related_work_order_number, "
                " related_serial_number, message, operator_id, occurred_at, correlation_id) "
                "VALUES (:id, 'EXECUTION_START', 'EQUIPMENT', :eq, :w, :s, 'execution started', :actor, :t, :corr)"
            ),
            {
                "id": gen_id("EVT"),
                "eq": equipment_code,
                "w": work_order_number,
                "s": serial_number,
                "actor": operator,
                "t": now,
                "corr": corr,
            },
        )
        await write_audit(
            db,
            action="EXECUTION_START",
            resource_type="PRODUCT_UNIT",
            resource_id=serial_number,
            before={},
            after=body,
            actor_id=operator,
        )
        await save_idempotent(db, idem_key, "200", serial_number, "started")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok({"serialNumber": serial_number, "status": "IN_PROCESS"}, corr)
