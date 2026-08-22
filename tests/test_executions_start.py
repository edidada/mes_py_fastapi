"""``POST /api/v1/executions/start`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/executions/start`` 一节。
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


def _setup(tmp_path, wo_status="RELEASED"):
    _exec(
        tmp_path,
        "INSERT INTO production_work_orders (work_order_number, material_code, planned_quantity, status) "
        "VALUES ('WO-A', 'MAT-A', 100, :s)",
        s=wo_status,
    )
    _exec(
        tmp_path,
        "INSERT INTO production_work_order_operations "
        "(work_order_number, sequence, operation_code, work_center_code) "
        "VALUES ('WO-A', 10, 'OP-10', 'WC-1')",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_operation_tasks "
        "(task_id, work_order_number, operation_sequence, station_code, worker_id, status) "
        "VALUES ('T-1', 'WO-A', 10, 'ST-1', 'W-1', 'PENDING')",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_product_units (serial_number, work_order_number, status) "
        "VALUES ('SN-1', 'WO-A', 'CREATED')",
    )


def _start(client: TestClient, key: str, **overrides):
    body = {
        "serialNumber": "SN-1",
        "workOrderNumber": "WO-A",
        "operationSequence": 10,
        "operationCode": "OP-10",
        "stationCode": "ST-1",
        "equipmentCode": "EQ-1",
    }
    body.update(overrides)
    return client.post(
        "/api/v1/executions/start",
        json=body,
        headers={"X-Idempotency-Key": key, "X-Actor-Id": "W-1"},
    )


def test_start_ok(tmp_path):
    """执行开始：返回 IN_PROCESS，事件/状态/审计全部落库。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _start(client, "k1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["serialNumber"] == "SN-1"
        assert data["status"] == "IN_PROCESS"
        assert "cached" not in data

        events = _query(tmp_path, "SELECT event_type, plant_code, operator_id FROM production_execution_events")
        assert len(events) == 1
        assert events[0][0] == "START"
        assert events[0][1] == "PLANT-A"
        assert events[0][2] == "W-1"

        unit = _query(tmp_path, "SELECT status, current_station_code FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "IN_PROCESS"
        assert unit[0][1] == "ST-1"

        wo = _query(tmp_path, "SELECT status FROM production_work_orders WHERE work_order_number='WO-A'")
        assert wo[0][0] == "IN_PROGRESS"

        ops = _query(tmp_path, "SELECT status FROM production_work_order_operations WHERE work_order_number='WO-A' AND sequence=10")
        assert ops[0][0] == "STARTED"

        tasks = _query(tmp_path, "SELECT status FROM production_operation_tasks WHERE task_id='T-1'")
        assert tasks[0][0] == "STARTED"

        labor = _query(tmp_path, "SELECT status, worker_id FROM production_labor_records")
        assert labor[0][0] == "OPEN"
        assert labor[0][1] == "W-1"

        traces = _query(tmp_path, "SELECT event_type, resource_type, resource_code FROM trace_events")
        assert len(traces) == 1
        assert traces[0][0] == "EXECUTION_START"
        assert traces[0][1] == "EQUIPMENT"
        assert traces[0][2] == "EQ-1"

        audit = _query(tmp_path, "SELECT action, resource_type, resource_id FROM audit_events WHERE action='EXECUTION_START'")
        assert len(audit) == 1
        assert audit[0][1] == "PRODUCT_UNIT"
        assert audit[0][2] == "SN-1"


def test_start_sequence_conversion(tmp_path):
    """operationSequence 转换：小数截断、整数字符串可转换、无效值默认 0。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        for value, expected in [("10", 10), (10.9, 10), ("abc", 0), (None, 0)]:
            resp = _start(client, f"k-{expected}", operationSequence=value)
            assert resp.status_code == 200
            seq = _query(tmp_path, "SELECT operation_sequence FROM production_execution_events ORDER BY rowid DESC LIMIT 1")
            assert seq[0][0] == expected


def test_start_operation_code_default(tmp_path):
    """operationCode 省略/空串 → 生成 OP-{sequence}。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _start(client, "k1", operationCode=None)
        assert resp.status_code == 200
        op = _query(tmp_path, "SELECT operation_code FROM production_execution_events")
        assert op[0][0] == "OP-10"

        resp = _start(client, "k2", operationSequence="5", operationCode="")
        assert resp.status_code == 200
        op = _query(tmp_path, "SELECT operation_code FROM production_execution_events ORDER BY rowid DESC LIMIT 1")
        assert op[0][0] == "OP-5"


def test_start_work_order_not_released(tmp_path):
    """工单非 RELEASED 时仍返回成功，工单状态不被修改。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path, wo_status="IN_PROGRESS")
        resp = _start(client, "k1")
        assert resp.status_code == 200
        wo = _query(tmp_path, "SELECT status FROM production_work_orders WHERE work_order_number='WO-A'")
        assert wo[0][0] == "IN_PROGRESS"


def test_start_idempotent(tmp_path):
    """幂等重放：cached=true，返回首次序列号，不重复落库。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        first = _start(client, "k1")
        replay = _start(client, "k1", stationCode="ST-OTHER")
        assert replay.status_code == 200
        data = replay.json()["data"]
        assert data["serialNumber"] == "SN-1"
        assert data["cached"] is True
        assert "status" not in data

        events = _query(tmp_path, "SELECT count(*) FROM production_execution_events")
        assert events[0][0] == 1


def test_start_validation(tmp_path):
    """缺幂等键/serialNumber/workOrderNumber → 422。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.post(
            "/api/v1/executions/start",
            json={"serialNumber": "SN-1", "workOrderNumber": "WO-A"},
        )
        assert resp.status_code == 422

        resp = _start(client, "k1", serialNumber="")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

        resp = _start(client, "k2", workOrderNumber=None)
        assert resp.status_code == 422


def test_start_unknown_serial_404(tmp_path):
    """序列号不存在 → 404 NOT_FOUND。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _start(client, "k1", serialNumber="SN-NOPE")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"
