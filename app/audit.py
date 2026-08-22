"""审计事件写入。"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.contract import utc_now
from app.models import AuditEvent


async def write_audit(
    session: AsyncSession,
    action: str,
    resource_type: str,
    resource_id: str,
    before: Any,
    after: Any,
    actor_id: str,
) -> None:
    """写入一条审计事件（不提交）。"""
    import json

    session.add(
        AuditEvent(
            occurred_at=utc_now(),
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            before_data=json.dumps(before, ensure_ascii=False),
            after_data=json.dumps(after, ensure_ascii=False),
            actor_id=actor_id,
        )
    )
