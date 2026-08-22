"""``POST /api/v1/trace/impact-query`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/trace/impact-query`` 一节。
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
        "INSERT INTO production_work_orders (work_order_number, plant_code, status) VALUES "
        "('WO-1', 'PLANT-A', 'RELEASED'),"
        "('WO-2', 'PLANT-A', 'RELEASED'),"
        "('WO-3', 'PLANT-A', 'RELEASED'),"
        "('WO-4', 'PLANT-A', 'RELEASED')",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_product_units (serial_number, work_order_number, status) VALUES "
        "('U-PASS', 'WO-1', 'PASSED'),"
        "('U-SCRAP', 'WO-2', 'SCRAPPED'),"
        "('U-PROC', 'WO-3', 'IN_PROCESS'),"
        "('U-FAIL', 'WO-4', 'FAILED')",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_material_consumptions "
        "(consumption_id, serial_number, work_order_number, material_code, lot_number, quantity, consumed_at) VALUES "
        "('C-1', 'U-PASS', 'WO-1', 'M-100', 'LOT-X', 1.0, '2026-08-22T01:00:00Z'),"
        "('C-2', 'U-PASS', 'WO-1', 'M-100', 'LOT-X', 1.0, '2026-08-22T01:01:00Z'),"
        "('C-3', 'U-SCRAP', 'WO-2', 'M-100', 'LOT-X', 1.0, '2026-08-22T01:02:00Z'),"
        "('C-4', 'U-PROC', 'WO-3', 'M-100', 'LOT-X', 1.0, '2026-08-22T01:03:00Z'),"
        "('C-5', 'U-FAIL', 'WO-4', 'M-100', 'LOT-X', 1.0, '2026-08-22T01:04:00Z'),"
        "('C-6', 'U-NO-WORK', 'WO-1', 'M-100', 'LOT-X', 1.0, '2026-08-22T01:05:00Z')",
    )


def _post(client: TestClient, key: str, **overrides):
    body = {"lotNumber": "LOT-X"}
    body.update(overrides)
    return client.post(
        "/api/v1/trace/impact-query",
        json=body,
        headers={"X-Idempotency-Key": key, "X-Actor-Id": "Q-1"},
    )


def test_impact_query_statistics(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-1")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["query"] == {"lotNumber": "LOT-X"}
    summary = data["impactSummary"]
    # 6 条消耗记录（U-PASS 重复计两次）；PASSED×2、SCRAPPED×1、其他 3（含 UNKNOWN）
    assert summary["affectedUnitCount"] == 6
    assert summary["completedCount"] == 2
    assert summary["scrappedCount"] == 1
    assert summary["inProcessCount"] == 3
    assert summary["riskLevel"] == "INFO"

    units = data["affectedUnits"]
    assert len(units) == 6
    by_serial = {u["serialNumber"]: u for u in units}
    assert by_serial["U-SCRAP"]["unitStatus"] == "SCRAPPED"
    assert by_serial["U-NO-WORK"]["unitStatus"] == "UNKNOWN"
    assert by_serial["U-NO-WORK"]["workOrderNumber"] == ""


def test_impact_query_empty_result(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-2", lotNumber="LOT-EMPTY")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["impactSummary"] == {
        "affectedUnitCount": 0,
        "inProcessCount": 0,
        "completedCount": 0,
        "scrappedCount": 0,
        "riskLevel": "INFO",
    }
    assert data["affectedUnits"] == []


def test_impact_query_idempotency(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        first = _post(client, "k-3")
        second = _post(client, "k-3")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["data"] == {"queryId": "LOT-X", "cached": True}


def test_impact_query_validation(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        missing = _post(client, "k-4", lotNumber=None)
        bad_json = client.post(
            "/api/v1/trace/impact-query",
            content="{bad",
            headers={"X-Idempotency-Key": "k-5"},
        )
        empty_key = client.post(
            "/api/v1/trace/impact-query",
            json={"lotNumber": "LOT-X"},
        )

    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "VALIDATION_ERROR"
    assert bad_json.status_code == 422
    assert empty_key.status_code == 422


def test_impact_query_risk_warning(tmp_path):
    """大于 10 条 → WARNING。"""
    with _make_client(tmp_path) as client:
        _exec(
            tmp_path,
            "INSERT INTO production_work_orders (work_order_number, plant_code, status) "
            "VALUES ('WO-1', 'PLANT-A', 'RELEASED')",
        )
        _exec(
            tmp_path,
            "INSERT INTO production_product_units (serial_number, work_order_number, status) "
            "VALUES ('U-1', 'WO-1', 'PASSED')",
        )
        cons = ", ".join(
            f"('C-B{i}', 'U-1', 'WO-1', 'M-100', 'LOT-Y', 1.0, '2026-08-22T01:00:00Z')"
            for i in range(12)
        )
        _exec(
            tmp_path,
            "INSERT INTO production_material_consumptions "
            "(consumption_id, serial_number, work_order_number, material_code, lot_number, quantity, consumed_at) "
            "VALUES " + cons,
        )
        resp = _post(client, "k-6", lotNumber="LOT-Y")

    assert resp.status_code == 200
    assert resp.json()["data"]["impactSummary"]["affectedUnitCount"] == 12
    assert resp.json()["data"]["impactSummary"]["riskLevel"] == "WARNING"
