"""``POST /api/v1/executions/scrap`` 创建报废记录并置产品单元为 SCRAPPED。

请求包含非空工单号时，增加工单拒收数并写入发往 ERP 的报废 outbox；无论
是否包含工单号都记录追溯、审计与幂等投影。接口不检查角色、序列号存在、
当前状态或工单匹配。与 C++ 一致：第 5 步刻意保留 12 列 / 11 个值的错误
INSERT 并被忽略，因此当前不会写入 SCRAP 执行事件；事务内其余写入与
commit 失败也不影响 HTTP 成功响应。
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

_DISPOSE = "DISPOSE"


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


def _parse_disposition(value) -> str:
    """省略、空字符串、null 或非字符串回退 DISPOSE，其他原样保存。"""
    if not isinstance(value, str) or value == "":
        return _DISPOSE
    return value


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("/scrap", status_code=200)
async def scrap_execution(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"serialNumber": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    serial_number = body.get("serialNumber")
    if not isinstance(serial_number, str) or not serial_number:
        raise MESError(422, "VALIDATION_ERROR", "serialNumber must be a non-empty string")

    scrap_code = _as_str(body.get("scrapCode"))
    disposition = _parse_disposition(body.get("disposition"))
    work_order_number = _as_str(body.get("workOrderNumber"))
    operator = get_actor(request)
    now = utc_now()
    has_work_order = bool(work_order_number)

    try:
        # 1. 报废记录（不对序列号/工单建外键）
        await db.execute(
            text(
                "INSERT INTO production_scrap_records "
                "(scrap_id, serial_number, scrap_code, disposition, work_order_number, operator_id, created_at) "
                "VALUES (:id, :s, :code, :disp, :w, :op, :t)"
            ),
            {
                "id": gen_id("SCR"),
                "s": serial_number,
                "code": scrap_code,
                "disp": disposition,
                "w": work_order_number,
                "op": operator,
                "t": now,
            },
        )
        # 2. 单元置为 SCRAPPED + 完成时间；工序序号和站点保持不变
        await db.execute(
            text(
                "UPDATE production_product_units SET status = 'SCRAPPED', completed_at = :t "
                "WHERE serial_number = :s"
            ),
            {"t": now, "s": serial_number},
        )
        # 3. 工单非空：拒收数 +1（工单不存在时更新零行）
        if has_work_order:
            await db.execute(
                text(
                    "UPDATE production_work_orders SET rejected_quantity = rejected_quantity + 1 "
                    "WHERE work_order_number = :w"
                ),
                {"w": work_order_number},
            )
        # 4. 与 C++ 一致：12 列 / 11 个占位符的 SCRAP event，SQLite 拒绝后忽略
        try:
            await db.execute(
                text(
                    "INSERT INTO production_execution_events "
                    "(event_id, serial_number, work_order_number, operation_sequence, operation_code, "
                    " station_code, equipment_code, operator_id, event_type, occurred_at, plant_code, correlation_id) "
                    "VALUES (:id, :s, :w, :seq, :code, :st, :eq, :actor, 'SCRAP', :t, 'PLANT-A')"
                ),
                {
                    "id": gen_id("EVT"),
                    "s": serial_number,
                    "w": work_order_number,
                    "seq": 0,
                    "code": scrap_code,
                    "st": "",
                    "eq": "",
                    "actor": operator,
                    "t": now,
                },
            )
        except Exception:
            pass
        # 5. 工单非空：ERP 报废 outbox
        if has_work_order:
            await db.execute(
                text(
                    "INSERT INTO integration_outbox_messages "
                    "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, status, payload, created_at) "
                    "VALUES (:id, 'WORK_ORDER', :w, 'work_order.scrap', 'ERP', 'PENDING', :payload, :t)"
                ),
                {
                    "id": gen_id("OUT"),
                    "w": work_order_number,
                    "payload": json.dumps(
                        {"serialNumber": serial_number, "scrapCode": scrap_code},
                        ensure_ascii=False,
                    ),
                    "t": now,
                },
            )
        # 6. 追溯：SCRAP / SERIAL_NUMBER，工厂固定 PLANT-A
        await db.execute(
            text(
                "INSERT INTO trace_events "
                "(event_id, event_type, resource_type, resource_code, related_work_order_number, "
                " related_serial_number, message, operator_id, occurred_at, correlation_id, plant_code) "
                "VALUES (:id, 'SCRAP', 'SERIAL_NUMBER', :s, :w, :s, 'scrapped', :op, :t, :corr, 'PLANT-A')"
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
        # 7. 审计
        await write_audit(
            db,
            action="SCRAP",
            resource_type="PRODUCT_UNIT",
            resource_id=serial_number,
            before={},
            after=body,
            actor_id=operator,
        )
        await save_idempotent(db, idem_key, "200", serial_number, "scrapped")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok({"serialNumber": serial_number, "status": "SCRAPPED"}, corr)
