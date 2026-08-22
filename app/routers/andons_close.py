"""``POST /api/v1/andons/{andonId}/close`` 关闭安灯。

允许角色为 ``MES_SUPERVISOR``、``QUALITY_ENGINEER``、``EQUIPMENT_ENGINEER`` 或
``MES_ADMIN``（角色校验先于幂等）。C++ 不检查 ``closed_at IS NULL``，因此对
同一已关闭安灯使用新幂等键会再次成功并覆盖关闭时间/关闭人、再次累加
``closed_count``。
"""

import json

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.audit import write_audit
from app.auth import get_actor, require_role
from app.contract import correlation_id, ok, utc_now
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()

_ALLOWED_ROLES = {"MES_SUPERVISOR", "QUALITY_ENGINEER", "EQUIPMENT_ENGINEER", "MES_ADMIN"}


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/{andonId}/close", status_code=200)
async def close_andon(request: Request, andonId: str, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "supervisor role required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"andonId": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    operator = get_actor(request)

    # 读取安灯（查询错误与不存在都映射 404）
    try:
        row = (
            await db.execute(
                text(
                    "SELECT plant_code, category, severity FROM trace_andon_events "
                    "WHERE andon_id = :id"
                ),
                {"id": andonId},
            )
        ).first()
    except Exception:
        raise MESError(404, "NOT_FOUND", "andon not found") from None
    if row is None:
        raise MESError(404, "NOT_FOUND", "andon not found")
    plant_code = row[0] if row[0] is not None else "PLANT-A"
    severity = row[2] if row[2] is not None else ""

    now = utc_now()
    try:
        # 1. 更新关闭时间/关闭人（不检查是否已关闭）
        await db.execute(
            text(
                "UPDATE trace_andon_events SET closed_at = :t, closed_by = :op "
                "WHERE andon_id = :id"
            ),
            {"t": now, "op": operator, "id": andonId},
        )
        # 2. 当天已有汇总行 closed_count + 1（不存在不创建）
        await db.execute(
            text(
                "UPDATE reporting_andon_summary SET closed_count = closed_count + 1 "
                "WHERE plant_code = :p AND summary_date = :d AND severity = :s"
            ),
            {"p": plant_code, "d": now[:10], "s": severity},
        )
        # 3. 审计 + 幂等
        await write_audit(
            db,
            action="ANDON_CLOSE",
            resource_type="ANDON",
            resource_id=andonId,
            before={},
            after=body,
            actor_id=operator,
        )
        await save_idempotent(db, idem_key, "200", andonId, "closed")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok({"andonId": andonId, "status": "CLOSED", "closedAt": now}, corr)
