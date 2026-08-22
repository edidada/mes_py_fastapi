"""``POST /api/v1/master/routings`` 创建工艺路线（DRAFT 版本）。"""

import json

from fastapi import APIRouter, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.audit import write_audit
from app.auth import get_actor, require_role
from app.contract import correlation_id, ok
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()

_ADMIN = {"MES_ADMIN"}


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value in ("true", "TRUE")
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _to_int(value) -> int:
    """sequence 转换：number 截断为整数；整数字符串可转换；其他为 0。"""
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


def _to_float(value) -> float:
    """standardCycleSeconds：仅 JSON number 被读取。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("", status_code=200)
async def create_routing(request: Request, db: DbSession) -> dict:
    require_role(request, _ADMIN, "forbidden")

    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok(
            {"routingCode": cached["resource_code"], "cached": True},
            correlation_id(request.headers.get("X-Correlation-Id")),
        )

    body = await _parse_body(request)
    routing_code = body.get("routingCode")
    version = body.get("version")
    material_code = body.get("materialCode")
    if not all(isinstance(v, str) and v.strip() for v in (routing_code, version, material_code)):
        raise MESError(422, "VALIDATION_ERROR", "invalid routing fields")

    # 验证物料存在
    material = (
        await db.execute(
            text("SELECT 1 FROM master_materials WHERE material_code = :code"),
            {"code": material_code},
        )
    ).first()
    if material is None:
        raise MESError(404, "NOT_FOUND", "material not found")

    operations = body.get("operations")
    ops = operations if isinstance(operations, list) else []

    try:
        await db.execute(
            text(
                "INSERT INTO master_routings "
                "(routing_code, version, material_code, status, description) "
                "VALUES (:code, :version, :material, 'DRAFT', '')"
            ),
            {"code": routing_code, "version": version, "material": material_code},
        )
        for op in ops:
            if not isinstance(op, dict):
                continue
            seq = _to_int(op.get("sequence"))
            if seq <= 0:
                raise MESError(422, "VALIDATION_ERROR", "invalid operation sequence")
            work_center = op.get("workCenterCode")
            if not isinstance(work_center, str) or not work_center.strip():
                raise MESError(422, "VALIDATION_ERROR", "invalid workCenterCode")
            await db.execute(
                text(
                    "INSERT INTO master_routing_operations "
                    "(routing_code, version, sequence, operation_code, work_center_code, "
                    "quality_gate, allow_skip, standard_cycle_seconds) "
                    "VALUES (:code, :version, :seq, :opcode, :wc, :qg, :skip, :std)"
                ),
                {
                    "code": routing_code,
                    "version": version,
                    "seq": seq,
                    "opcode": op.get("operationCode") if isinstance(op.get("operationCode"), str) else "",
                    "wc": work_center,
                    "qg": 1 if _to_bool(op.get("qualityGate")) else 0,
                    "skip": 1 if _to_bool(op.get("allowSkip")) else 0,
                    "std": _to_float(op.get("standardCycleSeconds")),
                },
            )
        await write_audit(
            db,
            action="ROUTING_CREATE",
            resource_type="ROUTING",
            resource_id=f"{routing_code}:{version}",
            before={},
            after=body,
            actor_id=get_actor(request),
        )
        await save_idempotent(db, idem_key, "200", routing_code, "routing created")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        raise MESError(409, "CONFLICT", str(exc.orig)) from exc

    return ok(
        {"routingCode": routing_code, "version": version, "status": "DRAFT"},
        correlation_id(request.headers.get("X-Correlation-Id")),
    )
