"""``POST /api/v1/integration/outbox/{id}/replay`` 重试失败的出站消息。

权限校验先于幂等。幂等未命中时按 path ``id`` 查询：不存在或失败返回 ``404``，
状态非 ``FAILED`` 返回 ``409 INVALID_STATE``。事务中：出站消息改 ``PENDING``、
清空 ``published_at``；死信 ``replayed_at`` 更新；写审计与幂等。两个必需 UPDATE
任一失败回滚 ``500 PERSISTENCE_ERROR``。首次成功 HTTP ``202``，幂等命中返回
``200`` + ``cached=true``。
"""

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from app.audit import write_audit
from app.auth import require_role
from app.contract import correlation_id, ok, utc_now
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()

_ALLOWED_ROLES = {"MES_ADMIN"}


@router.post("/outbox/{outbox_id}/replay", status_code=202)
async def replay_outbox_message(
    request: Request, response: Response, outbox_id: str, db: DbSession
) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "MES_ADMIN required")
    idem_key = await require_idempotency_key(request)
    try:
        cached = await find_idempotent(db, idem_key)
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None
    if cached is not None:
        # 幂等命中：不读 path 消息，返回缓存资源 ID + 固定 PENDING；HTTP 200
        response.status_code = 200
        return ok(
            {"outboxId": cached["resource_code"], "status": "PENDING", "cached": True},
            corr,
        )

    # 查询出站消息
    try:
        row = (
            await db.execute(
                text(
                    "SELECT status FROM integration_outbox_messages WHERE outbox_id = :id"
                ),
                {"id": outbox_id},
            )
        ).first()
    except Exception:
        raise MESError(404, "NOT_FOUND", "outbox message not found") from None
    if row is None:
        raise MESError(404, "NOT_FOUND", "outbox message not found")
    if row[0] != "FAILED":
        raise MESError(
            409, "INVALID_STATE", "only FAILED messages may be replayed"
        )

    now = utc_now()
    try:
        # 1. 出站消息改 PENDING、清空 published_at
        result = await db.execute(
            text(
                "UPDATE integration_outbox_messages SET status = 'PENDING', "
                "published_at = NULL WHERE outbox_id = :id"
            ),
            {"id": outbox_id},
        )
        if result.rowcount == 0:
            raise MESError(500, "PERSISTENCE_ERROR", "update failed")

        # 2. 死信 replayed_at 更新（不存在不视为错误，但 UPDATE 失败 → 500）
        dl_result = await db.execute(
            text(
                "UPDATE integration_outbox_dead_letters SET replayed_at = :t "
                "WHERE outbox_id = :id"
            ),
            {"t": now, "id": outbox_id},
        )
        # C++ 视两个 UPDATE 为必需；死信不存在时 rowcount=0 也算成功（不视为错误）
        # 但 SQL 执行异常会触发 except 分支

        # 3. 审计
        await write_audit(
            db,
            action="OUTBOX_REPLAY",
            resource_type="OUTBOX",
            resource_id=outbox_id,
            before={"status": "FAILED"},
            after={"status": "PENDING"},
            actor_id="",
        )
        # 4. 幂等
        await save_idempotent(db, idem_key, "200", outbox_id, "outbox replayed")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise MESError(500, "PERSISTENCE_ERROR", "update failed") from None

    return ok({"outboxId": outbox_id, "status": "PENDING"}, corr)
