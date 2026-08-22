"""``GET /api/v1/trace/units/{serialNumber}`` 接口契约测试。

契约依据：docs/http-api.md 中 ``GET /api/v1/trace/units/{serialNumber}`` 一节。
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
        "INSERT INTO production_work_orders (work_order_number, plant_code, material_code, routing_code, status) "
        "VALUES ('WO-1', 'PLANT-A', 'M-FIN', 'R-1', 'RELEASED')",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_product_units (serial_number, work_order_number, status) "
        "VALUES ('U-1', 'WO-1', 'IN_PROCESS')",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_execution_events "
        "(event_id, serial_number, operation_sequence, operation_code, station_code, equipment_code, "
        " operator_id, event_type, occurred_at) VALUES "
        "('EVT-1', 'U-1', 10, 'OP-10', 'ST-1', 'EQP-1', 'OP-1', 'START', '2026-08-20T01:00:00Z'),"
        "('EVT-2', 'U-1', 20, 'OP-20', NULL, NULL, 'OP-2', 'COMPLETE', '2026-08-21T01:00:00Z')",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_parameter_records "
        "(record_id, serial_number, operation_sequence, code, value, lower_limit, upper_limit, in_spec, recorded_at) VALUES "
        "('PAR-1', 'U-1', 10, 'DIM', 10.02, 9.9, 10.1, 1, '2026-08-20T01:05:00Z'),"
        "('PAR-2', 'U-1', 10, 'TEMP', 30.0, NULL, NULL, 0, '2026-08-20T01:06:00Z')",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_material_consumptions "
        "(consumption_id, serial_number, work_order_number, material_code, lot_number, quantity, consumed_at) "
        "VALUES ('MC-1', 'U-1', 'WO-1', 'M-100', 'LOT-A', 2.0, '2026-08-20T01:00:00Z')",
    )
    _exec(
        tmp_path,
        "INSERT INTO quality_inspection_results "
        "(inspection_id, serial_number, plan_code, operation_sequence, disposition, defect_code, inspector_id, inspected_at) "
        "VALUES ('RES-1', 'U-1', 'PLAN-A', 10, 'ACCEPTED', NULL, 'Q-1', '2026-08-20T02:00:00Z')",
    )


def test_trace_unit_full_view(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.get("/api/v1/trace/units/U-1", headers={"X-Correlation-Id": "trace-1"})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert resp.json()["meta"]["correlationId"] == "trace-1"

    product = data["product"]
    assert product["serialNumber"] == "U-1"
    assert product["workOrderNumber"] == "WO-1"
    assert product["materialCode"] == "M-FIN"
    assert product["routingVersion"] == "R-1"
    assert product["status"] == "IN_PROCESS"

    ops = data["operations"]
    assert len(ops) == 2
    assert ops[0]["sequence"] == 10
    assert ops[0]["operationCode"] == "OP-10"
    assert ops[0]["stationCode"] == "ST-1"
    assert ops[0]["equipmentCode"] == "EQP-1"
    assert ops[0]["operatorId"] == "OP-1"
    assert ops[0]["eventType"] == "START"
    assert ops[0]["occurredAt"] == "2026-08-20T01:00:00Z"
    # NULL 列 → 空字符串
    assert ops[1]["stationCode"] == ""
    assert ops[1]["equipmentCode"] == ""

    params = ops[0]["parameters"]
    assert len(params) == 2
    assert params[0]["code"] == "DIM"
    assert params[0]["value"] == 10.02
    assert params[0]["lowerLimit"] == 9.9
    assert params[0]["upperLimit"] == 10.1
    assert params[0]["inSpec"] is True
    assert params[0]["recordedAt"] == "2026-08-20T01:05:00Z"
    assert params[1]["inSpec"] is False

    cons = data["materialConsumptions"]
    assert len(cons) == 1
    assert cons[0] == {
        "materialCode": "M-100",
        "lotNumber": "LOT-A",
        "quantity": 2.0,
        "consumedAt": "2026-08-20T01:00:00Z",
    }

    insp = data["inspections"]
    assert len(insp) == 1
    assert insp[0]["planCode"] == "PLAN-A"
    assert insp[0]["operationSequence"] == 10
    assert insp[0]["disposition"] == "ACCEPTED"
    assert insp[0]["defectCode"] == ""
    assert insp[0]["inspectorId"] == "Q-1"
    assert insp[0]["inspectedAt"] == "2026-08-20T02:00:00Z"


def test_trace_unit_empty_children(tmp_path):
    with _make_client(tmp_path) as client:
        _exec(
            tmp_path,
            "INSERT INTO production_work_orders (work_order_number, plant_code, material_code, routing_code, status) "
            "VALUES ('WO-2', 'PLANT-A', 'M-FIN', NULL, 'RELEASED')",
        )
        _exec(
            tmp_path,
            "INSERT INTO production_product_units (serial_number, work_order_number, status) "
            "VALUES ('U-2', 'WO-2', 'PASSED')",
        )
        resp = client.get("/api/v1/trace/units/U-2")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["product"]["routingVersion"] == ""
    assert data["operations"] == []
    assert data["materialConsumptions"] == []
    assert data["inspections"] == []


def test_trace_unit_not_found(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.get("/api/v1/trace/units/U-NOPE")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert resp.json()["error"]["message"] == "serial not found"
