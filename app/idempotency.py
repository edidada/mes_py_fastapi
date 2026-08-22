"""幂等键查询与写入。"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contract import utc_now
from app.errors import MESError
from app.models import IdempotencyKey


async def find_idempotent(session: AsyncSession, key: str) -> dict | None:
    """查询幂等记录；命中返回 ``{"resource_code": ...}``。"""
    row = await session.get(IdempotencyKey, key)
    if row is None:
        return None
    return {"resource_code": row.resource_code}


async def save_idempotent(
    session: AsyncSession,
    key: str,
    status: str,
    resource_code: str,
    message: str,
) -> None:
    """写入幂等投影（expires_at 与 C++ 一致写当前时间）。"""
    session.add(
        IdempotencyKey(
            idempotency_key=key,
            status=status,
            resource_code=resource_code,
            message=message,
            created_at=utc_now(),
            expires_at=utc_now(),
        )
    )


async def require_idempotency_key(request) -> str:
    """校验幂等键非空，否则 422 VALIDATION_ERROR。"""
    key = request.headers.get("X-Idempotency-Key", "")
    if not key or not isinstance(key, str) or not key.strip():
        raise MESError(422, "VALIDATION_ERROR", "idempotency key required")
    return key
