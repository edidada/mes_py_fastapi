"""批次查询、状态、投料、参数、处置、EBR 端点。

实现文档中的：
- ``GET /api/v1/batches`` 批次列表（2535）
- ``GET /api/v1/batches/{id}`` 批次详情（2545）
- ``POST /api/v1/batches/{id}/state`` 批次状态迁移（2551）
- ``POST /api/v1/batches/{id}/charges`` 投料记录（2592）
- ``POST /api/v1/batches/{id}/parameters`` 参数录入（2622）
- ``POST /api/v1/batches/{id}/quality-disposition`` 质量处置（2643）
- ``GET /api/v1/batches/{id}/ebr`` 电子批记录（2670）

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


@router.post("/batches/{batch_id}/charges", status_code=200)
async def post_batch_charges(batch_id: str, request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, {"MES_SUPERVISOR", "MES_ADMIN"}, "MES_SUPERVISOR required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"batchNumber": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    charges = body.get("charges")
    if not isinstance(charges, list) or not charges:
        raise MESError(422, "VALIDATION_ERROR", "charges required")

    # 批次必须存在且取其工厂
    b = (
        await db.execute(
            text("SELECT plant_code, recipe_code, recipe_version FROM production_batches WHERE batch_number = :bid"),
            {"bid": batch_id},
        )
    ).first()
    if b is None:
        raise MESError(404, "NOT_FOUND", "batch not found")
    plant_code = b[0]
    recipe_code = b[1]
    recipe_version = b[2]

    # 校验每笔投料并经主配方组分匹配、库存可用量校验
    parsed = []
    for idx, c in enumerate(charges):
        if not isinstance(c, dict):
            raise MESError(422, "VALIDATION_ERROR", f"charge[{idx}] must be object")
        seq = c.get("componentSequence")
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else int(seq) if str(seq).isdigit() else 0
        material_code = c.get("materialCode")
        material_code = material_code if isinstance(material_code, str) else ""
        lot_number = c.get("lotNumber")
        lot_number = lot_number if isinstance(lot_number, str) else None
        quantity = c.get("quantity")
        try:
            quantity = float(quantity)
        except (TypeError, ValueError):
            raise MESError(422, "VALIDATION_ERROR", f"charge[{idx}] quantity invalid") from None
        unit_code = c.get("unitCode")
        unit_code = unit_code if isinstance(unit_code, str) else ""
        if not material_code:
            raise MESError(422, "VALIDATION_ERROR", f"charge[{idx}] materialCode required")
        if quantity <= 0:
            raise MESError(409, "CONFLICT", f"charge[{idx}] quantity must be > 0")

        # 组分必须匹配批次主配方
        comp = (
            await db.execute(
                text(
                    "SELECT 1 FROM master_recipe_components "
                    "WHERE recipe_code = :rc AND version = :rv "
                    "AND component_sequence = :seq AND material_code = :mc"
                ),
                {"rc": recipe_code, "rv": recipe_version, "seq": seq, "mc": material_code},
            )
        ).first()
        if comp is None:
            raise MESError(409, "CONFLICT", f"charge[{idx}] component mismatch with recipe")

        # 调用库存服务：按工厂/物料/批次号聚合可用量，校验充足
        avail = (
            await db.execute(
                text(
                    "SELECT COALESCE(SUM(on_hand_quantity - reserved_quantity), 0) "
                    "FROM material_inventory_balances "
                    "WHERE plant_code = :p AND material_code = :mc "
                    "AND (batch_number = :lot OR :lot IS NULL)"
                ),
                {"p": plant_code, "mc": material_code, "lot": lot_number},
            )
        ).scalar()
        if avail is None or avail < quantity:
            raise MESError(409, "CONFLICT", f"charge[{idx}] insufficient inventory")

        parsed.append((seq, material_code, lot_number, quantity, unit_code))

    now = utc_now()
    try:
        for seq, material_code, lot_number, quantity, unit_code in parsed:
            await db.execute(
                text(
                    "INSERT INTO production_batch_charges "
                    "(charge_id, batch_number, component_sequence, material_code, lot_number, "
                    "quantity, unit_code, charged_by, charged_at) "
                    "VALUES (:id, :bid, :seq, :mc, :lot, :q, :u, :by, :at)"
                ),
                {
                    "id": gen_id("CHG"),
                    "bid": batch_id,
                    "seq": seq,
                    "mc": material_code,
                    "lot": lot_number,
                    "q": quantity,
                    "u": unit_code,
                    "by": get_actor(request),
                    "at": now,
                },
            )
        await write_audit(
            db,
            action="BATCH_CHARGE",
            resource_type="BATCH",
            resource_id=batch_id,
            before={},
            after={"charges": len(parsed)},
            actor_id=get_actor(request),
        )
        await db.execute(
            text(
                "INSERT INTO integration_outbox_messages "
                "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, "
                "status, payload, created_at) "
                "VALUES (:id, 'BATCH', :aid, 'batch.charges.posted', 'ERP', "
                "'PENDING', :payload, :at)"
            ),
            {
                "id": gen_id("OUT"),
                "aid": batch_id,
                "payload": json.dumps({"batchNumber": batch_id, "charges": len(parsed)}, ensure_ascii=False),
                "at": now,
            },
        )
        await save_idempotent(db, idem_key, "200", batch_id, "charges posted")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:  # noqa: BLE001 - 与 C++ 一致忽略写失败
        await db.rollback()

    return ok({"batchNumber": batch_id, "chargesPosted": len(parsed)}, corr)


@router.post("/batches/{batch_id}/parameters", status_code=200)
async def post_batch_parameters(batch_id: str, request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, {"MES_OPERATOR", "MES_SUPERVISOR", "MES_ADMIN"}, "MES_OPERATOR required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"batchNumber": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    parameters = body.get("parameters")
    if not isinstance(parameters, list) or not parameters:
        raise MESError(422, "VALIDATION_ERROR", "parameters required")

    b = (
        await db.execute(
            text("SELECT recipe_code, recipe_version FROM production_batches WHERE batch_number = :bid"),
            {"bid": batch_id},
        )
    ).first()
    if b is None:
        raise MESError(404, "NOT_FOUND", "batch not found")
    recipe_code = b[0]
    recipe_version = b[1]

    parsed = []
    for idx, p in enumerate(parameters):
        if not isinstance(p, dict):
            raise MESError(422, "VALIDATION_ERROR", f"parameter[{idx}] must be object")
        seq = p.get("stepSequence")
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else int(seq) if str(seq).isdigit() else 0
        param_code = p.get("parameterCode")
        param_code = param_code if isinstance(param_code, str) else ""
        value = p.get("value")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise MESError(422, "VALIDATION_ERROR", f"parameter[{idx}] value invalid") from None
        unit_code = p.get("unitCode")
        unit_code = unit_code if isinstance(unit_code, str) else None
        if not param_code:
            raise MESError(422, "VALIDATION_ERROR", f"parameter[{idx}] parameterCode required")

        # 参数必须匹配主配方
        m = (
            await db.execute(
                text(
                    "SELECT lower_limit, upper_limit FROM master_recipe_parameters "
                    "WHERE recipe_code = :rc AND version = :rv "
                    "AND step_sequence = :seq AND parameter_code = :pc"
                ),
                {"rc": recipe_code, "rv": recipe_version, "seq": seq, "pc": param_code},
            )
        ).first()
        if m is None:
            raise MESError(409, "CONFLICT", f"parameter[{idx}] not in recipe")
        lower, upper = m[0], m[1]
        in_spec = 1
        if lower is not None and value < lower:
            in_spec = 0
        if upper is not None and value > upper:
            in_spec = 0
        parsed.append((seq, param_code, value, unit_code, lower, upper, in_spec))

    now = utc_now()
    try:
        for seq, param_code, value, unit_code, lower, upper, in_spec in parsed:
            await db.execute(
                text(
                    "INSERT INTO production_batch_parameters "
                    "(record_id, batch_number, step_sequence, parameter_code, value, "
                    "lower_limit, upper_limit, unit_code, in_spec, recorded_by, recorded_at) "
                    "VALUES (:id, :bid, :seq, :pc, :v, :lo, :up, :u, :spec, :by, :at)"
                ),
                {
                    "id": gen_id("PRM"),
                    "bid": batch_id,
                    "seq": seq,
                    "pc": param_code,
                    "v": value,
                    "lo": lower,
                    "up": upper,
                    "u": unit_code,
                    "spec": in_spec,
                    "by": get_actor(request),
                    "at": now,
                },
            )
        await write_audit(
            db,
            action="BATCH_PARAMETER",
            resource_type="BATCH",
            resource_id=batch_id,
            before={},
            after={"parameters": len(parsed)},
            actor_id=get_actor(request),
        )
        await db.execute(
            text(
                "INSERT INTO integration_outbox_messages "
                "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, "
                "status, payload, created_at) "
                "VALUES (:id, 'BATCH', :aid, 'batch.parameters.recorded', 'LIMS', "
                "'PENDING', :payload, :at)"
            ),
            {
                "id": gen_id("OUT"),
                "aid": batch_id,
                "payload": json.dumps({"batchNumber": batch_id, "parameters": len(parsed)}, ensure_ascii=False),
                "at": now,
            },
        )
        await save_idempotent(db, idem_key, "200", batch_id, "parameters recorded")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:  # noqa: BLE001 - 与 C++ 一致忽略写失败
        await db.rollback()

    return ok({"batchNumber": batch_id, "parametersRecorded": len(parsed)}, corr)


@router.post("/batches/{batch_id}/quality-disposition", status_code=200)
async def post_batch_disposition(batch_id: str, request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, {"QA_INSPECTOR", "MES_ADMIN"}, "QA_INSPECTOR required")
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"batchNumber": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    decision = body.get("decision")
    decision = decision if isinstance(decision, str) else ""
    disposition_code = body.get("dispositionCode")
    disposition_code = disposition_code if isinstance(disposition_code, str) else None
    inspector = body.get("inspector")
    inspector = inspector if isinstance(inspector, str) else ""
    notes = body.get("notes")
    notes = notes if isinstance(notes, str) else None

    if decision not in ("ACCEPT", "REJECT", "REWORK", "SCRAP", "USE_AS_IS"):
        raise MESError(422, "VALIDATION_ERROR", "decision invalid")
    if not inspector:
        raise MESError(422, "VALIDATION_ERROR", "inspector required")

    row = (
        await db.execute(
            text("SELECT status FROM production_batches WHERE batch_number = :bid"),
            {"bid": batch_id},
        )
    ).first()
    if row is None:
        raise MESError(404, "NOT_FOUND", "batch not found")
    # 质量判定仅允许在待判定/已判定质量相关状态进行
    if row[0] not in ("WAITING_QA", "QA_PASSED", "QA_FAILED"):
        raise MESError(409, "CONFLICT", "batch not in quality decision state")

    now = utc_now()
    try:
        await db.execute(
            text(
                "INSERT INTO production_batch_dispositions "
                "(id, batch_number, decision, disposition_code, inspector, inspected_at, notes, status) "
                "VALUES (:id, :bid, :dec, :dc, :ins, :at, :notes, 'RECORDED')"
            ),
            {
                "id": gen_id("DSP"),
                "bid": batch_id,
                "dec": decision,
                "dc": disposition_code,
                "ins": inspector,
                "at": now,
                "notes": notes,
            },
        )
        await db.execute(
            text(
                "UPDATE production_batches SET quality_disposition = :q, updated_at = :at "
                "WHERE batch_number = :bid"
            ),
            {"q": decision, "at": now, "bid": batch_id},
        )
        await write_audit(
            db,
            action="BATCH_DISPOSITION",
            resource_type="BATCH",
            resource_id=batch_id,
            before={},
            after={"decision": decision},
            actor_id=get_actor(request),
        )
        await db.execute(
            text(
                "INSERT INTO integration_outbox_messages "
                "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, "
                "status, payload, created_at) "
                "VALUES (:id, 'BATCH', :aid, 'batch.quality.disposition', 'QMS', "
                "'PENDING', :payload, :at)"
            ),
            {
                "id": gen_id("OUT"),
                "aid": batch_id,
                "payload": json.dumps({"batchNumber": batch_id, "decision": decision}, ensure_ascii=False),
                "at": now,
            },
        )
        await save_idempotent(db, idem_key, "200", batch_id, "disposition recorded")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:  # noqa: BLE001 - 与 C++ 一致忽略写失败
        await db.rollback()

    return ok({"batchNumber": batch_id, "decision": decision}, corr)


@router.get("/batches/{batch_id}/ebr", status_code=200)
async def get_batch_ebr(batch_id: str, request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))

    row = (
        await db.execute(
            text(
                "SELECT plant_code, product_material_code, status, quality_disposition, "
                "recipe_code, recipe_version, equipment_code, planned_quantity, "
                "actual_quantity, unit_code, started_at, completed_at, created_at, updated_at "
                "FROM production_batches WHERE batch_number = :bid"
            ),
            {"bid": batch_id},
        )
    ).first()
    if row is None:
        raise MESError(404, "NOT_FOUND", "batch not found")

    charges = (
        await db.execute(
            text(
                "SELECT component_sequence, material_code, lot_number, quantity, unit_code, "
                "charged_by, charged_at FROM production_batch_charges WHERE batch_number = :bid"
            ),
            {"bid": batch_id},
        )
    ).fetchall()
    parameters = (
        await db.execute(
            text(
                "SELECT step_sequence, parameter_code, value, lower_limit, upper_limit, "
                "unit_code, in_spec, recorded_by, recorded_at "
                "FROM production_batch_parameters WHERE batch_number = :bid"
            ),
            {"bid": batch_id},
        )
    ).fetchall()
    dispositions = (
        await db.execute(
            text(
                "SELECT decision, disposition_code, inspector, inspected_at, notes, status "
                "FROM production_batch_dispositions WHERE batch_number = :bid ORDER BY inspected_at"
            ),
            {"bid": batch_id},
        )
    ).fetchall()

    # 调用质量服务 enrich（v_quality_ebr）注入质量结论
    quality = (
        await db.execute(
            text("SELECT decision, disposition_status, batch_status FROM v_quality_ebr WHERE batch_number = :bid"),
            {"bid": batch_id},
        )
    ).first()

    ebr = {
        "batchNumber": batch_id,
        "plantCode": row[0],
        "materialCode": row[1],
        "status": _cap_status(row[2]),
        "qaStatus": row[3],
        "recipeCode": row[4],
        "recipeVersion": row[5],
        "equipmentCode": row[6],
        "plannedQuantity": row[7],
        "actualQuantity": row[8],
        "unitCode": row[9],
        "startedAt": row[10],
        "completedAt": row[11],
        "createdAt": row[12],
        "updatedAt": row[13],
        "charges": [
            {
                "componentSequence": c[0],
                "materialCode": c[1],
                "lotNumber": c[2],
                "quantity": c[3],
                "unitCode": c[4],
                "chargedBy": c[5],
                "chargedAt": c[6],
            }
            for c in charges
        ],
        "parameters": [
            {
                "stepSequence": p[0],
                "parameterCode": p[1],
                "value": p[2],
                "lowerLimit": p[3],
                "upperLimit": p[4],
                "unitCode": p[5],
                "inSpec": p[6],
                "recordedBy": p[7],
                "recordedAt": p[8],
            }
            for p in parameters
        ],
        "dispositions": [
            {
                "decision": d[0],
                "dispositionCode": d[1],
                "inspector": d[2],
                "inspectedAt": d[3],
                "notes": d[4],
                "status": d[5],
            }
            for d in dispositions
        ],
        "executionEvents": [],
        "qualityConclusion": (
            {"decision": quality[0], "dispositionStatus": quality[1], "batchStatus": quality[2]}
            if quality else None
        ),
    }

    # 写 EBR 查看审计（返回值忽略，与 C++ 一致）
    try:
        await write_audit(
            db,
            action="EBR_VIEW",
            resource_type="BATCH",
            resource_id=batch_id,
            before={},
            after={},
            actor_id=get_actor(request),
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()

    return ok(ebr, corr)

