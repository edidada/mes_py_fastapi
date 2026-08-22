"""``GET /api/v1/quality/spc/charts`` 接口契约测试。

契约依据：docs/http-api.md 中 ``GET /api/v1/quality/spc/charts`` 一节。
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
        "INSERT INTO quality_spc_measurements "
        "(measurement_id, material_code, characteristic_code, value, cl, ucl, lcl, usl, lsl, "
        " violation_rule, measured_at) VALUES "
        "('SPC-001', 'M-1', 'DIM-L', 10.01, 10.0, 10.04, 9.96, 10.05, 9.95, NULL, '2026-08-21T00:00:00Z'),"
        "('SPC-002', 'M-1', 'DIM-L', 10.06, 10.0, 10.04, 9.96, 10.05, 9.95, 'RULE_1', '2026-08-22T00:00:00Z'),"
        "('SPC-003', 'M-2', 'DIM-W', 5.0, NULL, NULL, NULL, NULL, NULL, NULL, '2026-08-20T00:00:00Z'),"
        "('SPC-004', 'M-3', '', 1.0, NULL, NULL, NULL, NULL, NULL, NULL, '2026-08-19T00:00:00Z')",
    )


def test_spc_empty(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.get("/api/v1/quality/spc/charts")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["data"] == []
    assert body["data"]["meta"]["page"] == 1
    assert body["data"]["meta"]["pageSize"] == 100
    assert body["data"]["meta"]["total"] == 0
    assert body["meta"]["correlationId"] == body["data"]["meta"]["correlationId"]


def test_spc_list_all_fields(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.get("/api/v1/quality/spc/charts")

    assert resp.status_code == 200
    items = resp.json()["data"]["data"]
    # 按 measured_at DESC：SPC-002、SPC-001、SPC-003、SPC-004
    assert [i["measurementId"] for i in items] == ["SPC-002", "SPC-001", "SPC-003", "SPC-004"]
    assert resp.json()["data"]["meta"]["total"] == 4

    first = items[0]
    assert first["characteristicCode"] == "DIM-L"
    assert first["value"] == 10.06
    assert first["cl"] == 10.0
    assert first["ucl"] == 10.04
    assert first["lcl"] == 9.96
    assert first["usl"] == 10.05
    assert first["lsl"] == 9.95
    assert first["violationRule"] == "RULE_1"
    assert first["inControl"] is False
    assert first["measuredAt"] == "2026-08-22T00:00:00Z"

    second = items[1]
    assert second["violationRule"] == ""
    assert second["inControl"] is True


def test_spc_filter_by_characteristic_code(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.get("/api/v1/quality/spc/charts", params={"characteristicCode": "DIM-L"})

    assert resp.status_code == 200
    items = resp.json()["data"]["data"]
    assert [i["measurementId"] for i in items] == ["SPC-002", "SPC-001"]
    assert resp.json()["data"]["meta"]["total"] == 2


def test_spc_explicit_empty_string_filters_empty_code(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.get("/api/v1/quality/spc/charts", params={"characteristicCode": ""})

    assert resp.status_code == 200
    items = resp.json()["data"]["data"]
    assert [i["measurementId"] for i in items] == ["SPC-004"]
    assert resp.json()["data"]["meta"]["total"] == 1


def test_spc_correlation_id_propagates(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.get("/api/v1/quality/spc/charts", headers={"X-Correlation-Id": "spc-list"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["correlationId"] == "spc-list"
    assert body["data"]["meta"]["correlationId"] == "spc-list"
