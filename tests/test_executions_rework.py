"""``POST /api/v1/executions/rework`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/executions/rework`` 一节。
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
        "INSERT INTO production_product_units "
        "(serial_number, work_order_number, status, current_operation_sequence) "
        "VALUES ('SN-1', 'WO-A', 'IN_PROCESS', 10)",
    )


def _rework(client: TestClient, key: str, **overrides):
    body = {
        "serialNumber": "SN-1",
        "targetOperationSequence": 10,
        "reasonCode": "RW-DEFECT",
        "approvedBy": "W-2",
    }
    body.update(overrides)
    return client.post(
        "/api/v1/executions/rework",
        json=body,
        headers={"X-Idempotency-Key": key, "X-Actor-Id": "W-1"},
    )


def test_rework_ok(tmp_path):
    """返工成功：返回 REWORK，记录/单元/追溯/审计落库。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _rework(client, "k1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["serialNumber"] == "SN-1"
        assert data["status"] == "REWORK"
        assert data["targetOperationSequence"] == 10
        assert "cached" not in data

        reworks = _query(tmp_path, "SELECT serial_number, target_operation_sequence, reason_code, approved_by FROM production_rework_orders")
        assert len(reworks) == 1
        assert reworks[0][0] == "SN-1"
        assert reworks[0][1] == 10
        assert reworks[0][2] == "RW-DEFECT"
        assert reworks[0][3] == "W-2"

        unit = _query(tmp_path, "SELECT status, current_operation_sequence FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "REWORK"
        assert unit[0][1] == 10

        traces = _query(tmp_path, "SELECT event_type, resource_type, plant_code FROM trace_events")
        assert len(traces) == 1
        assert traces[0][0] == "REWORK"
        assert traces[0][1] == "SERIAL_NUMBER"
        assert traces[0][2] == "PLANT-A"

        audit = _query(tmp_path, "SELECT action, resource_type FROM audit_events WHERE action='REWORK'")
        assert len(audit) == 1
        assert audit[0][1] == "PRODUCT_UNIT"

        # 与 C++ 一致：错误 SQL 被忽略，不写 REWORK 执行事件
        events = _query(tmp_path, "SELECT count(*) FROM production_execution_events")
        assert events[0][0] == 0


def test_rework_unknown_serial_ok(tmp_path):
    """未知序列号仍成功：不写单元，空工单号，返工记录保留。"""
    with _make_client(tmp_path) as client:
        resp = _rework(client, "k1", serialNumber="SN-NOPE")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "REWORK"

        reworks = _query(tmp_path, "SELECT count(*) FROM production_rework_orders")
        assert reworks[0][0] == 1
        traces = _query(tmp_path, "SELECT related_work_order_number FROM trace_events")
        assert traces[0][0] == ""


def test_rework_sequence_conversion(tmp_path):
    """目标工序转换：小数截断、整数字符串可转换、无效值默认 0。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        for value, expected in [("20", 20), (20.9, 20), ("abc", 0), (None, 0)]:
            resp = _rework(client, f"k-{expected}", targetOperationSequence=value)
            assert resp.status_code == 200
            seq = _query(tmp_path, "SELECT target_operation_sequence FROM production_rework_orders ORDER BY rowid DESC LIMIT 1")
            assert seq[0][0] == expected


def test_rework_idempotent(tmp_path):
    """幂等重放：cached=true，返回首次序列号，不重复落库。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        first = _rework(client, "k1")
        replay = _rework(client, "k1", serialNumber="SN-OTHER")
        assert replay.status_code == 200
        data = replay.json()["data"]
        assert data["serialNumber"] == "SN-1"
        assert data["cached"] is True

        reworks = _query(tmp_path, "SELECT count(*) FROM production_rework_orders")
        assert reworks[0][0] == 1


def test_rework_validation(tmp_path):
    """缺幂等键/序列号 → 422。"""
    with _make_client(tmp_path) as client:
        resp = client.post(
            "/api/v1/executions/rework",
            json={"serialNumber": "SN-1"},
        )
        assert resp.status_code == 422

        resp = _rework(client, "k1", serialNumber="")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
