"""``GET /api/v1/master/plants`` 工厂主数据列表。"""

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.contract import correlation_id, ok
from app.deps import DbSession

router = APIRouter()


@router.get("", status_code=200)
async def list_plants(request: Request, db: DbSession) -> dict:
    rows = await db.execute(
        text(
            "SELECT plant_code, plant_name, timezone, active "
            "FROM master_plants ORDER BY plant_code"
        )
    )
    plants = [
        {
            "plantCode": r[0],
            "plantName": r[1],
            "timezone": r[2],
            "active": r[3] == 1,
        }
        for r in rows.fetchall()
    ]
    return ok(plants, correlation_id(request.headers.get("X-Correlation-Id")))
