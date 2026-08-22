"""角色校验与 Header 读取工具。"""

from fastapi import Request

from app.errors import MESError

DEFAULT_ROLE = "MES_OPERATOR"
DEFAULT_ACTOR = "system"


def get_actor(request: Request) -> str:
    """读取操作人 Header，支持别名，默认 ``system``。"""
    return (
        request.headers.get("X-Actor-Id")
        or request.headers.get("X-User-Id")
        or DEFAULT_ACTOR
    )


def get_role(request: Request) -> str:
    """读取角色 Header，支持别名，默认 ``MES_OPERATOR``。"""
    return request.headers.get("X-Actor-Role") or request.headers.get("X-Role") or DEFAULT_ROLE


def require_role(request: Request, allowed: set[str], message: str = "forbidden") -> None:
    """校验有效角色在允许集合内，否则抛 403 FORBIDDEN。"""
    role = get_role(request)
    if role not in allowed:
        raise MESError(403, "FORBIDDEN", message)
