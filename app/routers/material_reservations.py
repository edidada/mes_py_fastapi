"""``POST /api/v1/material/reservations`` 按 FIFO 分配物料预留。

仅 ``PLANNER``、``MES_SUPERVISOR`` 或 ``MES_ADMIN`` 可调用。数量小于等于 0 时
直接成功且不分配批次。按 ``expiry_at`` 升序遍历 ``v_available_inventory``，
每个批次的预留/余额/物料事务三条 SQL 各自失败被忽略（例如未知工单使预留主
记录失败，但余额仍更新）；短缺量大于 0.0001 时整个事务回滚返回 409。
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


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


def _as_quantity(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _as_int(value) -> int:
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


@router.post("/reservations", status_code=200)
async def create_material_reservation(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "PLANNER required")
    idem_key = await require_idempotency_key(request)
    try:
        cached = await find_idempotent(db, idem_key)
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None
    if cached is not None:
        return ok({"reservationId": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    material_code = _as_str(body.get("materialCode"))
    reserved_quantity = _as_quantity(body.get("reservedQuantity"))
    work_order_number = _as_str(body.get("workOrderNumber"))
    operation_sequence = _as_int(body.get("operationSequence"))
    expires_at = _as_str(body.get("expiresAt"))
    operator = get_actor(request)
    now = utc_now()

    if reserved_quantity <= 0:
        # 直接成功且不分配批次
        try:
            await write_audit(
                db,
                action="MATERIAL_RESERVE",
                resource_type="MATERIAL",
                resource_id=material_code,
                before={},
                after=body,
                actor_id=operator,
            )
            await save_idempotent(db, idem_key, "200", "", "reserved")
            await db.commit()
        except MESError:
            await db.rollback()
            raise
        except Exception:
            await db.rollback()
        return ok({"status": "ACTIVE", "allocatedLots": []}, corr)

    # FIFO 分配：按 expiry_at 升序读取可用库存
    try:
        rows = (
            await db.execute(
                text(
                    "SELECT material_code, lot_number, available_quantity, expiry_at "
                    "FROM v_available_inventory WHERE material_code = :mc "
                    "ORDER BY expiry_at ASC"
                ),
                {"mc": material_code},
            )
        ).fetchall()
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None

    remaining = reserved_quantity
    allocated: list[dict] = []
    first_reservation_id = ""
    try:
        for r in rows:
            if remaining <= 0.0001:
                break
            lot_number = r[1]
            available = float(r[2]) if r[2] is not None else 0.0
            qty = min(remaining, available)
            rsv_id = gen_id("RSV")
            if not first_reservation_id:
                first_reservation_id = rsv_id
            # 1. 预留主记录（未知工单 → 外键失败，忽略）
            try:
                await db.execute(
                    text(
                        "INSERT INTO material_inventory_reservations "
                        "(reservation_id, material_code, lot_number, work_order_number, "
                        " operation_sequence, quantity, status, expires_at) "
                        "VALUES (:id, :mc, :lot, :wo, :seq, :q, 'ACTIVE', :exp)"
                    ),
                    {
                        "id": rsv_id,
                        "mc": material_code,
                        "lot": lot_number,
                        "wo": work_order_number,
                        "seq": operation_sequence,
                        "q": qty,
                        "exp": expires_at,
                    },
                )
            except Exception:
                pass
            # 2. 余额 reserved_quantity 累加
            try:
                await db.execute(
                    text(
                        "UPDATE material_inventory_balances SET reserved_quantity = "
                        "COALESCE(reserved_quantity, 0) + :q WHERE lot_number = :lot"
                    ),
                    {"q": qty, "lot": lot_number},
                )
            except Exception:
                pass
            # 3. 物料事务（与 C++ 一样不提供 transaction_id → SQLite 拒绝，忽略）
            try:
                await db.execute(
                    text(
                        "INSERT INTO material_material_transactions "
                        "(material_code, lot_number, transaction_type, quantity, plant_code, "
                        " location_code, reference_type, reference_id, transaction_at, actor_id) "
                        "VALUES (:mc, :lot, 'RESERVE', :q, 'PLANT-A', 'RAW-STORE', "
                        "        'WORK_ORDER', :wo, :t, :op)"
                    ),
                    {
                        "mc": material_code,
                        "lot": lot_number,
                        "q": qty,
                        "wo": work_order_number,
                        "t": now,
                        "op": operator,
                    },
                )
            except Exception:
                pass
            allocated.append(
                {"lotNumber": lot_number, "quantity": qty, "reservationId": rsv_id}
            )
            remaining -= qty

        if remaining > 0.0001:
            await db.rollback()
            raise MESError(
                409, "CONFLICT", f"insufficient inventory, short {remaining:.6f}"
            )

        await write_audit(
            db,
            action="MATERIAL_RESERVE",
            resource_type="MATERIAL",
            resource_id=material_code,
            before={},
            after=body,
            actor_id=operator,
        )
        await save_idempotent(db, idem_key, "200", first_reservation_id, "reserved")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()

    return ok({"status": "ACTIVE", "allocatedLots": allocated}, corr)
