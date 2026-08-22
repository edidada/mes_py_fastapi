"""``POST /api/v1/stations/{stationCode}/sessions`` 站点会话登录/退出。

``action`` 为 ``LOGIN``（或缺省）时创建 ACTIVE 会话；其他非空字符串执行退出。
接口不检查角色。与 C++ 一致，除登录会话 INSERT 外，其余 UPDATE、审计、幂等
和提交结果都不改变 HTTP 成功响应。
"""

import json

from fastapi import APIRouter, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.audit import write_audit
from app.auth import get_actor
from app.contract import correlation_id, gen_id, ok, utc_now
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()

_LOGIN = "LOGIN"


def _to_str(value) -> str:
    return value if isinstance(value, str) else ""


def _parse_action(value) -> str:
    """省略、null、非字符串或空字符串默认为 LOGIN；严格等于大写 LOGIN 时登录。"""
    if not isinstance(value, str):
        return _LOGIN
    if value == _LOGIN:
        return _LOGIN
    return value


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


async def _close_active_sessions(db: DbSession, station_code: str, user_id: str, at: str) -> None:
    """把相同站点、相同用户的其他 ACTIVE 会话关闭并填写 logged_out_at。"""
    await db.execute(
        text(
            "UPDATE production_station_sessions SET status = 'CLOSED', logged_out_at = :at "
            "WHERE station_code = :sc AND user_id = :uid AND status = 'ACTIVE'"
        ),
        {"at": at, "sc": station_code, "uid": user_id},
    )


@router.post("/{station_code}/sessions", status_code=200)
async def create_station_session(request: Request, station_code: str, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok({"sessionId": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    action = _parse_action(body.get("action"))
    user_id = _to_str(body.get("userId"))
    now = utc_now()

    if action != _LOGIN:
        # 退出分支：不开事务，只关闭匹配会话并写幂等记录；不写审计事件。
        # 所有写失败按 C++ 行为不影响 HTTP 成功。
        try:
            await _close_active_sessions(db, station_code, user_id, now)
            await save_idempotent(db, idem_key, "200", "", "logged out")
            await db.commit()
        except Exception:
            await db.rollback()
        return ok({"status": "CLOSED"}, corr)

    shift_code = _to_str(body.get("shiftCode"))
    session_id = gen_id("SES")

    # 唯一检查执行结果的写入：违反站点外键等约束 → 422
    try:
        await _close_active_sessions(db, station_code, user_id, now)
        await db.execute(
            text(
                "INSERT INTO production_station_sessions "
                "(session_id, station_code, user_id, status, shift_code, started_at) "
                "VALUES (:id, :sc, :uid, 'ACTIVE', :shift, :at)"
            ),
            {
                "id": session_id,
                "sc": station_code,
                "uid": user_id,
                "shift": shift_code,
                "at": now,
            },
        )
        await write_audit(
            db,
            action="STATION_LOGIN",
            resource_type="STATION",
            resource_id=station_code,
            before={},
            after=body,
            actor_id=get_actor(request),
        )
        await save_idempotent(db, idem_key, "200", session_id, "logged in")
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        raise MESError(422, "VALIDATION_ERROR", str(exc.orig)) from exc

    return ok({"sessionId": session_id, "status": "ACTIVE"}, corr)
