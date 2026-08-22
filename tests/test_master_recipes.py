"""``POST /api/v1/master/recipes`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/master/recipes`` 一节。
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


def _seed_material(tmp_path, code="MAT-PROD-01"):
    _exec(
        tmp_path,
        "INSERT INTO master_materials (material_code, material_name, unit_code) "
        f"VALUES ('{code}', 'Product', 'KG')",
    )


def _valid_body(**overrides):
    body = {
        "recipeCode": "REC-001",
        "version": "1",
        "recipeName": "Mixing",
        "productMaterialCode": "MAT-PROD-01",
        "targetBatchSize": 100.0,
        "unitCode": "KG",
        "status": "DRAFT",
        "components": [
            {
                "sequence": 10,
                "materialCode": "MAT-RAW-01",
                "phaseCode": "MIX",
                "targetQuantity": 25.0,
                "lowerLimit": 24.0,
                "upperLimit": 26.0,
                "unitCode": "KG",
                "required": True,
                "hazardous": False,
            }
        ],
        "parameters": [
            {
                "stepSequence": 1,
                "parameterCode": "TEMP",
                "parameterName": "Temperature",
                "targetValue": 50.0,
                "lowerLimit": 45.0,
                "upperLimit": 55.0,
                "unitCode": "C",
                "required": True,
            }
        ],
    }
    body.update(overrides)
    return body


def _post(client: TestClient, key: str, role: str = "PROCESS_ENGINEER", **overrides):
    return client.post(
        "/api/v1/master/recipes",
        json=_valid_body(**overrides),
        headers={"X-Idempotency-Key": key, "X-Actor-Role": role, "X-Actor-Id": "PE-1"},
    )


def test_recipe_create_draft_success(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_material(tmp_path)
        resp = _post(client, "k-1")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == {
        "recipeCode": "REC-001",
        "version": "1",
        "status": "DRAFT",
        "componentCount": 1,
        "parameterCount": 1,
    }

    recipe = _query(
        tmp_path,
        "SELECT recipe_name, status, approved_by, approved_at, effective_to "
        "FROM master_recipes WHERE recipe_code = 'REC-001'",
    )
    assert recipe[0][0] == "Mixing"
    assert recipe[0][1] == "DRAFT"
    assert recipe[0][2] is None
    assert recipe[0][3] is None
    assert recipe[0][4] is None

    comp = _query(
        tmp_path,
        "SELECT component_sequence, material_code, target_quantity, lower_limit, upper_limit, "
        "unit_code, required, hazardous FROM master_recipe_components",
    )
    assert len(comp) == 1
    assert comp[0] == (10, "MAT-RAW-01", 25.0, 24.0, 26.0, "KG", 1, 0)

    param = _query(
        tmp_path,
        "SELECT step_sequence, parameter_code, target_value, lower_limit, upper_limit, required "
        "FROM master_recipe_parameters",
    )
    assert len(param) == 1
    assert param[0] == (1, "TEMP", 50.0, 45.0, 55.0, 1)


def test_recipe_effective_writes_approval(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_material(tmp_path)
        resp = _post(client, "k-2", status="EFFECTIVE", approvedBy="APPROVER-1")

    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "EFFECTIVE"
    recipe = _query(
        tmp_path,
        "SELECT approved_by, approved_at FROM master_recipes WHERE recipe_code = 'REC-001'",
    )
    assert recipe[0][0] == "APPROVER-1"
    assert recipe[0][1]


def test_recipe_effective_default_approver(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_material(tmp_path)
        resp = _post(client, "k-3", status="EFFECTIVE")

    assert resp.status_code == 200
    recipe = _query(
        tmp_path,
        "SELECT approved_by FROM master_recipes WHERE recipe_code = 'REC-001'",
    )
    assert recipe[0][0] == "PE-1"  # 默认操作人


def test_recipe_component_defaults(tmp_path):
    """lower/upper 缺失各自默认目标值；unitCode 缺省继承配方；required/hazardous 默认。"""
    with _make_client(tmp_path) as client:
        _seed_material(tmp_path)
        resp = _post(
            client,
            "k-4",
            components=[
                {"sequence": 10, "materialCode": "MAT-RAW", "phaseCode": "MIX", "targetQuantity": 30.0}
            ],
        )

    assert resp.status_code == 200
    comp = _query(
        tmp_path,
        "SELECT lower_limit, upper_limit, unit_code, required, hazardous "
        "FROM master_recipe_components",
    )
    assert comp[0] == (30.0, 30.0, "KG", 1, 0)


def test_recipe_component_bool_helper(tmp_path):
    """布尔字段接受非零数字及 'true'/'TRUE'。"""
    with _make_client(tmp_path) as client:
        _seed_material(tmp_path)
        resp = _post(
            client,
            "k-5",
            components=[
                {
                    "sequence": 10,
                    "materialCode": "MAT-RAW",
                    "phaseCode": "MIX",
                    "targetQuantity": 30.0,
                    "required": 1,
                    "hazardous": "TRUE",
                }
            ],
        )

    assert resp.status_code == 200
    comp = _query(tmp_path, "SELECT required, hazardous FROM master_recipe_components")
    assert comp[0] == (1, 1)


def test_recipe_parameter_defaults_zero(tmp_path):
    """参数缺失数值均按 0。"""
    with _make_client(tmp_path) as client:
        _seed_material(tmp_path)
        resp = _post(
            client,
            "k-6",
            parameters=[
                {"stepSequence": 1, "parameterCode": "P", "unitCode": "U"}
            ],
        )

    assert resp.status_code == 200
    param = _query(
        tmp_path,
        "SELECT target_value, lower_limit, upper_limit, required "
        "FROM master_recipe_parameters",
    )
    assert param[0] == (0.0, 0.0, 0.0, 1)


def test_recipe_unknown_product_material_conflict(tmp_path):
    with _make_client(tmp_path) as client:
        # 不 seed 物料
        resp = _post(client, "k-7", productMaterialCode="MAT-NOPE")

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_recipe_idempotency(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_material(tmp_path)
        first = _post(client, "k-8")
        second = _post(client, "k-8")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["data"] == {"recipeId": "REC-001:1", "cached": True}


def test_recipe_role_before_idempotency(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_material(tmp_path)
        _post(client, "k-9")
        forbidden = _post(client, "k-9", role="MES_OPERATOR")

    assert forbidden.status_code == 403


def test_recipe_validation_main_fields(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_material(tmp_path)
        missing_rc = _post(client, "k-10", recipeCode="")
        bad_size = _post(client, "k-11", targetBatchSize="100")
        zero_size = _post(client, "k-12", targetBatchSize=0)
        no_unit = _post(client, "k-13", unitCode="")
        no_components = _post(client, "k-14", components=[])
        bad_status = _post(client, "k-15", status="BOGUS")
        empty_key = client.post(
            "/api/v1/master/recipes",
            json=_valid_body(),
            headers={"X-Actor-Role": "PROCESS_ENGINEER"},
        )

    assert missing_rc.status_code == 422
    assert bad_size.status_code == 422
    assert zero_size.status_code == 422
    assert no_unit.status_code == 422
    assert no_components.status_code == 422
    assert bad_status.status_code == 422
    assert empty_key.status_code == 422


def test_recipe_validation_component(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_material(tmp_path)
        bad_seq = _post(
            client, "k-16",
            components=[{"sequence": 0, "materialCode": "M", "phaseCode": "P", "targetQuantity": 1.0}],
        )
        bad_range = _post(
            client, "k-17",
            components=[{"sequence": 10, "materialCode": "M", "phaseCode": "P", "targetQuantity": 10.0, "lowerLimit": 20.0, "upperLimit": 5.0}],
        )
        non_positive_qty = _post(
            client, "k-18",
            components=[{"sequence": 10, "materialCode": "M", "phaseCode": "P", "targetQuantity": 0}],
        )

    assert bad_seq.status_code == 422
    assert bad_range.status_code == 422
    assert non_positive_qty.status_code == 422


def test_recipe_validation_parameter_range(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_material(tmp_path)
        bad_range = _post(
            client, "k-19",
            parameters=[{"stepSequence": 1, "parameterCode": "P", "unitCode": "U", "targetValue": 100, "lowerLimit": 0, "upperLimit": 50}],
        )

    assert bad_range.status_code == 422
