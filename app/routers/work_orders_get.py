"""``GET /api/v1/work-orders/{workOrderNumber}`` 工单详情。

读取工单生产概要、路线工序快照与 WIP 数量。不要求角色与写请求 Header。
"""

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.contract import correlation_id, ok
from app.deps import DbSession
from app.errors import MESError

router = APIRouter()

_WIP_STATUSES = ("CREATED", "IN_PROCESS", "REWORK", "HOLD", "WAITING_INSPECTION")


@router.get("/{work_order_number}", status_code=200)
async def get_work_order(request: Request, work_order_number: str, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))

    # 复刻 SqliteStore::queryOne 语义：主查询数据库错误同样表现为 404
    try:
        wo = (
            await db.execute(
                text(
                    "SELECT work_order_number, plant_code, material_code, routing_code, "
                    "routing_version, status, priority, planned_quantity, "
                    "completed_quantity, rejected_quantity "
                    "FROM production_work_orders WHERE work_order_number = :n"
                ),
                {"n": work_order_number},
            )
        ).first()
    except Exception:  # noqa: BLE001
        raise MESError(404, "NOT_FOUND", "work order not found") from None
    if wo is None:
        raise MESError(404, "NOT_FOUND", "work order not found")

    # 工序快照：查询失败降级为空数组
    try:
        ops = await db.execute(
            text(
                "SELECT sequence, operation_code, work_center_code, quality_gate, "
                "allow_skip, standard_cycle_seconds, status "
                "FROM production_work_order_operations "
                "WHERE work_order_number = :n ORDER BY sequence"
            ),
            {"n": work_order_number},
        )
        operations = [
            {
                "sequence": r[0],
                "operationCode": r[1],
                "workCenterCode": r[2],
                "qualityGate": r[3] == 1,
                "allowSkip": r[4] == 1,
                "standardCycleSeconds": r[5],
                "status": r[6],
            }
            for r in ops.fetchall()
        ]
    except Exception:  # noqa: BLE001
        operations = []

    # WIP 数量：查询失败降级为 0
    wip_placeholders = ", ".join(f":ws{i}" for i in range(len(_WIP_STATUSES)))
    wip_params = {"n": work_order_number, **{f"ws{i}": s for i, s in enumerate(_WIP_STATUSES)}}
    try:
        wip = await db.execute(
            text(
                "SELECT COUNT(*) FROM production_product_units "
                f"WHERE work_order_number = :n AND status IN ({wip_placeholders})"
            ),
            wip_params,
        )
        wip_quantity = wip.scalar() or 0
    except Exception:  # noqa: BLE001
        wip_quantity = 0

    return ok(
        {
            "workOrderNumber": wo[0],
            "plantCode": wo[1],
            "materialCode": wo[2],
            "routingCode": wo[3],
            "routingVersion": wo[4],
            "status": wo[5],
            "priority": int(wo[6]) if wo[6] is not None else 0,
            "plannedQuantity": int(wo[7]) if wo[7] is not None else 0,
            "completedQuantity": int(wo[8]) if wo[8] is not None else 0,
            "rejectedQuantity": int(wo[9]) if wo[9] is not None else 0,
            "wipQuantity": wip_quantity,
            "operations": operations,
        },
        corr,
    )
