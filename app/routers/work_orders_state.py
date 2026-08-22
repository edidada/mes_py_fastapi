"""``POST /api/v1/work-orders/{workOrderNumber}/state`` 工单状态迁移。

按 C++ 固定状态机迁移；迁移到 ``RELEASED`` 时将所有 ``PENDING`` 工序改为 ``READY``，
并写审计、ERP outbox 与幂等记录。与 C++ 一致忽略事务内写语句及 commit 的返回值。
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

_ALLOWED_ROLES = {"PLANNER", "MES_SUPERVISOR", "MES_ADMIN"}

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "DRAFT": {"RELEASED", "CANCELLED"},
    "RELEASED": {"IN_PROGRESS", "SUSPENDED", "CANCELLED"},
    "SUSPENDED": {"RELEASED", "CANCELLED"},
    "IN_PROGRESS": {"SUSPENDED", "COMPLETED"},
    "COMPLETED": {"CLOSED"},
    "CLOSED": set(),
    "CANCELLED": set(),
}


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/{work_order_number}/state", status_code=200)
async def transition_work_order(request: Request, work_order_number: str, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "PLANNER required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"workOrderNumber": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    target_status = body.get("targetStatus")
    target = target_status if isinstance(target_status, str) else ""

    # 复刻 C++ 查询包装：查询错误也映射为 404
    try:
        wo = (
            await db.execute(
                text("SELECT status, version FROM production_work_orders WHERE work_order_number = :n"),
                {"n": work_order_number},
            )
        ).first()
    except Exception:  # noqa: BLE001
        raise MESError(404, "NOT_FOUND", "work order not found") from None
    if wo is None:
        raise MESError(404, "NOT_FOUND", "work order not found")

    current_status = wo[0] or ""
    if target not in _ALLOWED_TRANSITIONS.get(current_status, set()):
        raise MESError(409, "CONFLICT", f"invalid transition {current_status}->{target}")

    now = utc_now()
    try:
        await db.execute(
            text("UPDATE production_work_orders SET status = :t, updated_at = :at WHERE work_order_number = :n"),
            {"t": target, "at": now, "n": work_order_number},
        )
        if target == "RELEASED":
            await db.execute(
                text(
                    "UPDATE production_work_order_operations SET status = 'READY' "
                    "WHERE work_order_number = :n AND status = 'PENDING'"
                ),
                {"n": work_order_number},
            )
        await write_audit(
            db,
            action="WORK_ORDER_TRANSITION",
            resource_type="WORK_ORDER",
            resource_id=work_order_number,
            before={"from": current_status},
            after={"to": target},
            actor_id=get_actor(request),
        )
        await db.execute(
            text(
                "INSERT INTO integration_outbox_messages "
                "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, "
                "status, payload, created_at) "
                "VALUES (:id, 'WORK_ORDER', :aid, 'work_order.state.changed', 'ERP', "
                "'PENDING', :payload, :at)"
            ),
            {
                "id": gen_id("OUT"),
                "aid": work_order_number,
                "payload": json.dumps({"workOrderNumber": work_order_number, "status": target}, ensure_ascii=False),
                "at": now,
            },
        )
        await save_idempotent(db, idem_key, "200", work_order_number, "transitioned")
        await db.commit()
    except Exception:  # noqa: BLE001 - 与 C++ 一致忽略写失败
        await db.rollback()

    return ok({"workOrderNumber": work_order_number, "status": target}, corr)
