"""``POST /api/v1/master/plants`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/master/plants`` 一节。
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
        "/api/v1/master/plants",
        json=body,
        headers={**ADMIN, "X-Idempotency-Key": key, **headers},
    )


def test_create_plant_ok(tmp_path):
    """正常创建返回 plantCode/plantName/active=true。"""
    with _make_client(tmp_path) as client:
        resp = _post(client, "k1", {"plantCode": "PLANT-Z", "plantName": "测试工厂Z", "timezone": "Asia/Shanghai"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["plantCode"] == "PLANT-Z"
    assert data["plantName"] == "测试工厂Z"
    assert data["active"] is True


def test_create_plant_idempotent(tmp_path):
    """相同幂等键返回首次创建的 plantCode 和 cached=true，不重复写库。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    with _make_client(tmp_path) as client:
        _post(client, "k-dup", {"plantCode": "PLANT-DUP"})
        resp = _post(client, "k-dup", {"plantCode": "PLANT-DUP"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["plantCode"] == "PLANT-DUP"
    assert data["cached"] is True

    async def _count():
        engine = await create_db_engine(db_url)
        async with engine.connect() as conn:
            n = (
                await conn.execute(
                    sqltext("SELECT COUNT(*) FROM master_plants WHERE plant_code='PLANT-DUP'")
                )
            ).scalar()
        await engine.dispose()
        return n

    n = asyncio.new_event_loop().run_until_complete(_count())
    assert n == 1


def test_create_plant_forbidden_without_admin(tmp_path):
    """非 MES_ADMIN 角色返回 403 FORBIDDEN。"""
    with _make_client(tmp_path) as client:
        resp = client.post(
            "/api/v1/master/plants",
            json={"plantCode": "PLANT-X"},
            headers={"X-Actor-Role": "MES_OPERATOR", "X-Idempotency-Key": "k2"},
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert "generatedAt" not in resp.json()["meta"]


def test_create_plant_validation(tmp_path):
    """缺少幂等键或 plantCode 返回 422 VALIDATION_ERROR。"""
    with _make_client(tmp_path) as client:
        resp = client.post("/api/v1/master/plants", json={"plantCode": "PLANT-X"}, headers=ADMIN)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

        resp = _post(client, "k3", {"plantName": "no code"})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_plant_invalid_json(tmp_path):
    """无效 JSON 返回 422 VALIDATION_ERROR。"""
    with _make_client(tmp_path) as client:
        resp = client.post(
            "/api/v1/master/plants",
            content=b"{not-json",
            headers={**ADMIN, "X-Idempotency-Key": "k9"},
        )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_plant_conflict(tmp_path):
    """重复 plantCode 返回 409 CONFLICT。"""
    with _make_client(tmp_path) as client:
        _post(client, "k4", {"plantCode": "PLANT-C"})
        resp = _post(client, "k5", {"plantCode": "PLANT-C"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_create_plant_audit_written(tmp_path):
    """创建成功后写入审计事件。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    with _make_client(tmp_path) as client:
        _post(client, "k6", {"plantCode": "PLANT-AUDIT"})

    async def _check():
        engine = await create_db_engine(db_url)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    sqltext("SELECT action, resource_type, resource_id FROM audit_events")
                )
            ).first()
        await engine.dispose()
        return row

    row = asyncio.new_event_loop().run_until_complete(_check())
    assert row is not None
    assert row[0] == "PLANT_CREATE"
    assert row[1] == "PLANT"
    assert row[2] == "PLANT-AUDIT"
