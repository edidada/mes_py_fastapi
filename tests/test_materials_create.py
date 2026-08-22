"""``POST /api/v1/master/materials`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/master/materials`` 一节。
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
        "/api/v1/master/materials",
        json=body,
        headers={**ADMIN, "X-Idempotency-Key": key, **headers},
    )


def test_create_material_ok(tmp_path):
    """正常创建返回 materialCode/materialName。"""
    with _make_client(tmp_path) as client:
        resp = _post(
            client,
            "m1",
            {"materialCode": "MAT-001", "materialName": "电阻", "unitCode": "EA", "lotControlled": True, "serialControlled": False},
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["materialCode"] == "MAT-001"
    assert data["materialName"] == "电阻"


def test_create_material_defaults(tmp_path):
    """unitCode 空/缺省时存 EA；lotControlled 默认 false。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    with _make_client(tmp_path) as client:
        _post(client, "m2", {"materialCode": "MAT-DEF"})
        _post(client, "m3", {"materialCode": "MAT-UNIT", "unitCode": ""})

    async def _rows():
        engine = await create_db_engine(db_url)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    sqltext("SELECT unit_code, lot_controlled FROM master_materials ORDER BY material_code")
                )
            ).all()
        await engine.dispose()
        return rows

    rows = asyncio.new_event_loop().run_until_complete(_rows())
    assert rows[0][0] == "EA"
    assert rows[0][1] == 0
    assert rows[1][0] == "EA"


def test_create_material_bool_conversion(tmp_path):
    """string/number 布尔转换：true/TRUE→1；非零数字→1；其他→0。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    with _make_client(tmp_path) as client:
        _post(client, "m4", {"materialCode": "MAT-B1", "lotControlled": "TRUE", "serialControlled": 5})
        _post(client, "m5", {"materialCode": "MAT-B2", "lotControlled": "false", "serialControlled": 0})

    async def _rows():
        engine = await create_db_engine(db_url)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    sqltext("SELECT material_code, lot_controlled, serial_controlled FROM master_materials ORDER BY material_code")
                )
            ).all()
        await engine.dispose()
        return rows

    rows = asyncio.new_event_loop().run_until_complete(_rows())
    assert rows[0] == ("MAT-B1", 1, 1)
    assert rows[1] == ("MAT-B2", 0, 0)


def test_create_material_idempotent(tmp_path):
    """相同幂等键返回首次 materialCode 和 cached=true。"""
    with _make_client(tmp_path) as client:
        _post(client, "m-dup", {"materialCode": "MAT-DUP"})
        resp = _post(client, "m-dup", {"materialCode": "MAT-DUP"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["materialCode"] == "MAT-DUP"
    assert data["cached"] is True
    assert "materialName" not in data


def test_create_material_forbidden(tmp_path):
    """非 MES_ADMIN 返回 403。"""
    with _make_client(tmp_path) as client:
        resp = client.post(
            "/api/v1/master/materials",
            json={"materialCode": "MAT-X"},
            headers={"X-Actor-Role": "MES_OPERATOR", "X-Idempotency-Key": "m6"},
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_create_material_validation(tmp_path):
    """缺少幂等键或 materialCode 返回 422。"""
    with _make_client(tmp_path) as client:
        resp = client.post("/api/v1/master/materials", json={"materialCode": "MAT-X"}, headers=ADMIN)
        assert resp.status_code == 422

        resp = _post(client, "m7", {"materialName": "no code"})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_material_conflict(tmp_path):
    """重复 materialCode 返回 409。"""
    with _make_client(tmp_path) as client:
        _post(client, "m8", {"materialCode": "MAT-C"})
        resp = _post(client, "m9", {"materialCode": "MAT-C"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"
