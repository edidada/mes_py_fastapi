"""``POST /api/v1/production-plans/import`` 生产计划导入。

接收 ERP/APS 计划，保存为 ``VALIDATED``，选择物料最新 ``EFFECTIVE`` 路线，
自动创建 ``DRAFT`` 工单并复制路线工序快照。没有角色校验。
"""

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.audit import write_audit
from app.auth import get_actor
from app.contract import correlation_id, gen_id, ok, utc_now
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()

_WO_DATE = re.compile(r"^WO-\d{2}-\d{2}-\d{2}-(\d+)$")


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


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


async def _next_work_order_number(db: DbSession) -> str:
    """生成 ``WO-yy-MM-dd-xxxxxx`` 工单号，当日序号自增。"""
    now = datetime.now(timezone.utc)
    prefix = f"WO-{now.strftime('%y-%m-%d')}-"
    row = (
        await db.execute(
            text(
                "SELECT work_order_number FROM production_work_orders "
                "WHERE work_order_number LIKE :prefix ORDER BY work_order_number DESC LIMIT 1"
            ),
            {"prefix": f"{prefix}%"},
        )
    ).first()
    seq = 1
    if row is not None:
        m = _WO_DATE.match(row[0])
        if m:
            seq = int(m.group(1)) + 1
    return f"{prefix}{seq:06d}"


@router.post("/import", status_code=202)
async def import_plan(request: Request, response: Response, db: DbSession) -> dict:
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        response.status_code = 200
        return ok({"planId": cached["resource_code"], "cached": True}, correlation_id(request.headers.get("X-Correlation-Id")))

    body = await _parse_body(request)
    source_system = body.get("sourceSystem")
    external_reference = body.get("externalReference")
    material_code = body.get("materialCode")
    if not all(isinstance(v, str) and v.strip() for v in (source_system, external_reference, material_code)):
        raise MESError(422, "VALIDATION_ERROR", "invalid plan fields")

    plant_code = body.get("plantCode") if isinstance(body.get("plantCode"), str) else ""
    quantity = _to_int(body.get("quantity"))
    priority = _to_int(body.get("priority"))
    due_at = body.get("dueAt") if isinstance(body.get("dueAt"), str) else ""

    # 物料必须存在
    material = (
        await db.execute(text("SELECT 1 FROM master_materials WHERE material_code = :code"), {"code": material_code})
    ).first()
    if material is None:
        raise MESError(404, "NOT_FOUND", "material not found")

    # 工厂必须存在（违反工厂约束→409 duplicate external reference）
    plant = (
        await db.execute(text("SELECT 1 FROM master_plants WHERE plant_code = :code"), {"code": plant_code})
    ).first()
    if plant is None:
        raise MESError(409, "CONFLICT", "duplicate external reference")

    # 数量必须为正（数据库约束失败被映射为 409 duplicate external reference）
    if quantity <= 0:
        raise MESError(409, "CONFLICT", "duplicate external reference")

    try:
        plan_id = gen_id("PLN")
        await db.execute(
            text(
                "INSERT INTO production_production_plans "
                "(plan_id, source_system, external_reference, plant_code, material_code, "
                "quantity, priority, due_at, status, payload) "
                "VALUES (:plan, :src, :ref, :plant, :mat, :qty, :pri, :due, 'VALIDATED', :payload)"
            ),
            {
                "plan": plan_id,
                "src": source_system,
                "ref": external_reference,
                "plant": plant_code,
                "mat": material_code,
                "qty": quantity,
                "pri": priority,
                "due": due_at,
                "payload": json.dumps(body, ensure_ascii=False),
            },
        )

        # 最新 EFFECTIVE 路线；无则回滚
        routing = (
            await db.execute(
                text(
                    "SELECT routing_code, version FROM master_routings "
                    "WHERE material_code = :mat AND status = 'EFFECTIVE' "
                    "ORDER BY effective_from DESC LIMIT 1"
                ),
                {"mat": material_code},
            )
        ).first()
        if routing is None:
            await db.rollback()
            raise MESError(422, "VALIDATION_ERROR", "no effective routing")

        # 最新有效 BOM；无则空字符串
        bom = (
            await db.execute(
                text(
                    "SELECT bom_code, version FROM master_boms "
                    "WHERE material_code = :mat AND status = 'EFFECTIVE' "
                    "ORDER BY effective_from DESC LIMIT 1"
                ),
                {"mat": material_code},
            )
        ).first()

        work_order_number = await _next_work_order_number(db)
        await db.execute(
            text(
                "INSERT INTO production_work_orders "
                "(work_order_number, plant_code, material_code, routing_code, routing_version, "
                "bom_code, bom_version, priority, status, planned_quantity) "
                "VALUES (:wo, :plant, :mat, :rc, :rv, :bc, :bv, :pri, 'DRAFT', :qty)"
            ),
            {
                "wo": work_order_number,
                "plant": plant_code,
                "mat": material_code,
                "rc": routing[0],
                "rv": routing[1],
                "bc": bom[0] if bom else "",
                "bv": bom[1] if bom else "",
                "pri": priority,
                "qty": quantity,
            },
        )

        # 复制路线工序快照（单条失败被忽略，不终止事务）
        ops = await db.execute(
            text(
                "SELECT sequence, operation_code, work_center_code, quality_gate, "
                "allow_skip, standard_cycle_seconds "
                "FROM master_routing_operations "
                "WHERE routing_code = :rc AND version = :rv ORDER BY sequence"
            ),
            {"rc": routing[0], "rv": routing[1]},
        )
        for op in ops.fetchall():
            try:
                await db.execute(
                    text(
                        "INSERT INTO production_work_order_operations "
                        "(work_order_number, sequence, operation_code, work_center_code, "
                        "quality_gate, allow_skip, standard_cycle_seconds, status) "
                        "VALUES (:wo, :seq, :op, :wc, :qg, :skip, :std, 'PENDING')"
                    ),
                    {
                        "wo": work_order_number,
                        "seq": op[0],
                        "op": op[1],
                        "wc": op[2],
                        "qg": op[3],
                        "skip": op[4],
                        "std": op[5],
                    },
                )
            except IntegrityError:
                pass

        await write_audit(
            db,
            action="PLAN_IMPORT",
            resource_type="PRODUCTION_PLAN",
            resource_id=plan_id,
            before={},
            after=body,
            actor_id=get_actor(request),
        )
        await save_idempotent(db, idem_key, "200", plan_id, "plan imported")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        raise MESError(409, "CONFLICT", "duplicate external reference") from exc

    return ok(
        {
            "planId": plan_id,
            "status": "VALIDATED",
            "workOrderNumber": work_order_number,
            "workOrderStatus": "DRAFT",
        },
        correlation_id(request.headers.get("X-Correlation-Id")),
    )
