"""``POST /api/v1/trace/impact-query`` 按物料批次反查受影响产品。

不校验角色。幂等命中发生在 JSON 与字段校验之前，返回 ``queryId``（实际为批次
号）。统计以消耗记录为行（无 DISTINCT），状态分类：``PASSED`` → completed、
``SCRAPPED`` → scrapped、其他（含 LEFT JOIN 产生的 ``UNKNOWN``）→ inProcess。
影响查询失败降级为空结果，审计/幂等写入错误忽略。
"""

import json

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.audit import write_audit
from app.auth import get_actor
from app.contract import correlation_id, ok
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/impact-query", status_code=200)
async def trace_impact_query(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    idem_key = await require_idempotency_key(request)
    try:
        cached = await find_idempotent(db, idem_key)
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None
    if cached is not None:
        return ok({"queryId": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    lot_number = _as_str(body.get("lotNumber"))
    if not lot_number:
        raise MESError(422, "VALIDATION_ERROR", "lotNumber required")
    operator = get_actor(request)

    # 影响查询（失败 → 空结果降级）
    try:
        rows = (
            await db.execute(
                text(
                    "SELECT mc.serial_number, pu.work_order_number, pu.status "
                    "FROM production_material_consumptions mc "
                    "LEFT JOIN production_product_units pu ON mc.serial_number = pu.serial_number "
                    "WHERE mc.lot_number = :lot"
                ),
                {"lot": lot_number},
            )
        ).fetchall()
    except Exception:
        rows = []

    completed = 0
    scrapped = 0
    in_process = 0
    units = []
    for r in rows:
        status = r[2] if r[2] is not None else "UNKNOWN"
        units.append(
            {
                "serialNumber": r[0],
                "workOrderNumber": r[1] if r[1] is not None else "",
                "unitStatus": status,
            }
        )
        if status == "PASSED":
            completed += 1
        elif status == "SCRAPPED":
            scrapped += 1
        else:
            in_process += 1

    affected_count = len(rows)
    if affected_count > 100:
        risk = "CRITICAL"
    elif affected_count > 10:
        risk = "WARNING"
    else:
        risk = "INFO"

    data = {
        "query": {"lotNumber": lot_number},
        "impactSummary": {
            "affectedUnitCount": affected_count,
            "inProcessCount": in_process,
            "completedCount": completed,
            "scrappedCount": scrapped,
            "riskLevel": risk,
        },
        "affectedUnits": units,
    }

    # 审计 + 幂等（不使用显式事务，写入错误忽略）
    try:
        await write_audit(
            db,
            action="TRACE_IMPACT_QUERY",
            resource_type="MATERIAL_LOT",
            resource_id=lot_number,
            before={},
            after=body,
            actor_id=operator,
        )
        await save_idempotent(db, idem_key, "200", lot_number, "impact")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok(data, corr)
