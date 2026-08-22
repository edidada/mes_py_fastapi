"""``POST /api/v1/quality/inspection-lots`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/quality/inspection-lots`` 一节。
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


def _setup_plan(tmp_path, sample_size=5):
    _exec(
        tmp_path,
        "INSERT INTO quality_inspection_plans "
        "(material_code, plan_code, version, inspection_type, status, effective_from) "
        "VALUES ('MAT-A', 'PLAN-A', '1', 'PATROL', 'EFFECTIVE', '2026-01-01')",
    )
    _exec(
        tmp_path,
        "INSERT INTO quality_sampling_rules (plan_code, version, sample_size) "
        "VALUES ('PLAN-A', '1', :ss)",
        ss=sample_size,
    )


def _create(client: TestClient, key: str, role: str = "QUALITY_ENGINEER", **overrides):
    body = {
        "plantCode": "PLANT-A",
        "materialCode": "MAT-A",
        "workOrderNumber": "WO-A",
        "serialNumber": "SN-1",
        "inspectionType": "PATROL",
    }
    body.update(overrides)
    headers = {"X-Idempotency-Key": key, "X-Actor-Role": role}
    return client.post("/api/v1/quality/inspection-lots", json=body, headers=headers)


def test_create_lot_ok(tmp_path):
    """创建成功：ILOT-...、PENDING、样本数、落库与审计。"""
    with _make_client(tmp_path) as client:
        _setup_plan(tmp_path)
        resp = _create(client, "k1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["lotId"].startswith("ILOT-")
        assert data["status"] == "PENDING"
        assert data["sampleSize"] == 5
        assert "cached" not in data

        lot = _query(
            tmp_path,
            "SELECT plant_code, material_code, inspection_type, plan_code, plan_version, sample_size, status "
            "FROM quality_inspection_lots",
        )
        assert lot[0][0] == "PLANT-A"
        assert lot[0][1] == "MAT-A"
        assert lot[0][2] == "PATROL"
        assert lot[0][3] == "PLAN-A"
        assert lot[0][4] == "1"
        assert lot[0][5] == 5
        assert lot[0][6] == "PENDING"

        audit = _query(tmp_path, "SELECT action, resource_type FROM audit_events")
        assert audit[0][0] == "INSPECTION_LOT_CREATE"
        assert audit[0][1] == "INSPECTION_LOT"


def test_create_lot_sample_size_default(tmp_path):
    """无抽样规则时样本数为 1。"""
    with _make_client(tmp_path) as client:
        _setup_plan(tmp_path, sample_size=None)
        _exec(tmp_path, "DELETE FROM quality_sampling_rules")
        resp = _create(client, "k1")
        assert resp.status_code == 200
        assert resp.json()["data"]["sampleSize"] == 1


def test_create_lot_defaults(tmp_path):
    """plant/inspectionType 缺省回退，工单/序列号缺省空串。"""
    with _make_client(tmp_path) as client:
        _setup_plan(tmp_path)
        resp = _create(client, "k1", plantCode=None, workOrderNumber=None, serialNumber=None, inspectionType=None)
        assert resp.status_code == 200
        lot = _query(tmp_path, "SELECT plant_code, inspection_type, work_order_number, serial_number FROM quality_inspection_lots")
        assert lot[0][0] == "PLANT-A"
        assert lot[0][1] == "PATROL"
        assert lot[0][2] == ""
        assert lot[0][3] == ""


def test_create_lot_custom_type(tmp_path):
    """自定义检验类型原样写入。"""
    with _make_client(tmp_path) as client:
        _setup_plan(tmp_path)
        resp = _create(client, "k1", inspectionType="CUSTOM")
        assert resp.status_code == 200
        itype = _query(tmp_path, "SELECT inspection_type FROM quality_inspection_lots")
        assert itype[0][0] == "CUSTOM"


def test_create_lot_no_plan_404(tmp_path):
    """无生效计划 → 404。"""
    with _make_client(tmp_path) as client:
        resp = _create(client, "k1")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_create_lot_plan_inactive_404(tmp_path):
    """只有非 EFFECTIVE 计划 → 404。"""
    with _make_client(tmp_path) as client:
        _exec(
            tmp_path,
            "INSERT INTO quality_inspection_plans "
            "(material_code, plan_code, version, inspection_type, status, effective_from) "
            "VALUES ('MAT-A', 'PLAN-A', '1', 'PATROL', 'OBSOLETE', '2026-01-01')",
        )
        resp = _create(client, "k1")
        assert resp.status_code == 404


def test_create_lot_role_check(tmp_path):
    """非合格角色 → 403；角色校验先于幂等。"""
    with _make_client(tmp_path) as client:
        _setup_plan(tmp_path)
        resp = _create(client, "k1", role="MES_OPERATOR")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"
        assert resp.json()["error"]["message"] == "QUALITY_ENGINEER required"

        # MES_ADMIN 允许
        resp = _create(client, "k1", role="MES_ADMIN")
        assert resp.status_code == 200


def test_create_lot_idempotent(tmp_path):
    """幂等重放：cached=true，返回首次 lotId，不重复落库。"""
    with _make_client(tmp_path) as client:
        _setup_plan(tmp_path)
        first = _create(client, "k1")
        replay = _create(client, "k1", materialCode="MAT-OTHER")
        assert replay.status_code == 200
        data = replay.json()["data"]
        assert data["lotId"] == first.json()["data"]["lotId"]
        assert data["cached"] is True
        assert "status" not in data

        lots = _query(tmp_path, "SELECT count(*) FROM quality_inspection_lots")
        assert lots[0][0] == 1


def test_create_lot_validation(tmp_path):
    """缺幂等键 → 422。"""
    with _make_client(tmp_path) as client:
        _setup_plan(tmp_path)
        resp = client.post(
            "/api/v1/quality/inspection-lots",
            json={"materialCode": "MAT-A"},
            headers={"X-Actor-Role": "QUALITY_ENGINEER"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
