"""``GET /api/v1/master/recipes/{recipe}/versions/{version}`` 接口契约测试。

契约依据：docs/http-api.md 中同名一节。
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
    _exec(
        tmp_path,
        "INSERT INTO master_materials (material_code, material_name, unit_code) "
        "VALUES ('MAT-PROD', 'Product', 'KG')",
    )
    _exec(
        tmp_path,
        "INSERT INTO master_recipes "
        "(recipe_code, version, recipe_name, product_material_code, target_batch_size, "
        " unit_code, status, effective_from, effective_to, approved_by, approved_at) VALUES "
        "('REC-1', '1', 'Mixing', 'MAT-PROD', 100.0, 'KG', 'DRAFT', '2026-08-01T00:00:00Z', NULL, NULL, NULL)",
    )
    _exec(
        tmp_path,
        "INSERT INTO master_recipe_components "
        "(recipe_code, version, component_sequence, material_code, phase_code, "
        " target_quantity, lower_limit, upper_limit, unit_code, required, hazardous) VALUES "
        "('REC-1', '1', 20, 'MAT-B', 'MIX', 30.0, 28.0, 32.0, 'KG', 0, 1),"
        "('REC-1', '1', 10, 'MAT-A', 'MIX', 25.0, 24.0, 26.0, 'KG', 1, 0)",
    )
    _exec(
        tmp_path,
        "INSERT INTO master_recipe_parameters "
        "(recipe_code, version, step_sequence, parameter_code, parameter_name, "
        " target_value, lower_limit, upper_limit, unit_code, required) VALUES "
        "('REC-1', '1', 1, 'TEMP-B', 'TempB', 60.0, 55.0, 65.0, 'C', 0),"
        "('REC-1', '1', 1, 'TEMP-A', 'TempA', 50.0, 45.0, 55.0, 'C', 1),"
        "('REC-1', '1', 2, 'PRES', 'Pressure', 100.0, 90.0, 110.0, 'kPa', 1)",
    )


def test_recipe_version_full(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = client.get(
            "/api/v1/master/recipes/REC-1/versions/1",
            headers={"X-Correlation-Id": "rv-1"},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert resp.json()["meta"]["correlationId"] == "rv-1"

    recipe = data["recipe"]
    # snake_case 列名 + nullable 字段保持 null
    assert recipe == {
        "recipe_code": "REC-1",
        "version": "1",
        "recipe_name": "Mixing",
        "product_material_code": "MAT-PROD",
        "target_batch_size": 100.0,
        "unit_code": "KG",
        "status": "DRAFT",
        "effective_from": "2026-08-01T00:00:00Z",
        "effective_to": None,
        "approved_by": None,
        "approved_at": None,
    }

    # 组分按 component_sequence 升序
    comps = data["components"]
    assert [c["component_sequence"] for c in comps] == [10, 20]
    assert comps[0]["required"] == 1
    assert comps[0]["hazardous"] == 0
    assert comps[1]["required"] == 0
    assert comps[1]["hazardous"] == 1

    # 参数按 step_sequence, parameter_code 升序
    params = data["parameters"]
    assert [(p["step_sequence"], p["parameter_code"]) for p in params] == [
        (1, "TEMP-A"), (1, "TEMP-B"), (2, "PRES"),
    ]


def test_recipe_version_empty_children(tmp_path):
    with _make_client(tmp_path) as client:
        _exec(
            tmp_path,
            "INSERT INTO master_materials (material_code) VALUES ('M')",
        )
        _exec(
            tmp_path,
            "INSERT INTO master_recipes "
            "(recipe_code, version, product_material_code, target_batch_size, unit_code, status) "
            "VALUES ('REC-2', '2', 'M', 50.0, 'KG', 'DRAFT')",
        )
        resp = client.get("/api/v1/master/recipes/REC-2/versions/2")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["components"] == []
    assert data["parameters"] == []
    assert data["recipe"]["recipe_name"] is None


def test_recipe_version_not_found(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.get("/api/v1/master/recipes/REC-NOPE/versions/9")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert resp.json()["error"]["message"] == "recipe version not found"
