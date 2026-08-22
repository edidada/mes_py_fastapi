"""``POST /api/v1/executions/complete`` 完成产品单元当前工序。

记录过程参数与物料消耗，按人工判定/参数规格/质量门决定新状态，并同步
工单/工序、人工、追溯、安灯、报表、outbox、审计与幂等投影。接口不检查角色。
与 C++ 一致：前置校验通过并成功开启事务后，事务内各项写入与 commit 失败
不影响 HTTP 成功响应。
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


def _parse_passed(value) -> bool:
    """默认 true；boolean 原值；string 仅 true/TRUE 为真；number 非零为真。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value in ("true", "TRUE")
    if isinstance(value, (int, float)):
        return value != 0
    return True


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


def _as_number(value, default: float) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/complete", status_code=200)
async def complete_execution(request: Request, db: DbSession) -> dict:
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
    station_code = _as_str(body.get("stationCode"))
    equipment_code = _as_str(body.get("equipmentCode"))
    defect_code = _as_str(body.get("defectCode"))
    passed = _parse_passed(body.get("passed"))
    parameters = body.get("parameters") if isinstance(body.get("parameters"), list) else []
    consumptions = (
        body.get("materialConsumptions")
        if isinstance(body.get("materialConsumptions"), list)
        else []
    )
    operator = get_actor(request)
    now = utc_now()

    # 前置：序列号必须存在且工序完全匹配
    try:
        result = await db.execute(
            text(
                "SELECT serial_number, current_operation_sequence "
                "FROM production_product_units WHERE serial_number = :s"
            ),
            {"s": serial_number},
        )
        unit = result.fetchone()
    except Exception as exc:
        raise MESError(404, "NOT_FOUND", "product unit not found") from exc
    if unit is None:
        raise MESError(404, "NOT_FOUND", "product unit not found")
    if unit[1] != sequence:
        raise MESError(409, "CONFLICT", "operation sequence does not match product unit")

    # 工序与工单主数据（缺省回退）
    operation = await _load_operation(db, work_order_number, sequence)
    operation_code = operation["operation_code"] if operation else f"OP-{sequence}"
    quality_gate = bool(operation["quality_gate"]) if operation else False
    wo = await _load_work_order(db, work_order_number)
    material_code = wo["material_code"] if wo else ""
    plant_code = wo["plant_code"] if wo else "PLANT-A"

    out_of_spec = False
    unit_status = ""
    wo_completed = False
    event_type = "PASS"
    next_sequence = None
    accepted = False

    try:
        # 过程参数：写入记录并判断规格是否越界
        for p in parameters:
            code = _as_str(p.get("code"))
            value = _as_number(p.get("value"), 0.0)
            spec = await _load_parameter_spec(db, material_code, code)
            lower, upper = 0.0, 0.0
            in_spec = 1
            if spec is not None:
                if spec["lower"] is not None:
                    lower = spec["lower"]
                    if value < lower:
                        in_spec = 0
                        out_of_spec = True
                if spec["upper"] is not None:
                    upper = spec["upper"]
                    if value > upper:
                        in_spec = 0
                        out_of_spec = True
            await db.execute(
                text(
                    "INSERT INTO production_parameter_records "
                    "(record_id, serial_number, work_order_number, operation_sequence, code, "
                    " value, lower_limit, upper_limit, in_spec, operator_id, recorded_at) "
                    "VALUES (:id, :s, :w, :seq, :code, :v, :lo, :up, :spec, :op, :t)"
                ),
                {
                    "id": gen_id("PR"),
                    "s": serial_number,
                    "w": work_order_number,
                    "seq": sequence,
                    "code": code,
                    "v": value,
                    "lo": lower,
                    "up": upper,
                    "spec": in_spec,
                    "op": operator,
                    "t": now,
                },
            )

        # 物料消耗：写入消耗记录、扣减库存、写追溯
        for c in consumptions:
            mat = _as_str(c.get("materialCode"))
            lot = _as_str(c.get("lotNumber"))
            quantity = _as_number(c.get("quantity"), 1.0)
            await db.execute(
                text(
                    "INSERT INTO production_material_consumptions "
                    "(consumption_id, serial_number, work_order_number, operation_sequence, "
                    " material_code, lot_number, quantity, unit_code, consumed_by, consumed_at) "
                    "VALUES (:id, :s, :w, :seq, :m, :lot, :q, 'EA', :op, :t)"
                ),
                {
                    "id": gen_id("MC"),
                    "s": serial_number,
                    "w": work_order_number,
                    "seq": sequence,
                    "m": mat,
                    "lot": lot,
                    "q": quantity,
                    "op": operator,
                    "t": now,
                },
            )
            await db.execute(
                text(
                    "UPDATE material_inventory_balances "
                    "SET on_hand_quantity = on_hand_quantity - :q, "
                    "    reserved_quantity = CASE WHEN reserved_quantity > :q "
                    "        THEN reserved_quantity - :q ELSE reserved_quantity END "
                    "WHERE plant_code = :p AND material_code = :m AND lot_number = :lot"
                ),
                {"q": quantity, "p": plant_code, "m": mat, "lot": lot},
            )
            await db.execute(
                text(
                    "INSERT INTO trace_events "
                    "(event_id, event_type, resource_type, resource_code, related_work_order_number, "
                    " related_serial_number, message, operator_id, occurred_at, correlation_id, plant_code) "
                    "VALUES (:id, 'MATERIAL_CONSUMED', 'MATERIAL', :m, :w, :s, 'material consumed', :op, :t, :corr, :p)"
                ),
                {
                    "id": gen_id("EVT"),
                    "m": mat,
                    "w": work_order_number,
                    "s": serial_number,
                    "op": operator,
                    "t": now,
                    "corr": corr,
                    "p": plant_code,
                },
            )
            await db.execute(
                text(
                    "INSERT INTO trace_events "
                    "(event_id, event_type, resource_type, resource_code, related_work_order_number, "
                    " related_serial_number, message, operator_id, occurred_at, correlation_id, plant_code) "
                    "VALUES (:id, 'MATERIAL_LOT', 'LOT', :lot, :w, :s, 'material consumed from lot', :op, :t, :corr, :p)"
                ),
                {
                    "id": gen_id("EVT"),
                    "lot": lot,
                    "w": work_order_number,
                    "s": serial_number,
                    "op": operator,
                    "t": now,
                    "corr": corr,
                    "p": plant_code,
                },
            )

        # 下一工序与质量门（质量门先查询 ACCEPTED 检验结果）
        next_sequence = await _load_next_sequence(db, work_order_number, sequence)
        if quality_gate:
            accepted = await _has_accepted_inspection(db, serial_number, sequence)

        # 状态判定
        if not passed:
            unit_status, wo_completed, event_type = "FAILED", False, "FAIL"
        elif out_of_spec:
            unit_status, wo_completed, event_type = "HOLD", False, "PASS"
        elif quality_gate and not accepted:
            unit_status, wo_completed, event_type = "WAITING_INSPECTION", False, "PASS"
        elif next_sequence is not None:
            unit_status, wo_completed, event_type = "IN_PROCESS", False, "PASS"
        else:
            unit_status, wo_completed, event_type = "PASSED", True, "PASS"

        # FAILED → rejected_quantity+1
        if unit_status == "FAILED":
            await db.execute(
                text(
                    "UPDATE production_work_orders SET rejected_quantity = rejected_quantity + 1 "
                    "WHERE work_order_number = :w"
                ),
                {"w": work_order_number},
            )
        # HOLD → CRITICAL/QUALITY 安灯 + 当天汇总累加
        if unit_status == "HOLD":
            await db.execute(
                text(
                    "INSERT INTO trace_andon_events "
                    "(andon_id, plant_code, severity, category, resource_type, resource_code, "
                    " related_work_order_number, related_serial_number, message, raised_at) "
                    "VALUES (:id, :p, 'CRITICAL', 'QUALITY', 'PRODUCT_UNIT', :s, :w, :s, 'quality hold', :t)"
                ),
                {"id": gen_id("AND"), "p": plant_code, "s": serial_number, "w": work_order_number, "t": now},
            )
            await db.execute(
                text(
                    "UPDATE reporting_andon_summary SET raised_count = raised_count + 1 "
                    "WHERE plant_code = :p AND summary_date = :d AND severity = 'CRITICAL'"
                ),
                {"p": plant_code, "d": now[:10]},
            )

        # 执行事件（含幂等键、关联 ID、缺陷代码）
        await db.execute(
            text(
                "INSERT INTO production_execution_events "
                "(event_id, serial_number, work_order_number, operation_sequence, operation_code, "
                " station_code, equipment_code, operator_id, event_type, occurred_at, plant_code, "
                " correlation_id, idempotency_key, defect_code) "
                "VALUES (:id, :s, :w, :seq, :op, :st, :eq, :actor, :etype, :t, :p, :corr, :idem, :defect)"
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
                "etype": event_type,
                "t": now,
                "p": plant_code,
                "corr": corr,
                "idem": idem_key,
                "defect": defect_code,
            },
        )

        # 状态同步
        await db.execute(
            text(
                "UPDATE production_work_order_operations SET status = 'COMPLETED' "
                "WHERE work_order_number = :w AND sequence = :seq"
            ),
            {"w": work_order_number, "seq": sequence},
        )
        if unit_status == "PASSED":
            await db.execute(
                text("UPDATE production_product_units SET status = 'PASSED' WHERE serial_number = :s"),
                {"s": serial_number},
            )
            await db.execute(
                text(
                    "UPDATE production_work_orders SET completed_quantity = completed_quantity + 1 "
                    "WHERE work_order_number = :w"
                ),
                {"w": work_order_number},
            )
            if await _all_operations_completed(db, work_order_number):
                await db.execute(
                    text("UPDATE production_work_orders SET status = 'COMPLETED' WHERE work_order_number = :w"),
                    {"w": work_order_number},
                )
            await db.execute(
                text(
                    "INSERT INTO integration_outbox_messages "
                    "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, status, payload, created_at) "
                    "VALUES (:id, 'WORK_ORDER', :w, 'work_order.completion', 'ERP', 'PENDING', :payload, :t)"
                ),
                {
                    "id": gen_id("OUT"),
                    "w": work_order_number,
                    "payload": json.dumps(
                        {"workOrderNumber": work_order_number, "serialNumber": serial_number},
                        ensure_ascii=False,
                    ),
                    "t": now,
                },
            )
        elif unit_status == "IN_PROCESS":
            await db.execute(
                text(
                    "UPDATE production_work_order_operations SET status = 'STARTED' "
                    "WHERE work_order_number = :w AND sequence = :seq"
                ),
                {"w": work_order_number, "seq": next_sequence},
            )
            await db.execute(
                text(
                    "UPDATE production_product_units SET current_operation_sequence = :seq "
                    "WHERE serial_number = :s"
                ),
                {"seq": next_sequence, "s": serial_number},
            )
        elif unit_status == "WAITING_INSPECTION":
            await db.execute(
                text("UPDATE production_product_units SET status = 'WAITING_INSPECTION' WHERE serial_number = :s"),
                {"s": serial_number},
            )
        else:  # HOLD / FAILED
            await db.execute(
                text("UPDATE production_product_units SET status = :st WHERE serial_number = :s"),
                {"st": unit_status, "s": serial_number},
            )

        # 关闭 OPEN 人工记录
        await db.execute(
            text(
                "UPDATE production_labor_records "
                "SET status = 'CLOSED', end_at = :t, duration_seconds = "
                "  CAST(strftime('%s', :t) AS INTEGER) - CAST(strftime('%s', start_at) AS INTEGER) "
                "WHERE serial_number = :s AND operation_sequence = :seq AND status = 'OPEN'"
            ),
            {"t": now, "s": serial_number, "seq": sequence},
        )

        # 追溯 + 审计
        await db.execute(
            text(
                "INSERT INTO trace_events "
                "(event_id, event_type, resource_type, resource_code, related_work_order_number, "
                " related_serial_number, message, operator_id, occurred_at, correlation_id, plant_code) "
                "VALUES (:id, 'EXECUTION_COMPLETE', 'SERIAL_NUMBER', :s, :w, :s, 'execution completed', :op, :t, :corr, :p)"
            ),
            {
                "id": gen_id("EVT"),
                "s": serial_number,
                "w": work_order_number,
                "op": operator,
                "t": now,
                "corr": corr,
                "p": plant_code,
            },
        )
        await write_audit(
            db,
            action="EXECUTION_COMPLETE",
            resource_type="PRODUCT_UNIT",
            resource_id=serial_number,
            before={},
            after=body,
            actor_id=operator,
        )
        # 当天班次汇总（存在才更新）
        await db.execute(
            text(
                "UPDATE reporting_production_shift_summary "
                "SET completed_quantity = completed_quantity + 1, last_event_at = :t "
                "WHERE plant_code = :p AND shift_date = :d"
            ),
            {"p": plant_code, "d": now[:10], "t": now},
        )

        await save_idempotent(db, idem_key, "200", serial_number, "completed")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok(
        {
            "serialNumber": serial_number,
            "operationSequence": sequence,
            "unitStatus": unit_status,
            "workOrderCompleted": wo_completed,
        },
        corr,
    )


