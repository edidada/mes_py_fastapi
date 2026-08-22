"""``POST /api/v1/quality/capas`` 创建纠正预防措施记录。

与 C++ 一致：``nonconformanceId`` 绑定空字符串（非 NULL），表列带 NOT NULL +
外键，因此空/未知 NC 会使 INSERT 失败并被忽略，但接口仍返回成功；
``ownerId``、``rootCause``、``correctiveAction``、``dueAt`` 请求字符串原样写入，
``status`` 固定 ``OPEN``，``closed_at`` 保持 NULL。仅 ``QUALITY_ENGINEER`` 或
``MES_ADMIN`` 可调用。
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


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/capas", status_code=200)
async def create_capa(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "QUALITY_ENGINEER required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"capaId": cached["resource_code"], "status": "OPEN", "cached": True}, corr)

    body = await _parse_body(request)
    plant_code = _as_str(body.get("plantCode")) or _DEFAULT_PLANT
    nonconformance_id = _as_str(body.get("nonconformanceId"))
    owner_id = _as_str(body.get("ownerId"))
    root_cause = _as_str(body.get("rootCause"))
    corrective_action = _as_str(body.get("correctiveAction"))
    due_at = _as_str(body.get("dueAt"))
    operator = get_actor(request)
    now = utc_now()

    capa_id = gen_id("CAPA")
    try:
        # 1. CAPA：nonconformance_id 绑定空字符串，空/未知 NC 使 INSERT 失败（忽略）
        try:
            await db.execute(
                text(
                    "INSERT INTO quality_capa_cases "
                    "(capa_id, plant_code, nonconformance_id, owner_id, root_cause, corrective_action, "
                    " status, due_at, created_at, created_by, closed_at) "
                    "VALUES (:id, :plant, :ncid, :owner, :cause, :action, 'OPEN', :due, :t, :op, NULL)"
                ),
                {
                    "id": capa_id,
                    "plant": plant_code,
                    "ncid": nonconformance_id,
                    "owner": owner_id,
                    "cause": root_cause,
                    "action": corrective_action,
                    "due": due_at,
                    "op": operator,
                    "t": now,
                },
            )
        except Exception:
            pass
        # 2. 审计 + 幂等
        await write_audit(
            db,
            action="CAPA_CREATE",
            resource_type="CAPA",
            resource_id=capa_id,
            before={},
            after=body,
            actor_id=operator,
        )
        await save_idempotent(db, idem_key, "200", capa_id, "capa created")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok({"capaId": capa_id, "status": "OPEN"}, corr)
