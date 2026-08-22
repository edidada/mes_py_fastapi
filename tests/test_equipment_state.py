"""``GET /api/v1/equipment/{equipmentCode}/state`` 接口契约测试。

契约依据：docs/http-api.md 中 ``GET /api/v1/equipment/{equipmentCode}/state`` 一节。
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


def _setup(tmp_path):
    _exec(
        tmp_path,
        "INSERT INTO asset_equipment "
        "(equipment_code, plant_code, work_center_code, equipment_name, criticality, current_status, last_heartbeat_at) "
        "VALUES ('EQP-1', 'PLANT-A', 'WC-1', 'CNC-01', 'HIGH', 'RUNNING', '2026-08-21T01:00:00Z'),"
        "('EQP-2', 'PLANT-A', 'WC-1', 'CNC-02', 'MEDIUM', 'IDLE', NULL)",
    )
    _exec(
        tmp_path,
        "INSERT INTO asset_maintenance_work_orders "
        "(maintenance_work_order_id, equipment_code, maintenance_type, description, assignee_id, status, due_at, completed_at) VALUES "
        "('MWO-1', 'EQP-1', 'CORRECTIVE', 'a', '', 'OPEN', '', NULL),"
        "('MWO-2', 'EQP-1', 'PREVENTIVE', 'b', '', 'COMPLETED', '', '2026-08-22T00:00:00Z'),"
        "('MWO-3', 'EQP-1', 'PREDICTIVE', 'c', '', 'MAINTENANCE', '', NULL)",
    )


def test_equipment_state_found(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.get("/api/v1/equipment/EQP-1/state")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["equipmentCode"] == "EQP-1"
    assert data["plantCode"] == "PLANT-A"
    assert data["status"] == "RUNNING"
    assert data["lastHeartbeatAt"] == "2026-08-21T01:00:00Z"
    assert isinstance(data["heartbeatAgeSeconds"], int)
    # 仅 OPEN 与 MAINTENANCE 计入（COMPLETED 排除）
    assert data["openMaintenanceCount"] == 2


def test_equipment_state_null_heartbeat(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.get("/api/v1/equipment/EQP-2/state")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "IDLE"
    assert data["lastHeartbeatAt"] == ""
    assert data["heartbeatAgeSeconds"] == 0


def test_equipment_not_found(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.get("/api/v1/equipment/EQP-NOPE/state")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert resp.json()["error"]["message"] == "equipment not found"


def test_equipment_state_ignores_query_and_body(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.request(
            "GET",
            "/api/v1/equipment/EQP-1/state?foo=bar",
            content='{"ignored": true}',
            headers={"X-Correlation-Id": "eq-state-1"},
        )

    assert resp.status_code == 200
    assert resp.json()["meta"]["correlationId"] == "eq-state-1"
    assert resp.json()["data"]["equipmentCode"] == "EQP-1"
