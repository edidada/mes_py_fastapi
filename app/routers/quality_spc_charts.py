"""``GET /api/v1/quality/spc/charts`` 读取最新 SPC 测量值。

接口不分页、不做角色/认证校验、不计算控制限；控制限与违规规则直接来自已保存
测量记录。返回 C++ 的双层 envelope。数据库查询失败时返回 ``500 DATABASE_ERROR``。
"""

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from app.contract import correlation_id, list_envelope, ok
from app.deps import DbSession
from app.errors import MESError

router = APIRouter()


@router.get("/spc/charts", status_code=200)
async def get_spc_charts(
    request: Request, db: DbSession, characteristicCode: str | None = Query(default=None)
) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    sql = (
        "SELECT measurement_id, characteristic_code, value, cl, ucl, lcl, usl, lsl, "
        "       violation_rule, measured_at "
        "FROM quality_spc_measurements"
    )
    params: dict = {}
    if characteristicCode is not None:
        sql += " WHERE characteristic_code = :cc"
        params["cc"] = characteristicCode
    sql += " ORDER BY measured_at DESC LIMIT 100"

    try:
        result = await db.execute(text(sql), params)
        rows = result.fetchall()
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None

    items = [
        {
            "measurementId": r[0],
            "characteristicCode": r[1],
            "value": r[2],
            "cl": r[3],
            "ucl": r[4],
            "lcl": r[5],
            "usl": r[6],
            "lsl": r[7],
            "violationRule": r[8] if r[8] is not None else "",
            "inControl": r[8] is None,
            "measuredAt": r[9],
        }
        for r in rows
    ]
    inner = list_envelope(items, corr, page=1, page_size=100)
    return ok(inner, corr)
