"""``POST /api/v1/quality/inspections`` 提交检验结果并更新检验批。

``ACCEPTED`` 放行 ``WAITING_INSPECTION`` 产品并推进/完成，``REJECTED``
创建 MAJOR/OPEN 不合格项并置产品 ``HOLD``。接口写入质量报表、追溯、审计
与幂等投影。仅 ``QUALITY_ENGINEER`` 或 ``MES_ADMIN`` 可调用，且角色校验
先于幂等查询。与 C++ 一致：事务开始后的写入失败被忽略，接口仍返回成功。
"""

import json

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.audit import write_audit
from app.auth import get_actor, require_role
from app.contract import correlation_id, gen_id, ok, utc_now
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()

_ALLOWED_ROLES = {"QUALITY_ENGINEER", "MES_ADMIN"}
_DEFAULT_PLAN = "IP-01"


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


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


def _parse_plan_code(value) -> str:
    """省略、空、null 或非字符串使用 IP-01；非空值原样保存。"""
    if not isinstance(value, str) or value == "":
        return _DEFAULT_PLAN
    return value


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/inspections", status_code=200)
async def submit_inspection(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "QUALITY_ENGINEER required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"inspectionId": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    serial_number = _as_str(body.get("serialNumber"))
    disposition = _as_str(body.get("disposition"))
    work_order_number = _as_str(body.get("workOrderNumber"))
    inspection_lot_id = _as_str(body.get("inspectionLotId"))
    plan_code = _parse_plan_code(body.get("inspectionPlanCode"))
    operation_sequence = _parse_sequence(body.get("operationSequence"))
    defect_code = _as_str(body.get("defectCode"))
    operator = get_actor(request)
    now = utc_now()

    inspection_id = gen_id("INS")
    unit_disposition = disposition

    try:
        # 1. 检验结果：工厂 PLANT-A、检验员来自 Header、payload 为完整请求
        try:
            await db.execute(
                text(
                    "INSERT INTO quality_inspection_results "
                    "(inspection_id, plant_code, lot_id, serial_number, work_order_number, plan_code, "
                    " operation_sequence, disposition, inspector_id, inspected_at, payload) "
                    "VALUES (:id, 'PLANT-A', :lot, :s, :w, :plan, :seq, :disp, :insp, :t, :payload)"
                ),
                {
                    "id": inspection_id,
                    "lot": inspection_lot_id,
                    "s": serial_number,
                    "w": work_order_number,
                    "plan": plan_code,
                    "seq": operation_sequence,
                    "disp": disposition,
                    "insp": operator,
                    "t": now,
                    "payload": json.dumps(body, ensure_ascii=False),
                },
            )
        except Exception:
            pass
        # 2. 非空检验批时更新状态
        if inspection_lot_id:
            await db.execute(
                text("UPDATE quality_inspection_lots SET status = :s WHERE lot_id = :id"),
                {"s": disposition, "id": inspection_lot_id},
            )

        # 3. ACCEPTED：放行 WAITING_INSPECTION 产品
        if disposition == "ACCEPTED":
            unit = await _load_unit(db, serial_number)
            if unit is None:
                unit_disposition = ""
            elif unit["status"] != "WAITING_INSPECTION":
                unit_disposition = unit["status"]
            else:
                unit_wo = unit["work_order_number"]
                next_sequence = await _load_next_sequence(db, unit_wo, unit["current_operation_sequence"])
                if next_sequence is not None:
                    unit_disposition = "IN_PROCESS"
                    await db.execute(
                        text(
                            "UPDATE production_product_units "
                            "SET status = 'IN_PROCESS', current_operation_sequence = :seq, updated_at = :t "
                            "WHERE serial_number = :s"
                        ),
                        {"seq": next_sequence, "t": now, "s": serial_number},
                    )
                else:
                    unit_disposition = "PASSED"
                    await db.execute(
                        text(
                            "UPDATE production_product_units SET status = 'PASSED', updated_at = :t "
                            "WHERE serial_number = :s"
                        ),
                        {"t": now, "s": serial_number},
                    )
                    await db.execute(
                        text(
                            "UPDATE production_work_orders SET completed_quantity = completed_quantity + 1 "
                            "WHERE work_order_number = :w"
                        ),
                        {"w": unit_wo},
                    )
                    if await _all_operations_completed(db, unit_wo):
                        await db.execute(
                            text(
                                "UPDATE production_work_orders SET status = 'COMPLETED' WHERE work_order_number = :w"
                            ),
                            {"w": unit_wo},
                        )
                # 放行分支始终写 completion outbox（工单号取产品单元）
                if unit_wo:
                    await db.execute(
                        text(
                            "INSERT INTO integration_outbox_messages "
                            "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, status, payload, created_at) "
                            "VALUES (:id, 'WORK_ORDER', :w, 'work_order.completion', 'ERP', 'PENDING', :payload, :t)"
                        ),
                        {
                            "id": gen_id("OUT"),
                            "w": unit_wo,
                            "payload": json.dumps(
                                {"workOrderNumber": unit_wo, "serialNumber": serial_number},
                                ensure_ascii=False,
                            ),
                            "t": now,
                        },
                    )

        # 4. REJECTED：MAJOR/OPEN 不合格项 + 产品 HOLD + CRITICAL/QUALITY 安灯
        if disposition == "REJECTED":
            ncid = gen_id("NC")
            try:
                await db.execute(
                    text(
                        "INSERT INTO quality_nonconformances "
                        "(nonconformance_id, plant_code, serial_number, work_order_number, defect_code, "
                        " severity, status, affected_qty, reported_by, reported_at) "
                        "VALUES (:id, 'PLANT-A', :s, :w, :defect, 'MAJOR', 'OPEN', 1, :op, :t)"
                    ),
                    {
                        "id": ncid,
                        "s": serial_number,
                        "w": work_order_number,
                        "defect": defect_code if defect_code else "DEF-UNKNOWN",
                        "op": operator,
                        "t": now,
                    },
                )
            except Exception:
                pass
            await db.execute(
                text(
                    "UPDATE production_product_units SET status = 'HOLD', updated_at = :t "
                    "WHERE serial_number = :s"
                ),
                {"t": now, "s": serial_number},
            )
            await db.execute(
                text(
                    "INSERT INTO trace_andon_events "
                    "(andon_id, plant_code, severity, category, resource_type, resource_code, "
                    " related_work_order_number, related_serial_number, message, raised_at) "
                    "VALUES (:id, 'PLANT-A', 'CRITICAL', 'QUALITY', 'PRODUCT_UNIT', :s, :w, :s, 'inspection rejected', :t)"
                ),
                {"id": gen_id("AND"), "s": serial_number, "w": work_order_number, "t": now},
            )
            unit_disposition = "HOLD"

        # 5. 当天质量班次汇总：inspection_count+1，按处置增加对应计数
        await db.execute(
            text(
                "UPDATE reporting_quality_shift_summary "
                "SET inspection_count = inspection_count + 1, "
                "    accepted_count = accepted_count + CASE WHEN :disp = 'ACCEPTED' THEN 1 ELSE 0 END, "
                "    rejected_count = rejected_count + CASE WHEN :disp = 'REJECTED' THEN 1 ELSE 0 END, "
                "    hold_count = hold_count + CASE WHEN :disp = 'HOLD' THEN 1 ELSE 0 END "
                "WHERE plant_code = 'PLANT-A' AND shift_date = :d"
            ),
            {"disp": disposition, "d": now[:10]},
        )

        # 6. 追溯 + 审计
        await db.execute(
            text(
                "INSERT INTO trace_events "
                "(event_id, event_type, resource_type, resource_code, related_work_order_number, "
                " related_serial_number, message, operator_id, occurred_at, correlation_id, plant_code) "
                "VALUES (:id, 'INSPECTION', 'SERIAL_NUMBER', :s, :w, :s, 'inspection submitted', :op, :t, :corr, 'PLANT-A')"
            ),
            {
                "id": gen_id("EVT"),
                "s": serial_number,
                "w": work_order_number,
                "op": operator,
                "t": now,
                "corr": corr,
            },
        )
        await write_audit(
            db,
            action="INSPECTION_SUBMIT",
            resource_type="INSPECTION",
            resource_id=inspection_id,
            before={},
            after=body,
            actor_id=operator,
        )
        # 7. 幂等投影：消息为请求处置
        await save_idempotent(db, idem_key, "200", inspection_id, disposition)
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok(
        {
            "inspectionId": inspection_id,
            "lotStatus": disposition,
            "unitDisposition": unit_disposition,
        },
        corr,
    )


async def _load_unit(db: DbSession, serial_number: str) -> dict | None:
    try:
        result = await db.execute(
            text(
                "SELECT serial_number, work_order_number, status, current_operation_sequence "
                "FROM production_product_units WHERE serial_number = :s"
            ),
            {"s": serial_number},
        )
        row = result.fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return {
        "serial_number": row[0],
        "work_order_number": row[1],
        "status": row[2],
        "current_operation_sequence": row[3],
    }


async def _load_next_sequence(db: DbSession, work_order_number: str, sequence: int | None) -> int | None:
    if not work_order_number:
        return None
    try:
        result = await db.execute(
            text(
                "SELECT sequence FROM production_work_order_operations "
                "WHERE work_order_number = :w AND sequence > :seq ORDER BY sequence LIMIT 1"
            ),
            {"w": work_order_number, "seq": sequence if sequence is not None else 0},
        )
        row = result.fetchone()
    except Exception:
        return None
    return row[0] if row is not None else None


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
