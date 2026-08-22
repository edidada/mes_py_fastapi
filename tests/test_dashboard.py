"""``GET /api/v1/dashboard`` 接口契约测试。

契约依据：docs/http-api.md 中 ``GET /api/v1/dashboard`` 一节。
"""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import text as sqltext

from app.config import Settings
from app.database import create_db_engine
from app.main import create_app


def _make_client(tmp_path) -> TestClient:
    return TestClient(create_app(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")))


def _db_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


def _exec(tmp_path, sql, **params):
    async def _run():
        engine = await create_db_engine(_db_url(tmp_path))
        async with engine.begin() as conn:
            await conn.execute(sqltext(sql), params)
        await engine.dispose()

    asyncio.new_event_loop().run_until_complete(_run())


def _seed(tmp_path):
    # 班次生产汇总
    _exec(
        tmp_path,
        "INSERT INTO reporting_production_shift_summary "
        "(plant_code, shift_date, shift_code, planned_quantity, completed_quantity, "
        " good_quantity, rejected_quantity, last_event_at) VALUES "
        "('PLANT-A', '2026-08-22', 'DAY', 150, 90, 80, 10, '2026-08-22T00:00:00Z'),"
        "('PLANT-A', '2026-08-22', 'NIGHT', 0, 0, 0, 0, '2026-08-22T01:00:00Z')",
    )
    # 工单 + 工序 + 产品单元（WIP）
    _exec(
        tmp_path,
        "INSERT INTO production_work_orders (work_order_number, plant_code, status) VALUES "
        "('WO-1', 'PLANT-A', 'RELEASED')",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_work_order_operations (work_order_number, sequence, operation_code) VALUES "
        "('WO-1', 10, 'OP-10')",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_product_units (serial_number, work_order_number, status) VALUES "
        "('U-1', 'WO-1', 'IN_PROCESS'),"
        "('U-2', 'WO-1', 'HOLD'),"
        "('U-3', 'WO-1', 'REWORK'),"
        "('U-4', 'WO-1', 'WAITING_INSPECTION')",  # 不计数
    )
    # OEE
    _exec(
        tmp_path,
        "INSERT INTO reporting_equipment_oee_hourly "
        "(equipment_code, hour_bucket, actual_output, good_output, running_seconds, "
        " planned_seconds, ideal_cycle_seconds) VALUES "
        "('EQP-1', '2026-08-22T00', 80, 72, 5400, 7200, 100),"
        "('EQP-2', '2026-08-22T00', 20, 18, 1800, 2400, 20)",
    )
    # 安灯
    _exec(
        tmp_path,
        "INSERT INTO trace_andon_events "
        "(andon_id, plant_code, severity, category, message, raised_at, closed_at) VALUES "
        "('AND-C1', 'PLANT-A', 'CRITICAL', 'QUALITY', 'a', '2026-08-22T01:00:00Z', NULL),"
        "('AND-W1', 'PLANT-A', 'WARNING', 'QUALITY', 'b', '2026-08-22T02:00:00Z', NULL),"
        "('AND-W2', 'PLANT-A', 'WARNING', 'QUALITY', 'c', '2026-08-22T03:00:00Z', '2026-08-22T04:00:00Z')",  # 已关闭不计数
    )


def test_dashboard_full(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = client.get("/api/v1/dashboard", headers={"X-Correlation-Id": "dash-1"})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert resp.json()["meta"]["correlationId"] == "dash-1"
    assert data["scope"] == {"plantCode": "PLANT-A"}

    prod = data["production"]
    assert prod["plannedQuantity"] == 150
    assert prod["completedQuantity"] == 90
    assert prod["goodQuantity"] == 80
    assert prod["rejectedQuantity"] == 10
    assert prod["planAttainmentPercent"] == 60.0  # 90/150*100
    assert prod["firstPassYieldPercent"] == 80.0  # 80/(90+10)*100

    wip = data["wip"]
    assert wip == {"total": 3, "processing": 1, "hold": 1, "rework": 1}

    oee = data["oee"]
    # running=7200, planned=9600 → 75
    # actual=100, ideal=120 → 83.333...
    # good=90, actual=100 → 90
    assert oee["availabilityPercent"] == 75.0
    assert abs(oee["performancePercent"] - 83.33333333333334) < 0.001
    assert oee["qualityPercent"] == 90.0
    assert abs(oee["oeePercent"] - (0.75 * (100/120) * 0.9 * 100)) < 0.001

    andon = data["andon"]
    assert andon == {"critical": 1, "warning": 1, "unacknowledged": 2}

    fresh = data["freshness"]
    assert fresh["lastEventAt"] == "2026-08-22T01:00:00Z"
    assert fresh["delaySeconds"] == 0
    assert fresh["stale"] is False


def test_dashboard_empty_all_zero(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.get("/api/v1/dashboard")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["scope"] == {"plantCode": "PLANT-A"}
    assert data["production"]["plannedQuantity"] == 0
    assert data["production"]["planAttainmentPercent"] == 0
    assert data["wip"] == {"total": 0, "processing": 0, "hold": 0, "rework": 0}
    assert data["oee"] == {
        "availabilityPercent": 0,
        "performancePercent": 0,
        "qualityPercent": 0,
        "oeePercent": 0,
    }
    assert data["andon"] == {"critical": 0, "warning": 0, "unacknowledged": 0}
    # 无汇总行 → lastEventAt 为当前时间
    assert data["freshness"]["lastEventAt"]
    assert data["freshness"]["stale"] is False


def test_dashboard_plant_code_filter(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = client.get("/api/v1/dashboard", params={"plantCode": "PLANT-B"})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["scope"] == {"plantCode": "PLANT-B"}
    # 无 PLANT-B 汇总 → 全零
    assert data["production"]["plannedQuantity"] == 0
    # WIP 不按工厂过滤（视图无 plant_code 列）
    assert data["wip"]["total"] == 3


def test_dashboard_empty_string_plant_code(tmp_path):
    """参数存在但空串 → 不回退默认，使用空串。"""
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = client.get("/api/v1/dashboard", params={"plantCode": ""})

    assert resp.status_code == 200
    assert resp.json()["data"]["scope"] == {"plantCode": ""}
    assert resp.json()["data"]["production"]["plannedQuantity"] == 0
