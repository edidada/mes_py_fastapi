"""``GET /api/v1/dashboard`` 综合看板。

不校验角色。可选 ``plantCode`` 缺失默认 ``PLANT-A``；参数存在但空串使用空串。
四个数据源各自查询失败按 C++ ``queryOne/queryAll`` 行为降级为空数据：
- ``v_dashboard_current``：生产汇总，无行/``last_event_at`` 空时 ``lastEventAt``
  使用当前 UTC；计划达成率 ``completed/planned*100``、一次合格率
  ``good/(completed+rejected)*100``，分母不大于 0 时为 0。
- ``v_wip_by_operation``：累加 ``IN_PROCESS/REWORK/HOLD``，``total`` 为三者之和。
- ``reporting_equipment_oee_hourly``：跨设备/小时求和后算可用/性能/质量/OEE。
- ``v_active_andons``：统计未关闭的 ``CRITICAL`` 与 ``WARNING``，
  ``unacknowledged`` 等于两者之和。
"""

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from app.contract import correlation_id, ok, utc_now
from app.deps import DbSession

router = APIRouter()


def _safe_div(num: float, den: float) -> float:
    return num / den if den > 0 else 0.0


@router.get("", status_code=200)
async def get_dashboard(
    request: Request, db: DbSession, plantCode: str | None = Query(default=None)
) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    plant = "PLANT-A" if plantCode is None else plantCode
    now = utc_now()

    # 1. 生产汇总
    planned = completed = good = rejected = 0.0
    last_event_at = now
    try:
        row = (
            await db.execute(
                text(
                    "SELECT planned_quantity, completed_quantity, good_quantity, "
                    "       rejected_quantity, last_event_at "
                    "FROM v_dashboard_current WHERE plant_code = :p"
                ),
                {"p": plant},
            )
        ).first()
        if row is not None:
            planned = float(row[0]) if row[0] is not None else 0.0
            completed = float(row[1]) if row[1] is not None else 0.0
            good = float(row[2]) if row[2] is not None else 0.0
            rejected = float(row[3]) if row[3] is not None else 0.0
            if row[4]:
                last_event_at = row[4]
    except Exception:
        pass

    production = {
        "plannedQuantity": planned,
        "completedQuantity": completed,
        "goodQuantity": good,
        "rejectedQuantity": rejected,
        "planAttainmentPercent": _safe_div(completed, planned) * 100,
        "firstPassYieldPercent": _safe_div(good, completed + rejected) * 100,
    }

    # 2. WIP
    processing = rework = hold = 0
    try:
        rows = (
            await db.execute(
                text(
                    "SELECT status, COUNT(*) FROM v_wip_by_operation "
                    "GROUP BY status"
                )
            )
        ).fetchall()
        for r in rows:
            status = r[0]
            cnt = r[1] if r[1] is not None else 0
            if status == "IN_PROCESS":
                processing = cnt
            elif status == "REWORK":
                rework = cnt
            elif status == "HOLD":
                hold = cnt
    except Exception:
        pass
    wip = {
        "total": processing + hold + rework,
        "processing": processing,
        "hold": hold,
        "rework": rework,
    }

    # 3. OEE
    running = planned_sec = actual = ideal = good_out = 0.0
    try:
        row = (
            await db.execute(
                text(
                    "SELECT SUM(COALESCE(running_seconds, 0)), "
                    "       SUM(COALESCE(planned_seconds, 0)), "
                    "       SUM(COALESCE(actual_output, 0)), "
                    "       SUM(COALESCE(ideal_cycle_seconds, 0)), "
                    "       SUM(COALESCE(good_output, 0)) "
                    "FROM reporting_equipment_oee_hourly"
                )
            )
        ).first()
        if row is not None:
            running = float(row[0]) if row[0] is not None else 0.0
            planned_sec = float(row[1]) if row[1] is not None else 0.0
            actual = float(row[2]) if row[2] is not None else 0.0
            ideal = float(row[3]) if row[3] is not None else 0.0
            good_out = float(row[4]) if row[4] is not None else 0.0
    except Exception:
        pass
    availability = _safe_div(running, planned_sec)
    performance = _safe_div(actual, ideal)
    quality = _safe_div(good_out, actual)
    oee = {
        "availabilityPercent": availability * 100,
        "performancePercent": performance * 100,
        "qualityPercent": quality * 100,
        "oeePercent": availability * performance * quality * 100,
    }

    # 4. 活动安灯
    critical = warning = 0
    try:
        rows = (
            await db.execute(
                text(
                    "SELECT severity, COUNT(*) FROM v_active_andons "
                    "WHERE severity IN ('CRITICAL', 'WARNING') GROUP BY severity"
                )
            )
        ).fetchall()
        for r in rows:
            if r[0] == "CRITICAL":
                critical = r[1] if r[1] is not None else 0
            elif r[0] == "WARNING":
                warning = r[1] if r[1] is not None else 0
    except Exception:
        pass
    andon = {
        "critical": critical,
        "warning": warning,
        "unacknowledged": critical + warning,
    }

    return ok(
        {
            "scope": {"plantCode": plant},
            "production": production,
            "wip": wip,
            "oee": oee,
            "andon": andon,
            "freshness": {
                "lastEventAt": last_event_at,
                "delaySeconds": 0,
                "stale": False,
            },
        },
        corr,
    )