async def _load_operation(db: DbSession, work_order_number: str, sequence: int) -> dict | None:
    try:
        result = await db.execute(
            text(
                "SELECT operation_code, quality_gate FROM production_work_order_operations "
                "WHERE work_order_number = :w AND sequence = :seq"
            ),
            {"w": work_order_number, "seq": sequence},
        )
        row = result.fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return {"operation_code": row[0], "quality_gate": row[1]}


async def _load_work_order(db: DbSession, work_order_number: str) -> dict | None:
    try:
        result = await db.execute(
            text(
                "SELECT material_code, plant_code FROM production_work_orders WHERE work_order_number = :w"
            ),
            {"w": work_order_number},
        )
        row = result.fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return {"material_code": row[0], "plant_code": row[1]}


async def _load_parameter_spec(db: DbSession, material_code: str, code: str) -> dict | None:
    try:
        result = await db.execute(
            text(
                "SELECT lower_limit, upper_limit FROM master_parameter_specifications "
                "WHERE material_code = :m AND code = :code"
            ),
            {"m": material_code, "code": code},
        )
        row = result.fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return {"lower": row[0], "upper": row[1]}


async def _load_next_sequence(db: DbSession, work_order_number: str, sequence: int) -> int | None:
    try:
        result = await db.execute(
            text(
                "SELECT sequence FROM production_work_order_operations "
                "WHERE work_order_number = :w AND sequence > :seq ORDER BY sequence LIMIT 1"
            ),
            {"w": work_order_number, "seq": sequence},
        )
        row = result.fetchone()
    except Exception:
        return None
    return row[0] if row is not None else None


async def _has_accepted_inspection(db: DbSession, serial_number: str, sequence: int) -> bool:
    try:
        result = await db.execute(
            text(
                "SELECT count(*) FROM quality_inspection_results "
                "WHERE serial_number = :s AND operation_sequence = :seq AND disposition = 'ACCEPTED'"
            ),
            {"s": serial_number, "seq": sequence},
        )
        return bool(result.scalar())
    except Exception:
        return False


async def _all_operations_completed(db: DbSession, work_order_number: str) -> bool:
    try:
        result = await db.execute(
            text(
                "SELECT count(*) FROM production_work_order_operations "
                "WHERE work_order_number = :w AND status NOT IN ('COMPLETED', 'SKIPPED')"
            ),
            {"w": work_order_number},
        )
        return result.scalar() == 0
    except Exception:
        return False
