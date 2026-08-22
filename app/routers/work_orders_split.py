"""工单拆分与合并端点。

实现文档中的：
- ``POST /api/v1/work-orders/{id}/split`` 工单拆分（2709）
- ``POST /api/v1/work-orders/{id}/merge`` 工单合并（2731）

拆分：原工单须为 ``RELEASED`` / ``IN_PROGRESS``，按 ``splitQuantities`` 生成独立子工单
（复制工序快照），原工单保留，谱系写入 ``production_work_order_lineage``。
合并：各源工单须为 ``COMPLETED`` / ``CLOSED``，新建合并工单
（``plannedQuantity`` 为各源之和），谱系写入 ``production_work_order_lineage``。
角色 ``PLANNER`` / ``MES_SUPERVISOR`` / ``MES_ADMIN`` 可调用；写审计、ERP outbox 与幂等记录。
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

_ALLOWED_ROLES = {"PLANNER", "MES_SUPERVISOR", "MES_ADMIN"}


def _to_str(value) -> str:
    return value if isinstance(value, str) else ""


async def _copy_operations(db, source_wo: str, target_wo: str) -> None:
    """复制源工单的工序快照到目标工单。"""

    ops = (
        await db.execute(
            text(
                "SELECT sequence, operation_code, work_center_code, quality_gate, "
                "allow_skip, standard_cycle_seconds, status "
                "FROM production_work_order_operations WHERE work_order_number = :wo ORDER BY sequence"
            ),
            {"wo": source_wo},
        )
    ).fetchall()
    for op in ops:
        try:
            await db.execute(
                text(
                    "INSERT INTO production_work_order_operations "
                    "(work_order_number, sequence, operation_code, work_center_code, "
                    "quality_gate, allow_skip, standard_cycle_seconds, status) "
                    "VALUES (:wo, :seq, :op, :wc, :qg, :skip, :std, :st)"
                ),
                {
                    "wo": target_wo,
                    "seq": op[0],
                    "op": op[1],
                    "wc": op[2],
                    "qg": op[3],
                    "skip": op[4],
                    "std": op[5],
                    "st": op[6] or "PENDING",
                },
            )
        except IntegrityError:
            pass


async def _write_lineage(db, source: str, target: str, event_type: str, quantity, now: str) -> None:
    await db.execute(
        text(
            "INSERT INTO production_work_order_lineage "
            "(source_work_order_number, target_work_order_number, event_type, quantity, occurred_at) "
            "VALUES (:s, :t, :e, :q, :at)"
        ),
        {"s": source, "t": target, "e": event_type, "q": quantity, "at": now},
    )


@router.post("/{work_order_id}/split", status_code=200)
async def split_work_order(work_order_id: str, request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "PLANNER required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"workOrderNumber": cached["resource_code"], "cached": True}, corr)

    body = await request.json() if (await request.body()) else {}
    body = body if isinstance(body, dict) else {}
    quantities = body.get("splitQuantities")
    if not isinstance(quantities, list) or not quantities:
        raise MESError(422, "VALIDATION_ERROR", "splitQuantities required")

    wo = (
        await db.execute(
            text(
                "SELECT plant_code, material_code, routing_code, routing_version, bom_code, "
                "bom_version, priority, status, planned_quantity, planned_start_at, planned_end_at "
                "FROM production_work_orders WHERE work_order_number = :n"
            ),
            {"n": work_order_id},
        )
    ).first()
    if wo is None:
        raise MESError(404, "NOT_FOUND", "work order not found")
    if wo[7] not in ("RELEASED", "IN_PROGRESS"):
        raise MESError(409, "CONFLICT", "work order not in splittable state")
    for q in quantities:
        try:
            if float(q) <= 0:
                raise MESError(409, "CONFLICT", "split quantity must be > 0")
        except (TypeError, ValueError):
            raise MESError(422, "VALIDATION_ERROR", "split quantity invalid") from None

    children = []
    now = utc_now()
    base = work_order_id
    idx = 1
    try:
        for q in quantities:
            child_no = f"{base}-{idx}"
            idx += 1
            await db.execute(
                text(
                    "INSERT INTO production_work_orders "
                    "(work_order_number, plant_code, material_code, routing_code, routing_version, "
                    "bom_code, bom_version, priority, status, planned_quantity, "
                    "planned_start_at, planned_end_at) "
                    "VALUES (:wo, :plant, :mat, :rc, :rv, :bc, :bv, :pri, 'DRAFT', :qty, :start, :end)"
                ),
                {
                    "wo": child_no,
                    "plant": wo[0],
                    "mat": wo[1],
                    "rc": wo[2],
                    "rv": wo[3],
                    "bc": wo[4],
                    "bv": wo[5],
                    "pri": wo[6],
                    "qty": float(q),
                    "start": wo[9],
                    "end": wo[10],
                },
            )
            await _copy_operations(db, work_order_id, child_no)
            await _write_lineage(db, work_order_id, child_no, "SPLIT", float(q), now)
            children.append(child_no)

        await write_audit(
            db,
            action="WORK_ORDER_SPLIT",
            resource_type="WORK_ORDER",
            resource_id=work_order_id,
            before={},
            after={"children": children},
            actor_id=get_actor(request),
        )
        await db.execute(
            text(
                "INSERT INTO integration_outbox_messages "
                "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, "
                "status, payload, created_at) "
                "VALUES (:id, 'WORK_ORDER', :aid, 'work_order.split', 'ERP', "
                "'PENDING', :payload, :at)"
            ),
            {
                "id": gen_id("OUT"),
                "aid": work_order_id,
                "payload": json.dumps({"workOrderNumber": work_order_id, "children": children}, ensure_ascii=False),
                "at": now,
            },
        )
        await save_idempotent(db, idem_key, "200", work_order_id, "split")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        raise MESError(409, "CONFLICT", str(exc.orig)) from exc
    except Exception:  # noqa: BLE001 - 与 C++ 一致忽略写失败
        await db.rollback()

    return ok({"workOrderNumber": work_order_id, "children": children}, corr)


@router.post("/{work_order_id}/merge", status_code=200)
async def merge_work_orders(work_order_id: str, request: Request, db: DbSession) -> dict:
    # 注意：文档合并端点路径为 /work-orders/{id}/merge，{id} 为合并后工单号（由调用方指定）
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "PLANNER required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"workOrderNumber": cached["resource_code"], "cached": True}, corr)

    body = await request.json() if (await request.body()) else {}
    body = body if isinstance(body, dict) else {}
    sources = body.get("sourceOrderNumbers")
    if not isinstance(sources, list) or not sources:
        raise MESError(422, "VALIDATION_ERROR", "sourceOrderNumbers required")

    total_qty = 0.0
    merged_plant = None
    merged_material = None
    merged_routing = None
    merged_rv = None
    for src in sources:
        src = _to_str(src)
        if not src:
            raise MESError(422, "VALIDATION_ERROR", "source order number required")
        s = (
            await db.execute(
                text(
                    "SELECT plant_code, material_code, routing_code, routing_version, status, "
                    "planned_quantity FROM production_work_orders WHERE work_order_number = :n"
                ),
                {"n": src},
            )
        ).first()
        if s is None:
            raise MESError(404, "NOT_FOUND", f"source work order not found: {src}")
        if s[4] not in ("COMPLETED", "CLOSED"):
            raise MESError(409, "CONFLICT", f"source not completed: {src}")
        total_qty += float(s[5] or 0)
        merged_plant = merged_plant or s[0]
        merged_material = merged_material or s[1]
        merged_routing = merged_routing or s[2]
        merged_rv = merged_rv or s[3]

    if not work_order_id:
        raise MESError(422, "VALIDATION_ERROR", "merged work order number required")
    now = utc_now()
    try:
        await db.execute(
            text(
                "INSERT INTO production_work_orders "
                "(work_order_number, plant_code, material_code, routing_code, routing_version, "
                "priority, status, planned_quantity) "
                "VALUES (:wo, :plant, :mat, :rc, :rv, 0, 'DRAFT', :qty)"
            ),
            {
                "wo": work_order_id,
                "plant": merged_plant,
                "mat": merged_material,
                "rc": merged_routing,
                "rv": merged_rv,
                "qty": total_qty,
            },
        )
        for src in sources:
            src = _to_str(src)
            await _write_lineage(db, src, work_order_id, "MERGED", None, now)

        await write_audit(
            db,
            action="WORK_ORDER_MERGE",
            resource_type="WORK_ORDER",
            resource_id=work_order_id,
            before={},
            after={"sources": sources, "plannedQuantity": total_qty},
            actor_id=get_actor(request),
        )
        await db.execute(
            text(
                "INSERT INTO integration_outbox_messages "
                "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, "
                "status, payload, created_at) "
                "VALUES (:id, 'WORK_ORDER', :aid, 'work_order.merged', 'ERP', "
                "'PENDING', :payload, :at)"
            ),
            {
                "id": gen_id("OUT"),
                "aid": work_order_id,
                "payload": json.dumps(
                    {"workOrderNumber": work_order_id, "sources": sources, "plannedQuantity": total_qty},
                    ensure_ascii=False,
                ),
                "at": now,
            },
        )
        await save_idempotent(db, idem_key, "200", work_order_id, "merged")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        raise MESError(409, "CONFLICT", str(exc.orig)) from exc
    except Exception:  # noqa: BLE001 - 与 C++ 一致忽略写失败
        await db.rollback()

    return ok({"workOrderNumber": work_order_id, "plannedQuantity": total_qty, "sources": sources}, corr)
