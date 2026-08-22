"""``POST /api/v1/maintenance-work-orders`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/maintenance-work-orders`` 一节。
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


def _query(tmp_path, sql, **params):
    async def _run():
        engine = await create_db_engine(_db_url(tmp_path))
        async with engine.connect() as conn:
            result = await conn.execute(sqltext(sql), params)
            rows = result.fetchall()
        await engine.dispose()
        return rows

    return asyncio.new_event_loop().run_until_complete(_run())


def _setup(tmp_path):
    _exec(
        tmp_path,
        "INSERT INTO asset_equipment "
        "(equipment_code, plant_code, work_center_code, equipment_name, criticality, current_status, last_heartbeat_at) "
        "VALUES ('EQP-1', 'PLANT-A', 'WC-1', 'CNC-01', 'HIGH', 'RUNNING', '2026-08-22T09:00:00Z')",
    )


def _post(client: TestClient, key: str, role: str = "EQUIPMENT_ENGINEER", **overrides):
    body = {
        "equipmentCode": "EQP-1",
        "maintenanceType": "PREVENTIVE",
        "description": "更换润滑油",
        "assigneeId": "W-004",
        "dueAt": "2026-08-31T00:00:00Z",
    }
    body.update(overrides)
    return client.post(
        "/api/v1/maintenance-work-orders",
        json=body,
        headers={"X-Idempotency-Key": key, "X-Actor-Role": role, "X-Actor-Id": "W-004"},
    )


def test_mwo_created(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-ok")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["maintenanceWorkOrderId"].startswith("MWO-")
    assert data["status"] == "OPEN"

    rows = _query(
        tmp_path,
        "SELECT equipment_code, maintenance_type, description, assignee_id, status, due_at, completed_at "
        "FROM asset_maintenance_work_orders",
    )
    assert len(rows) == 1
    assert rows[0][0] == "EQP-1"
    assert rows[0][1] == "PREVENTIVE"
    assert rows[0][2] == "更换润滑油"
    assert rows[0][3] == "W-004"
    assert rows[0][4] == "OPEN"
    assert rows[0][5] == "2026-08-31T00:00:00Z"
    assert rows[0][6] is None


def test_mwo_default_maintenance_type(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-default", maintenanceType=None)

    assert resp.status_code == 200
    rows = _query(
        tmp_path,
        "SELECT maintenance_type, equipment_code FROM asset_maintenance_work_orders",
    )
    assert rows[0][0] == "CORRECTIVE"
    assert rows[0][1] == "EQP-1"


def test_mwo_empty_body_still_ok(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.post(
            "/api/v1/maintenance-work-orders",
            json={},
            headers={"X-Idempotency-Key": "k-empty", "X-Actor-Role": "EQUIPMENT_ENGINEER"},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["maintenanceWorkOrderId"].startswith("MWO-")
    assert resp.json()["data"]["status"] == "OPEN"
    # 空设备外键 → 主记录不落库，但成功响应存在
    rows = _query(tmp_path, "SELECT maintenance_work_order_id FROM asset_maintenance_work_orders")
    assert len(rows) == 0


def test_mwo_unknown_equipment_still_ok(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-unknown", equipmentCode="EQP-NOPE")

    assert resp.status_code == 200
    rows = _query(tmp_path, "SELECT maintenance_work_order_id FROM asset_maintenance_work_orders")
    assert len(rows) == 0


def test_mwo_invalid_maintenance_type_still_ok(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-bad", maintenanceType="BOGUS")

    assert resp.status_code == 200
    rows = _query(tmp_path, "SELECT maintenance_work_order_id FROM asset_maintenance_work_orders")
    assert len(rows) == 0


def test_mwo_idempotency(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        first = _post(client, "k-idem")
        second = _post(client, "k-idem")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"]["maintenanceWorkOrderId"] == second.json()["data"]["maintenanceWorkOrderId"]
    assert second.json()["data"]["cached"] is True


def test_mwo_forbidden_role(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-403", role="MES_OPERATOR")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_mwo_validation(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        invalid_json = client.post(
            "/api/v1/maintenance-work-orders",
            content="{bad",
            headers={"X-Idempotency-Key": "k-422", "X-Actor-Role": "EQUIPMENT_ENGINEER"},
        )
        empty_key = client.post(
            "/api/v1/maintenance-work-orders",
            json={},
            headers={"X-Actor-Role": "EQUIPMENT_ENGINEER"},
        )

    assert invalid_json.status_code == 422
    assert invalid_json.json()["error"]["code"] == "VALIDATION_ERROR"
    assert empty_key.status_code == 422
    assert empty_key.json()["error"]["code"] == "VALIDATION_ERROR"
