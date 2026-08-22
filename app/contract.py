"""响应信封、关联 ID、时间与 ID 生成工具。"""

import random
import string
import uuid
from datetime import datetime, timezone
from typing import Any

_CORR_SEQ = 0


def utc_now() -> str:
    """UTC RFC 3339，精度到秒。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def correlation_id(existing: str | None = None) -> str:
    """返回传入的关联 ID；省略或为空时生成 ``corr-...``。"""
    global _CORR_SEQ
    if existing and isinstance(existing, str) and existing.strip():
        return existing
    _CORR_SEQ += 1
    return f"corr-{_CORR_SEQ}"


def gen_id(prefix: str) -> str:
    """生成 ``PREFIX-<时间戳>-<随机后缀>`` 形式的业务 ID。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    rand = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{prefix}-{ts}-{rand}"


def gen_uuid_short() -> str:
    return str(uuid.uuid4())


def ok(data: Any, correlation_id: str) -> dict:
    """标准成功信封。"""
    return {"data": data, "meta": {"correlationId": correlation_id, "generatedAt": utc_now()}}


def list_envelope(
    items: list[Any],
    correlation_id: str,
    page: int = 1,
    page_size: int = 100,
) -> dict:
    """C++ ``listEnvelope``：内层分页信封（又被外层 respondOk 包裹，形成双层）。"""
    return {
        "data": items,
        "meta": {
            "page": page,
            "pageSize": page_size,
            "total": len(items),
            "correlationId": correlation_id,
            "generatedAt": utc_now(),
        },
    }
