"""``POST /api/v1/andons`` 创建安灯事件。

不限制角色。未知 ``severity``（数据库 CHECK 约束外）会使安灯主记录 INSERT
失败，但审计、幂等记录与成功响应仍存在；``raised_count`` 汇总只更新当天已有
行，不存在时不创建。
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

_ALLOWED_SEVERITIES = {"INFO", "WARNING", "CRITICAL"}
_DEFAULT_PLANT = "PLANT-A"


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("", status_code=200)
async def create_andon(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"andonId": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    plant_code = _as_str(body.get("plantCode")) or _DEFAULT_PLANT
    severity = _as_str(body.get("severity")) or "WARNING"
    category = _as_str(body.get("category")) or "QUALITY"
    resource_type = _as_str(body.get("resourceType"))
    resource_code = _as_str(body.get("resourceCode"))
    related_wo = _as_str(body.get("relatedWorkOrderNumber"))
    related_serial = _as_str(body.get("relatedSerialNumber"))
    message = _as_str(body.get("message"))
    operator = get_actor(request)
    now = utc_now()

    andon_id = gen_id("AND")
    try:
        # 1. 安灯主记录（非法 severity 等价 C++ CHECK 约束失败 → INSERT 失败，忽略）
        if severity in _ALLOWED_SEVERITIES:
            try:
                await db.execute(
                    text(
                        "INSERT INTO trace_andon_events "
                        "(andon_id, plant_code, severity, category, resource_type, resource_code, "
                        " related_work_order_number, related_serial_number, message, raised_at) "
                        "VALUES (:id, :p, :sev, :cat, :rt, :rc, :wo, :sn, :msg, :t)"
                    ),
                    {
                        "id": andon_id,
                        "p": plant_code,
                        "sev": severity,
                        "cat": category,
                        "rt": resource_type,
                        "rc": resource_code,
                        "wo": related_wo,
                        "sn": related_serial,
                        "msg": message,
                        "t": now,
                    },
                )
            except Exception:
                pass
        # 2. 当天已有汇总行 raised_count + 1（不存在不创建）
        await db.execute(
            text(
                "UPDATE reporting_andon_summary SET raised_count = raised_count + 1 "
                "WHERE plant_code = :p AND summary_date = :d AND severity = :s"
            ),
            {"p": plant_code, "d": now[:10], "s": severity},
        )
        # 3. 审计 + 幂等
        await write_audit(
            db,
            action="ANDON_RAISE",
            resource_type="ANDON",
            resource_id=andon_id,
            before={},
            after=body,
            actor_id=operator,
        )
        await save_idempotent(db, idem_key, "200", andon_id, "raised")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok({"andonId": andon_id, "status": "ACTIVE", "raisedAt": now}, corr)
