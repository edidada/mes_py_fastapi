"""``POST /api/v1/work-orders/{workOrderNumber}/dispatch`` 工序派工。

创建一条 ``DISPATCHED`` 派工记录并尝试同步更新已有工序任务。
仅 ``MES_SUPERVISOR`` / ``MES_ADMIN`` 可调用；插入派工记录是唯一检查执行结果的写入，
其余写语句及提交的返回值与 C++ 一致被忽略。
"""

import json

from fastapi import APIRouter, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.audit import write_audit
from app.auth import get_actor, require_role
from app.contract import correlation_id, gen_id, ok, utc_now
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()

_ALLOWED_ROLES = {"MES_SUPERVISOR", "MES_ADMIN"}


def _to_int(value) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _to_str(value) -> str:
    return value if isinstance(value, str) else ""


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/{work_order_number}/dispatch", status_code=200)
async def dispatch_work_order(request: Request, work_order_number: str, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "MES_SUPERVISOR required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"assignmentId": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    operation_sequence = _to_int(body.get("operationSequence"))
    station_code = _to_str(body.get("stationCode"))
    worker_id = _to_str(body.get("workerId"))
    shift_code = _to_str(body.get("shiftCode"))
    priority = _to_int(body.get("priority"))
    scheduled_start_at = _to_str(body.get("scheduledStartAt"))
    now = utc_now()
    assignment_id = gen_id("DSP")

    # 唯一检查执行结果的写入：违反工单/站点/员工外键等约束 → 422，消息保留 SQLite 错误
    try:
        await db.execute(
            text(
                "INSERT INTO production_dispatch_assignments "
                "(assignment_id, work_order_number, operation_sequence, station_code, worker_id, "
                "shift_code, status, priority, scheduled_start_at, created_at) "
                "VALUES (:id, :wo, :seq, :station, :worker, :shift, 'DISPATCHED', :pri, :start, :at)"
            ),
            {
                "id": assignment_id,
                "wo": work_order_number,
                "seq": operation_sequence,
                "station": station_code,
                "worker": worker_id,
                "shift": shift_code,
                "pri": priority,
                "start": scheduled_start_at,
                "at": now,
            },
        )

        # 以下写语句及提交的返回值不参与成败判断
        await db.execute(
            text(
                "UPDATE production_operation_tasks SET station_code = :station, "
                "worker_id = :worker, status = 'DISPATCHED' "
                "WHERE work_order_number = :wo AND operation_sequence = :seq"
            ),
            {"station": station_code, "worker": worker_id, "wo": work_order_number, "seq": operation_sequence},
        )
        await write_audit(
            db,
            action="DISPATCH",
            resource_type="WORK_ORDER",
            resource_id=work_order_number,
            before={},
            after=body,
            actor_id=get_actor(request),
        )
        await save_idempotent(db, idem_key, "200", assignment_id, "dispatched")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        raise MESError(422, "VALIDATION_ERROR", str(exc.orig)) from exc

    return ok({"assignmentId": assignment_id, "status": "DISPATCHED"}, corr)
