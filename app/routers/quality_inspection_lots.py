"""``POST /api/v1/quality/inspection-lots`` 创建 PENDING 检验批。

按物料匹配最新生效检验计划，读取抽样规则数量（无规则时为 1）。仅
``QUALITY_ENGINEER`` 或 ``MES_ADMIN`` 可调用，且角色校验先于幂等查询。
与 C++ 一致：事务开始后的 SQL/commit 返回值不影响最终响应。
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


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


def _parse_inspection_type(value) -> str:
    """省略、空、null 或非字符串使用 PATROL；其他非空字符串原样保存。"""
    if not isinstance(value, str) or value == "":
        return "PATROL"
    return value


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/inspection-lots", status_code=200)
async def create_inspection_lot(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "QUALITY_ENGINEER required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"lotId": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    material_code = _as_str(body.get("materialCode"))
    plant_code = _as_str(body.get("plantCode")) or "PLANT-A"
    work_order_number = _as_str(body.get("workOrderNumber"))
    serial_number = _as_str(body.get("serialNumber"))
    inspection_type = _parse_inspection_type(body.get("inspectionType"))
    operator = get_actor(request)
    now = utc_now()

    # 匹配最新生效检验计划；无计划或查询失败按 C++ queryOne 语义 → 404
    try:
        result = await db.execute(
            text(
                "SELECT plan_code, version FROM quality_inspection_plans "
                "WHERE material_code = :m AND status = 'EFFECTIVE' "
                "ORDER BY effective_from DESC LIMIT 1"
            ),
            {"m": material_code},
        )
        plan = result.fetchone()
    except Exception as exc:
        raise MESError(404, "NOT_FOUND", "inspection plan not found") from exc
    if plan is None:
        raise MESError(404, "NOT_FOUND", "inspection plan not found")

    plan_code, plan_version = plan[0] or "", plan[1] or ""
    sample_size = await _load_sample_size(db, plan_code, plan_version)

    lot_id = gen_id("ILOT")
    try:
        await db.execute(
            text(
                "INSERT INTO quality_inspection_lots "
                "(lot_id, plant_code, material_code, work_order_number, serial_number, "
                " inspection_type, plan_code, plan_version, sample_size, status, operator_id, created_at) "
                "VALUES (:id, :p, :m, :w, :s, :itype, :pc, :pv, :ss, 'PENDING', :op, :t)"
            ),
            {
                "id": lot_id,
                "p": plant_code,
                "m": material_code,
                "w": work_order_number,
                "s": serial_number,
                "itype": inspection_type,
                "pc": plan_code,
                "pv": plan_version,
                "ss": sample_size,
                "op": operator,
                "t": now,
            },
        )
        await write_audit(
            db,
            action="INSPECTION_LOT_CREATE",
            resource_type="INSPECTION_LOT",
            resource_id=lot_id,
            before={},
            after=body,
            actor_id=operator,
        )
        await save_idempotent(db, idem_key, "200", lot_id, "lot created")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok({"lotId": lot_id, "status": "PENDING", "sampleSize": sample_size}, corr)


async def _load_sample_size(db: DbSession, plan_code: str, plan_version: str) -> int:
    """取该计划代码/版本的第一条抽样规则数量；没有规则时为 1。"""
    try:
        result = await db.execute(
            text(
                "SELECT sample_size FROM quality_sampling_rules "
                "WHERE plan_code = :pc AND version = :pv LIMIT 1"
            ),
            {"pc": plan_code, "pv": plan_version},
        )
        row = result.fetchone()
    except Exception:
        return 1
    if row is None or row[0] is None:
        return 1
    return int(row[0])
