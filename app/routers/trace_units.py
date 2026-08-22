"""``GET /api/v1/trace/units/{serialNumber}`` 产品单元正向追溯视图。

不做角色/认证校验，不写数据库。产品主查询失败或不存在返回 ``404 serial not
found``；执行事件、参数、消耗或检验子查询失败按 C++ ``queryAll`` 行为映射为
空数组。``routingVersion`` 取工单 ``routing_code`` 列（C++ 查询但响应只输出
``routingVersion`` 名称）。
"""

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.contract import correlation_id, ok
from app.deps import DbSession
from app.errors import MESError

router = APIRouter()


@router.get("/units/{serialNumber}", status_code=200)
async def trace_unit(request: Request, serialNumber: str, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))

    # 1. 产品主查询（失败或不存在 → 404）
    try:
        row = (
            await db.execute(
                text(
                    "SELECT pu.work_order_number, wo.routing_code, pu.status, wo.material_code "
                    "FROM production_product_units pu "
                    "LEFT JOIN production_work_orders wo "
                    "  ON pu.work_order_number = wo.work_order_number "
                    "WHERE pu.serial_number = :sn"
                ),
                {"sn": serialNumber},
            )
        ).first()
    except Exception:
        raise MESError(404, "NOT_FOUND", "serial not found") from None
    if row is None:
        raise MESError(404, "NOT_FOUND", "serial not found")

    product = {
        "serialNumber": serialNumber,
        "workOrderNumber": row[0] if row[0] is not None else "",
        "materialCode": row[3] if row[3] is not None else "",
        "routingVersion": row[1] if row[1] is not None else "",
        "status": row[2] if row[2] is not None else "",
    }

    # 2. 执行事件（按 occurred_at 升序；失败 → 空数组）
    async def _operations():
        events = (
            await db.execute(
                text(
                    "SELECT operation_sequence, operation_code, station_code, equipment_code, "
                    "       operator_id, event_type, occurred_at "
                    "FROM production_execution_events WHERE serial_number = :sn "
                    "ORDER BY occurred_at ASC"
                ),
                {"sn": serialNumber},
            )
        ).fetchall()

        async def _params(seq):
            return [
                {
                    "code": p[0],
                    "value": p[1],
                    "lowerLimit": p[2],
                    "upperLimit": p[3],
                    "inSpec": p[4] == 1,
                    "recordedAt": p[5],
                }
                for p in (
                    await db.execute(
                        text(
                            "SELECT code, value, lower_limit, upper_limit, in_spec, recorded_at "
                            "FROM production_parameter_records "
                            "WHERE serial_number = :sn AND operation_sequence = :seq "
                            "ORDER BY recorded_at ASC"
                        ),
                        {"sn": serialNumber, "seq": seq},
                    )
                ).fetchall()
            ]

        items = []
        for e in events:
            try:
                params = await _params(e[0])
            except Exception:
                params = []
            items.append(
                {
                    "sequence": e[0],
                    "operationCode": e[1] if e[1] is not None else "",
                    "stationCode": e[2] if e[2] is not None else "",
                    "equipmentCode": e[3] if e[3] is not None else "",
                    "operatorId": e[4] if e[4] is not None else "",
                    "eventType": e[5],
                    "occurredAt": e[6],
                    "parameters": params,
                }
            )
        return items

    async def _consumptions():
        return [
            {
                "materialCode": c[0],
                "lotNumber": c[1] if c[1] is not None else "",
                "quantity": c[2],
                "consumedAt": c[3],
            }
            for c in (
                await db.execute(
                    text(
                        "SELECT material_code, lot_number, quantity, consumed_at "
                        "FROM production_material_consumptions WHERE serial_number = :sn "
                        "ORDER BY consumed_at ASC"
                    ),
                    {"sn": serialNumber},
                )
            ).fetchall()
        ]

    async def _inspections():
        return [
            {
                "planCode": i[0] if i[0] is not None else "",
                "operationSequence": i[1],
                "disposition": i[2] if i[2] is not None else "",
                "defectCode": i[3] if i[3] is not None else "",
                "inspectorId": i[4] if i[4] is not None else "",
                "inspectedAt": i[5],
            }
            for i in (
                await db.execute(
                    text(
                        "SELECT plan_code, operation_sequence, disposition, defect_code, "
                        "       inspector_id, inspected_at "
                        "FROM quality_inspection_results WHERE serial_number = :sn "
                        "ORDER BY inspected_at ASC"
                    ),
                    {"sn": serialNumber},
                )
            ).fetchall()
        ]

    async def _safe(fn):
        try:
            return await fn()
        except Exception:
            return []

    operations = await _safe(_operations)
    consumptions = await _safe(_consumptions)
    inspections = await _safe(_inspections)

    return ok(
        {
            "product": product,
            "operations": operations,
            "materialConsumptions": consumptions,
            "inspections": inspections,
        },
        corr,
    )
