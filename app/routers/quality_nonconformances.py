"""``POST /api/v1/quality/nonconformances`` 创建不合格项与序列号隔离记录。

请求带非空 ``resourceCode`` 时把对应产品单元置为 ``HOLD``，并始终尝试创建
质量安灯、审计和幂等投影。仅 ``QUALITY_ENGINEER`` 或 ``MES_ADMIN`` 可调用。
与 C++ 一致：NC/隔离记录写入失败（如源检验缺失）时仍继续执行其余写入，
事务内 SQL 与 commit 失败不影响 HTTP 成功响应。
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
_DEFAULT_PLANT = "PLANT-A"
_DEFAULT_SEVERITY = "MAJOR"
_DEFAULT_ANDON_SEVERITY = "WARNING"
_DEFAULT_DISPOSITION = "OPEN"


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


def _as_number(value, default: float) -> float:
    """仅 JSON number 有效；省略、null、字符串或其他类型用默认值。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/nonconformances", status_code=200)
async def create_nonconformance(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "QUALITY_ENGINEER required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"nonconformanceId": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    plant_code = _as_str(body.get("plantCode")) or _DEFAULT_PLANT
    source_inspection_id = _as_str(body.get("sourceInspectionId"))
    severity = _as_str(body.get("severity")) or _DEFAULT_SEVERITY
    andon_severity = _as_str(body.get("severity")) or _DEFAULT_ANDON_SEVERITY
    defect_code = _as_str(body.get("defectCode"))
    disposition = _as_str(body.get("disposition")) or _DEFAULT_DISPOSITION
    affected_quantity = _as_number(body.get("affectedQuantity"), 1.0)
    resource_code = _as_str(body.get("resourceCode"))
    operator = get_actor(request)
    now = utc_now()

    nonconformance_id = gen_id("NC")
    try:
        # 1. NC：源检验外键；空/未知 ID 使写入失败（忽略）
        try:
            await db.execute(
                text(
                    "INSERT INTO quality_nonconformances "
                    "(nonconformance_id, source_inspection_id, plant_code, serial_number, work_order_number, "
                    " defect_code, severity, status, affected_qty, reported_by, reported_at) "
                    "VALUES (:id, :src, :plant, '', '', :defect, :sev, :disp, :qty, :op, :t)"
                ),
                {
                    "id": nonconformance_id,
                    "src": source_inspection_id or None,
                    "plant": plant_code,
                    "defect": defect_code,
                    "sev": severity,
                    "disp": disposition,
                    "qty": affected_quantity,
                    "op": operator,
                    "t": now,
                },
            )
        except Exception:
            pass
        # 2. 隔离记录：nonconference_id 拼写沿用 C++ schema，引用本次 NC
        try:
            await db.execute(
                text(
                    "INSERT INTO quality_quarantine_records "
                    "(quarantine_id, nonconference_id, resource_type, resource_code, operator_id, created_at) "
                    "VALUES (:id, :ncid, 'SERIAL_NUMBER', :rc, :op, :t)"
                ),
                {
                    "id": gen_id("QRT"),
                    "ncid": nonconformance_id,
                    "rc": resource_code,
                    "op": operator,
                    "t": now,
                },
            )
        except Exception:
            pass
        # 3. 非空资源代码：独立把同序列号产品置为 HOLD
        if resource_code:
            await db.execute(
                text(
                    "UPDATE production_product_units SET status = 'HOLD', updated_at = :t "
                    "WHERE serial_number = :s"
                ),
                {"t": now, "s": resource_code},
            )
        # 4. 质量安灯：工厂固定 PLANT-A、资源类型 NONCONFORMANCE、资源代码为 NC ID
        try:
            await db.execute(
                text(
                    "INSERT INTO trace_andon_events "
                    "(andon_id, plant_code, severity, category, resource_type, resource_code, "
                    " related_work_order_number, related_serial_number, message, raised_at) "
                    "VALUES (:id, 'PLANT-A', :sev, 'QUALITY', 'NONCONFORMANCE', :ncid, '', :rc, "
                    "        'nonconformance created', :t)"
                ),
                {
                    "id": gen_id("AND"),
                    "sev": andon_severity,
                    "ncid": nonconformance_id,
                    "rc": resource_code,
                    "t": now,
                },
            )
        except Exception:
            pass
        # 5. 审计 + 幂等
        await write_audit(
            db,
            action="NC_CREATE",
            resource_type="NONCONFORMANCE",
            resource_id=nonconformance_id,
            before={},
            after=body,
            actor_id=operator,
        )
        await save_idempotent(db, idem_key, "200", nonconformance_id, "nc created")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok({"nonconformanceId": nonconformance_id, "status": "OPEN"}, corr)
