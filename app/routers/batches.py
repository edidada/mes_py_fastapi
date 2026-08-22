"""批次查询与状态、投料、参数、处置、EBR 端点。

实现文档中的：
- ``GET /api/v1/batches`` 批次列表（2535）
- ``GET /api/v1/batches/{id}`` 批次详情（2545）
- ``POST /api/v1/batches/{id}/state`` 批次状态迁移（2551）

列表支持可选 ``status`` 过滤；详情返回 ``production_batches`` 整行，
字段名映射遵循文档契约（``currentQty`` → ``actual_quantity``，
``targetQty`` → ``planned_quantity``，``qaStatus`` → ``quality_disposition``，
``materialCode`` → ``product_material_code``，``uom`` → ``unit_code``）。
状态码遵循 C++ 固定枚举（上限见 ``_STATUS_CAP``）。
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

# production_batches.status 固定枚举上限，与 C++ 一致截断超长值
_STATUS_CAP = (
    "CREATED",
    "RELEASED",
    "IN_PROGRESS",
    "WAITING_QA",
    "QA_PASSED",
    "QA_FAILED",
    "COMPLETED",
    "CLOSED",
    "CANCELLED",
)

# 固定状态机迁移（文档 2562-2590）；* 可到 COMPLETED / CANCELLED（需 qa_status）
_STATE_GRAPH: dict[str, set[str]] = {
    "CREATED": {"RELEASED"},
    "RELEASED": {"IN_PROGRESS"},
    "IN_PROGRESS": {"WAITING_QA"},
    "WAITING_QA": {"QA_PASSED", "QA_FAILED"},
    "QA_PASSED": {"CLOSED"},
    "QA_FAILED": set(),
    "COMPLETED": set(),
    "CLOSED": set(),
    "CANCELLED": set(),
}
_TERMINAL = {"COMPLETED", "CLOSED", "CANCELLED", "QA_FAILED"}

_ALLOWED_ROLES = {"MES_SUPERVISOR", "QA_INSPECTOR", "MES_ADMIN"}


def _cap_status(value: str | None) -> str | None:
    if value is None:
        return None
    return value if value in _STATUS_CAP else _STATUS_CAP[-1]


def _row_to_dict(r) -> dict:
    return {
        "plantCode": r[0],
        "batchNumber": r[1],
        "workOrderNumber": None,
        "materialCode": r[2],
        "status": _cap_status(r[3]),
        "currentQty": r[4],
        "targetQty": r[5],
        "qaStatus": r[6],
        "uom": r[7],
        "createdAt": r[8],
        "updatedAt": r[9],
    }


_SQL_COLS = (
    "plant_code, batch_number, product_material_code, status, "
    "actual_quantity, planned_quantity, quality_disposition, unit_code, "
    "created_at, updated_at"
)


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.get("/batches", status_code=200)
async def list_batches(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    status = request.query_params.get("status")
    params: dict = {}
    sql = f"SELECT {_SQL_COLS} FROM production_batches"
    if status:
        sql += " WHERE status = :st"
        params["st"] = status
    sql += " ORDER BY updated_at DESC"
    rows = (await db.execute(text(sql), params)).fetchall()
    items = [_row_to_dict(r) for r in rows]
    return ok({"batches": items, "total": len(items)}, corr)


@router.get("/batches/{batch_id}", status_code=200)
async def get_batch(batch_id: str, request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    row = (
        await db.execute(
            text(f"SELECT {_SQL_COLS} FROM production_batches WHERE batch_number = :bid"),
            {"bid": batch_id},
        )
    ).first()
    if row is None:
        raise MESError(404, "NOT_FOUND", "batch not found")
    return ok(_row_to_dict(row), corr)


@router.post("/batches/{batch_id}/state", status_code=200)
async def transition_batch(batch_id: str, request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "MES_SUPERVISOR/QA_INSPECTOR required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"batchNumber": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    target = body.get("targetStatus")
    target = target if isinstance(target, str) else ""

    try:
        row = (
            await db.execute(
                text(
                    "SELECT status, quality_disposition, recipe_code, recipe_version "
                    "FROM production_batches WHERE batch_number = :bid"
                ),
                {"bid": batch_id},
            )
        ).first()
    except Exception:  # noqa: BLE001
        raise MESError(404, "NOT_FOUND", "batch not found") from None
    if row is None:
        raise MESError(404, "NOT_FOUND", "batch not found")

    current_status = row[0] or ""
    qa_status = row[1]

    # 固定状态机迁移判定
    allowed = _STATE_GRAPH.get(current_status, set())
    if target in ("COMPLETED", "CANCELLED"):
        # 任意状态可到终态 COMPLETED/CANCELLED，但要求 qa_status 已判定
        if qa_status is None:
            raise MESError(409, "CONFLICT", "qa status required for completion")
    elif target not in allowed:
        raise MESError(409, "CONFLICT", f"invalid transition {current_status}->{target}")

    now = utc_now()
    try:
        # IN_PROGRESS → WAITING_QA 时校验 WIP 集合非空并调用合规服务
        compliance_ok = None
        if current_status == "IN_PROGRESS" and target == "WAITING_QA":
            wip = (
                await db.execute(
                    text(
                        "SELECT COUNT(*) FROM production_batch_charges "
                        "WHERE batch_number = :bid"
                    ),
                    {"bid": batch_id},
                )
            ).scalar()
            if not wip:
                raise MESError(409, "CONFLICT", "no WIP found for batch")
            comp = (
                await db.execute(
                    text(
                        "SELECT recipe_compliant FROM v_batch_recipe_compliance "
                        "WHERE batch_number = :bid"
                    ),
                    {"bid": batch_id},
                )
            ).first()
            compliance_ok = bool(comp and comp[0])

        await db.execute(
            text("UPDATE production_batches SET status = :t, updated_at = :at WHERE batch_number = :bid"),
            {"t": target, "at": now, "bid": batch_id},
        )

        # RELEASED → IN_PROGRESS：同步推进关联工单状态（C++ production_orders）
        if current_status == "RELEASED" and target == "IN_PROGRESS":
            await db.execute(
                text(
                    "UPDATE production_work_orders SET status = 'IN_PROGRESS', updated_at = :at "
                    "WHERE work_order_number = :bid"
                ),
                {"at": now, "bid": batch_id},
            )

        # WAITING_QA → QA_PASSED/QA_FAILED：同步写入 quality_disposition
        if current_status == "WAITING_QA" and target in ("QA_PASSED", "QA_FAILED"):
            await db.execute(
                text(
                    "UPDATE production_batches SET quality_disposition = :q WHERE batch_number = :bid"
                ),
                {"q": target, "bid": batch_id},
            )

        await write_audit(
            db,
            action="BATCH_TRANSITION",
            resource_type="BATCH",
            resource_id=batch_id,
            before={"from": current_status},
            after={"to": target, "recipeCompliant": compliance_ok},
            actor_id=get_actor(request),
        )
        await db.execute(
            text(
                "INSERT INTO integration_outbox_messages "
                "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, "
                "status, payload, created_at) "
                "VALUES (:id, 'BATCH', :aid, 'batch.state.changed', 'ERP', "
                "'PENDING', :payload, :at)"
            ),
            {
                "id": gen_id("OUT"),
                "aid": batch_id,
                "payload": json.dumps(
                    {"batchNumber": batch_id, "status": target, "recipeCompliant": compliance_ok},
                    ensure_ascii=False,
                ),
                "at": now,
            },
        )
        await save_idempotent(db, idem_key, "200", batch_id, "transitioned")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:  # noqa: BLE001 - 与 C++ 一致忽略写失败
        await db.rollback()

    return ok({"batchNumber": batch_id, "status": target}, corr)

