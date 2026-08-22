"""``POST /api/v1/master/workers`` 创建员工主数据。"""

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


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("", status_code=200)
async def create_worker(request: Request, db: DbSession) -> dict:
    require_role(request, _ADMIN, "forbidden")

    idem_key = await require_idempotency_key(request)
    cached = await find_idempotent(db, idem_key)
    if cached is not None:
        return ok(
            {"workerId": cached["resource_code"], "cached": True},
            correlation_id(request.headers.get("X-Correlation-Id")),
        )

    body = await _parse_body(request)
    worker_id = body.get("workerId")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise MESError(422, "VALIDATION_ERROR", "invalid workerId")
    team_code = body.get("teamCode")
    if not isinstance(team_code, str) or not team_code.strip():
        raise MESError(409, "CONFLICT", "invalid teamCode")
    display_name = body.get("displayName") if isinstance(body.get("displayName"), str) else ""

    try:
        await db.execute(
            text(
                "INSERT INTO master_workers (worker_id, team_code, display_name) "
                "VALUES (:id, :team, :name)"
            ),
            {"id": worker_id, "team": team_code, "name": display_name},
        )
        quals = body.get("qualifications")
        if isinstance(quals, list):
            seen = set()
            for q in quals:
                if not isinstance(q, str) or not q.strip() or q in seen:
                    continue
                seen.add(q)
                await db.execute(
                    text(
                        "INSERT OR IGNORE INTO master_worker_qualifications "
                        "(worker_id, qualification, granted_by) VALUES (:id, :q, :team)"
                    ),
                    {"id": worker_id, "q": q, "team": team_code},
                )
        await write_audit(
            db,
            action="WORKER_CREATE",
            resource_type="WORKER",
            resource_id=worker_id,
            before={},
            after=body,
            actor_id=get_actor(request),
        )
        await save_idempotent(db, idem_key, "200", worker_id, "worker created")
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise MESError(409, "CONFLICT", str(exc.orig)) from exc

    return ok(
        {"workerId": worker_id, "displayName": display_name},
        correlation_id(request.headers.get("X-Correlation-Id")),
    )
