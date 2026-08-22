"""``POST /api/v1/batches`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/batches`` 一节。
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


def _seed(tmp_path, status="EFFECTIVE", effective_from="2026-01-01T00:00:00Z", effective_to=None):
    _exec(
        tmp_path,
        "INSERT INTO master_materials (material_code, material_name, unit_code) "
        "VALUES ('MAT-PROD', 'Product', 'KG')",
    )
    to_sql = "NULL" if effective_to is None else f"'{effective_to}'"
    _exec(
        tmp_path,
        "INSERT INTO master_recipes "
        "(recipe_code, version, recipe_name, product_material_code, target_batch_size, "
        f" unit_code, status, effective_from, effective_to, approved_by, approved_at) VALUES "
        f"('REC-1', '1', 'Mix', 'MAT-PROD', 100.0, 'KG', '{status}', '{effective_from}', {to_sql}, 'A', '2026-01-01T00:00:00Z')",
    )
    _exec(
        tmp_path,
        "INSERT INTO asset_equipment (equipment_code, plant_code, current_status) "
        "VALUES ('EQP-1', 'PLANT-A', 'IDLE')",
    )


def _valid_body(**overrides):
    body = {
        "batchNumber": "BATCH-001",
        "plantCode": "PLANT-A",
        "recipeCode": "REC-1",
        "recipeVersion": "1",
        "equipmentCode": "EQP-1",
        "plannedQuantity": 250.5,
    }
    body.update(overrides)
    return body


def _post(client: TestClient, key: str, role: str = "PLANNER", **overrides):
    return client.post(
        "/api/v1/batches",
        json=_valid_body(**overrides),
        headers={"X-Idempotency-Key": key, "X-Actor-Role": role, "X-Actor-Id": "P-1"},
    )


def test_batch_create_success(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = _post(client, "k-1")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == {
        "batchNumber": "BATCH-001",
        "status": "DRAFT",
        "recipeCode": "REC-1",
        "recipeVersion": "1",
        "plannedQuantity": 250.5,
    }

    batch = _query(
        tmp_path,
        "SELECT plant_code, product_material_code, unit_code, actual_quantity, version "
        "FROM production_batches WHERE batch_number = 'BATCH-001'",
    )
    assert batch[0][0] == "PLANT-A"
    assert batch[0][1] == "MAT-PROD"  # 从配方复制
    assert batch[0][2] == "KG"  # 从配方复制
    assert batch[0][3] == 0.0
    assert batch[0][4] == 0


def test_batch_create_recipe_not_found(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = _post(client, "k-2", recipeCode="REC-NOPE")

    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "recipe version not found"


def test_batch_create_recipe_not_effective(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path, status="DRAFT")
        resp = _post(client, "k-3")

    assert resp.status_code == 409
    assert resp.json()["error"]["message"] == "recipe version is not effective"


def test_batch_create_recipe_outside_period(tmp_path):
    with _make_client(tmp_path) as client:
        # effective_to 已过
        _seed(tmp_path, effective_to="2026-01-02T00:00:00Z")
        resp = _post(client, "k-4")

    assert resp.status_code == 409
    assert resp.json()["error"]["message"] == "recipe version is outside its effective period"


def test_batch_create_equipment_not_found(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = _post(client, "k-5", equipmentCode="EQP-NOPE")

    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "equipment not found"


def test_batch_create_equipment_wrong_plant(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = _post(client, "k-6", plantCode="PLANT-B")

    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "equipment does not belong to plant"


def test_batch_create_admin_forbidden(tmp_path):
    """MES_ADMIN 不在允许列表。"""
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = _post(client, "k-7", role="MES_ADMIN")

    assert resp.status_code == 403


def test_batch_create_idempotency(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        first = _post(client, "k-8")
        second = _post(client, "k-8")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["data"] == {"batchNumber": "BATCH-001", "cached": True}


def test_batch_create_validation(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        missing = _post(client, "k-9", batchNumber="")
        bad_qty = _post(client, "k-10", plannedQuantity="250")
        zero_qty = _post(client, "k-11", plannedQuantity=0)
        empty_key = client.post(
            "/api/v1/batches", json=_valid_body(), headers={"X-Actor-Role": "PLANNER"}
        )

    assert missing.status_code == 422
    assert bad_qty.status_code == 422
    assert zero_qty.status_code == 422
    assert empty_key.status_code == 422


def test_batch_create_duplicate_conflict(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        _post(client, "k-12")
        resp = _post(client, "k-13")

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"
