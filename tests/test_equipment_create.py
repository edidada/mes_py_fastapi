"""``POST /api/v1/master/equipment`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/master/equipment`` 一节。
"""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import text as sqltext

from app.config import Settings
from app.database import create_db_engine
from app.main import create_app

ADMIN = {"X-Actor-Role": "MES_ADMIN"}


def _make_client(tmp_path) -> TestClient:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    return TestClient(create_app(settings))


def _post(client: TestClient, key: str, body: dict, **headers):
    return client.post(
        "/api/v1/master/equipment",
        json=body,
        headers={**ADMIN, "X-Idempotency-Key": key, **headers},
    )


def test_create_equipment_ok(tmp_path):
    """正常创建返回 equipmentCode 与 currentStatus=OFFLINE。"""
    with _make_client(tmp_path) as client:
        resp = _post(client, "e1", {"equipmentCode": "EQ-01", "plantCode": "PLANT-A", "equipmentName": "数控机床", "criticality": "HIGH"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["equipmentCode"] == "EQ-01"
    assert data["currentStatus"] == "OFFLINE"


def test_create_equipment_defaults(tmp_path):
    """plantCode 缺省→PLANT-A；criticality 缺省→NORMAL；currentStatus 强制 OFFLINE。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    with _make_client(tmp_path) as client:
        _post(client, "e2", {"equipmentCode": "EQ-DEF"})

    async def _row():
        engine = await create_db_engine(db_url)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    sqltext("SELECT plant_code, criticality, current_status FROM asset_equipment WHERE equipment_code='EQ-DEF'")
                )
            ).first()
        await engine.dispose()
        return row

    row = asyncio.new_event_loop().run_until_complete(_row())
    assert row[0] == "PLANT-A"
    assert row[1] == "NORMAL"
    assert row[2] == "OFFLINE"


def test_create_equipment_ignores_extra_fields(tmp_path):
    """currentStatus/active/lastHeartbeatAt 被忽略。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    with _make_client(tmp_path) as client:
        _post(client, "e3", {"equipmentCode": "EQ-IGN", "currentStatus": "RUNNING", "active": True, "lastHeartbeatAt": "2026-01-01T00:00:00Z"})

    async def _row():
        engine = await create_db_engine(db_url)
        async with engine.connect() as conn:
            status, hb = (
                await conn.execute(
                    sqltext("SELECT current_status, last_heartbeat_at FROM asset_equipment WHERE equipment_code='EQ-IGN'")
                )
            ).first()
        await engine.dispose()
        return status, hb

    status, hb = asyncio.new_event_loop().run_until_complete(_row())
    assert status == "OFFLINE"
    assert hb is None


def test_create_equipment_idempotent(tmp_path):
    """相同幂等键返回 equipmentCode + cached=true。"""
    with _make_client(tmp_path) as client:
        _post(client, "e-dup", {"equipmentCode": "EQ-DUP"})
        resp = _post(client, "e-dup", {"equipmentCode": "EQ-DUP"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["equipmentCode"] == "EQ-DUP"
    assert data["cached"] is True


def test_create_equipment_forbidden(tmp_path):
    """非 MES_ADMIN 返回 403。"""
    with _make_client(tmp_path) as client:
        resp = client.post(
            "/api/v1/master/equipment",
            json={"equipmentCode": "EQ-X"},
            headers={"X-Actor-Role": "MES_OPERATOR", "X-Idempotency-Key": "e4"},
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_create_equipment_validation(tmp_path):
    """缺少幂等键或 equipmentCode 返回 422。"""
    with _make_client(tmp_path) as client:
        resp = client.post("/api/v1/master/equipment", json={"equipmentCode": "EQ-X"}, headers=ADMIN)
        assert resp.status_code == 422

        resp = _post(client, "e5", {"equipmentName": "no code"})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_equipment_conflict(tmp_path):
    """重复 equipmentCode 返回 409。"""
    with _make_client(tmp_path) as client:
        _post(client, "e6", {"equipmentCode": "EQ-C"})
        resp = _post(client, "e7", {"equipmentCode": "EQ-C"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"
