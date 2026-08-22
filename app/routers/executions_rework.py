"""``POST /api/v1/executions/rework`` 创建返工记录并移动单元到目标工序。

接口不检查角色、序列号是否存在、当前状态是否允许返工或目标工序是否属于
工单（与 C++ 处理器一致）。与 C++ 一致：第 5 步刻意保留 11 列 / 10 个
值的错误 INSERT，SQLite 拒绝后忽略，因此该 URL 当前不会写入 REWORK
执行事件；事务内其余写入与 commit 失败也不影响 HTTP 成功响应。
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


def _parse_sequence(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            return int(float(value)) if value.strip() else 0
    except (ValueError, TypeError):
        pass
    return 0


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/rework", status_code=200)
async def rework_execution(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"serialNumber": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    serial_number = body.get("serialNumber")
    if not isinstance(serial_number, str) or not serial_number:
        raise MESError(422, "VALIDATION_ERROR", "serialNumber must be a non-empty string")

    target_sequence = _parse_sequence(body.get("targetOperationSequence"))
    reason_code = _as_str(body.get("reasonCode"))
    approved_by = _as_str(body.get("approvedBy"))
    operator = get_actor(request)
    now = utc_now()

    try:
        # 1. 返工记录（不对序列号建外键，未知序列号也可留下记录）
        await db.execute(
            text(
                "INSERT INTO production_rework_orders "
                "(rework_id, serial_number, target_operation_sequence, reason_code, approved_by, created_at) "
                "VALUES (:id, :s, :seq, :reason, :approved, :t)"
            ),
            {
                "id": gen_id("RW"),
                "s": serial_number,
                "seq": target_sequence,
                "reason": reason_code,
                "approved": approved_by,
                "t": now,
            },
        )
        # 2. 移动单元：REWORK + 目标工序；当前站点/时间保持不变
        await db.execute(
            text(
                "UPDATE production_product_units "
                "SET status = 'REWORK', current_operation_sequence = :seq "
                "WHERE serial_number = :s"
            ),
            {"seq": target_sequence, "s": serial_number},
        )
        # 3. 读回工单号，查不到用空串
        work_order_number = ""
        try:
            result = await db.execute(
                text("SELECT work_order_number FROM production_product_units WHERE serial_number = :s"),
                {"s": serial_number},
            )
            row = result.fetchone()
            if row is not None:
                work_order_number = row[0] or ""
        except Exception:
            pass
        # 4. 与 C++ 一致：11 列 / 10 个占位符的 REWORK execution event，SQLite 拒绝后忽略
        try:
            await db.execute(
                text(
                    "INSERT INTO production_execution_events "
                    "(event_id, serial_number, work_order_number, operation_sequence, operation_code, "
                    " station_code, equipment_code, operator_id, event_type, occurred_at, plant_code) "
                    "VALUES (:id, :s, :w, :seq, :op, :st, :eq, :actor, 'REWORK', :t)"
                ),
                {
                    "id": gen_id("EVT"),
                    "s": serial_number,
                    "w": work_order_number,
                    "seq": target_sequence,
                    "op": "",
                    "st": "",
                    "eq": "",
                    "actor": operator,
                    "t": now,
                },
            )
        except Exception:
            pass
        # 5. 追溯：REWORK / SERIAL_NUMBER，工厂固定 PLANT-A
        await db.execute(
            text(
                "INSERT INTO trace_events "
                "(event_id, event_type, resource_type, resource_code, related_work_order_number, "
                " related_serial_number, message, operator_id, occurred_at, correlation_id, plant_code) "
                "VALUES (:id, 'REWORK', 'SERIAL_NUMBER', :s, :w, :s, 'rework', :op, :t, :corr, 'PLANT-A')"
            ),
            {
                "id": gen_id("EVT"),
                "s": serial_number,
                "w": work_order_number,
                "op": operator,
                "t": now,
                "corr": corr,
            },
        )
        # 6. 审计
        await write_audit(
            db,
            action="REWORK",
            resource_type="PRODUCT_UNIT",
            resource_id=serial_number,
            before={},
            after=body,
            actor_id=operator,
        )
        await save_idempotent(db, idem_key, "200", serial_number, "rework")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok(
        {
            "serialNumber": serial_number,
            "status": "REWORK",
            "targetOperationSequence": target_sequence,
        },
        corr,
    )
