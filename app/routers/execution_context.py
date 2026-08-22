"""``GET /api/v1/execution-context`` 执行上下文。

查询 ``production_product_units`` 定位序列号；不存在时按工单自动创建
``CREATED`` 单元（当前工序固定为 10）。聚合当前工序参数规格、SOP 与生效 BOM。
与 C++ 一致：查询失败时聚合数据退化为空数组/空对象，不影响成功响应。
"""

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from app.contract import correlation_id, ok, utc_now
from app.deps import DbSession
from app.errors import MESError

router = APIRouter()

_DEFAULT_WORK_ORDER = "WO-20260801-001"


@router.get("", status_code=200)
async def get_execution_context(
    request: Request,
    db: DbSession,
    serialNumber: str | None = Query(default=None),
    workOrderNumber: str | None = Query(default=None),
) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    if serialNumber is None:
        raise MESError(422, "VALIDATION_ERROR", "serialNumber required")

    unit = await _load_unit(db, serialNumber)

    if unit is None:
        wo_number = workOrderNumber if workOrderNumber is not None else _DEFAULT_WORK_ORDER
        wo = await _load_work_order(db, wo_number)
        if wo is None:
            raise MESError(404, "NOT_FOUND", "work order not found for new serial")

        now = utc_now()
        await db.execute(
            text(
                "INSERT INTO production_product_units "
                "(serial_number, work_order_number, status, current_operation_sequence, created_at) "
                "VALUES (:s, :w, 'CREATED', 10, :t)"
            ),
            {"s": serialNumber, "w": wo["work_order_number"], "t": now},
        )
        await db.commit()

        unit = await _load_unit(db, serialNumber)
        if unit is None:
            raise MESError(500, "DATABASE_ERROR", "failed to load product unit after auto-create")
    else:
        wo = await _load_work_order(db, unit["work_order_number"] or "")
        if wo is None:
            raise MESError(500, "DATABASE_ERROR", "failed to load work order for serial")

    operation = None
    if unit["current_operation_sequence"] is not None:
        operation = await _load_current_operation(
            db, wo["work_order_number"], unit["current_operation_sequence"]
        )

    parameters = []
    sop = None
    if operation is not None:
        parameters = await _load_parameters(db, wo["material_code"] or "", operation["operation_code"] or "")
        if operation["sop_id"]:
            sop = await _load_sop(db, operation["sop_id"])

    materials = await _load_bom_materials(db, wo["material_code"] or "")

    data = {
        "serialNumber": serialNumber,
        "workOrderNumber": wo["work_order_number"] or "",
        "materialCode": wo["material_code"] or "",
        "currentOperationSequence": (
            unit["current_operation_sequence"]
            if unit["current_operation_sequence"] is not None
            else 10
        ),
        "currentStationCode": unit["current_station_code"] or "",
        "unitStatus": unit["status"] or "",
        "sop": sop,
        "parameters": parameters,
        "materials": materials,
        "validations": {"operationMatch": True, "equipmentReady": True},
    }
    return ok(data, corr)


async def _load_unit(db: DbSession, serial_number: str) -> dict | None:
    result = await db.execute(
        text(
            "SELECT serial_number, work_order_number, material_code, status, "
            "current_operation_sequence, current_station_code "
            "FROM production_product_units WHERE serial_number = :s"
        ),
        {"s": serial_number},
    )
    row = result.fetchone()
    if row is None:
        return None
    return {
        "serial_number": row[0],
        "work_order_number": row[1],
        "material_code": row[2],
        "status": row[3],
        "current_operation_sequence": row[4],
        "current_station_code": row[5],
    }


async def _load_work_order(db: DbSession, work_order_number: str) -> dict | None:
    result = await db.execute(
        text(
            "SELECT work_order_number, material_code FROM production_work_orders "
            "WHERE work_order_number = :w"
        ),
        {"w": work_order_number},
    )
    row = result.fetchone()
    if row is None:
        return None
    return {"work_order_number": row[0], "material_code": row[1]}


async def _load_current_operation(db: DbSession, work_order_number: str, sequence: int) -> dict | None:
    result = await db.execute(
        text(
            "SELECT operation_code, work_center_code, sop_id FROM production_work_order_operations "
            "WHERE work_order_number = :w AND sequence = :seq"
        ),
        {"w": work_order_number, "seq": sequence},
    )
    row = result.fetchone()
    if row is None:
        return None
    return {"operation_code": row[0], "work_center_code": row[1], "sop_id": row[2]}


async def _load_parameters(db: DbSession, material_code: str, operation_code: str) -> list:
    try:
        result = await db.execute(
            text(
                "SELECT code, unit_code, lower_limit, target_value, upper_limit, required "
                "FROM master_parameter_specifications "
                "WHERE material_code = :m AND operation_code = :op"
            ),
            {"m": material_code, "op": operation_code},
        )
        return [
            {
                "code": r[0],
                "unit": r[1] or "",
                "lowerLimit": _num(r[2]),
                "target": _num(r[3]),
                "upperLimit": _num(r[4]),
                "required": bool(r[5]),
            }
            for r in result.fetchall()
        ]
    except Exception:
        return []


async def _load_sop(db: DbSession, sop_id: str) -> dict | None:
    try:
        result = await db.execute(
            text(
                "SELECT sop_id, document_uri, version FROM master_sop_documents WHERE sop_id = :s"
            ),
            {"s": sop_id},
        )
        row = result.fetchone()
        if row is None:
            return None
        return {"sopId": row[0], "documentUri": row[1] or "", "version": row[2] or ""}
    except Exception:
        return None


async def _load_bom_materials(db: DbSession, material_code: str) -> list:
    try:
        result = await db.execute(
            text(
                "SELECT c.material_code, c.quantity_per, c.unit_code, m.material_name "
                "FROM master_bom_components c "
                "JOIN master_boms b ON b.bom_code = c.bom_code AND b.version = c.version "
                "JOIN master_materials m ON m.material_code = c.material_code "
                "WHERE b.material_code = :m AND b.status = 'EFFECTIVE'"
            ),
            {"m": material_code},
        )
        return [
            {
                "materialCode": r[0],
                "quantityPer": _num(r[1]),
                "unit": r[2] or "",
                "materialName": r[3] or "",
            }
            for r in result.fetchall()
        ]
    except Exception:
        return []


def _num(value) -> float | int | None:
    if value is None:
        return None
    return float(value)
