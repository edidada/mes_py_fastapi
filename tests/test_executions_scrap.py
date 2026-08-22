"""``POST /api/v1/executions/scrap`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/executions/scrap`` 一节。
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
    _exec(
        tmp_path,
        "INSERT INTO production_work_orders "
        "(work_order_number, material_code, plant_code, planned_quantity, status, rejected_quantity) "
        "VALUES ('WO-A', 'MAT-A', 'PLANT-A', 100, 'RELEASED', 0)",
    )


def _scrap(client: TestClient, key: str, **overrides):
    body = {
        "serialNumber": "SN-1",
        "scrapCode": "SC-CRACK",
        "disposition": "DISPOSE",
        "workOrderNumber": "WO-A",
    }
    body.update(overrides)
    return client.post(
        "/api/v1/executions/scrap",
        json=body,
        headers={"X-Idempotency-Key": key, "X-Actor-Id": "W-1"},
    )


def test_scrap_ok_with_work_order(tmp_path):
    """报废成功（含工单）：记录/单元/拒收数/outbox/追溯/审计落库，无 SCRAP event。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _scrap(client, "k1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["serialNumber"] == "SN-1"
        assert data["status"] == "SCRAPPED"
        assert "cached" not in data

        scraps = _query(tmp_path, "SELECT serial_number, scrap_code, disposition, work_order_number, operator_id FROM production_scrap_records")
        assert len(scraps) == 1
        assert scraps[0][0] == "SN-1"
        assert scraps[0][1] == "SC-CRACK"
        assert scraps[0][2] == "DISPOSE"
        assert scraps[0][3] == "WO-A"
        assert scraps[0][4] == "W-1"

        unit = _query(tmp_path, "SELECT status, completed_at FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "SCRAPPED"
        assert unit[0][1] is not None

        wo = _query(tmp_path, "SELECT rejected_quantity FROM production_work_orders WHERE work_order_number='WO-A'")
        assert wo[0][0] == 1

        outbox = _query(tmp_path, "SELECT event_type, aggregate_id, target_system FROM integration_outbox_messages")
        assert len(outbox) == 1
        assert outbox[0][0] == "work_order.scrap"
        assert outbox[0][1] == "WO-A"
        assert outbox[0][2] == "ERP"

        traces = _query(tmp_path, "SELECT event_type, resource_type, related_work_order_number FROM trace_events")
        assert traces[0][0] == "SCRAP"
        assert traces[0][1] == "SERIAL_NUMBER"
        assert traces[0][2] == "WO-A"

        audit = _query(tmp_path, "SELECT action, resource_type FROM audit_events WHERE action='SCRAP'")
        assert len(audit) == 1
        assert audit[0][1] == "PRODUCT_UNIT"

        # 与 C++ 一致：错误 SQL 被忽略，不写 SCRAP 执行事件
        events = _query(tmp_path, "SELECT count(*) FROM production_execution_events")
        assert events[0][0] == 0


def test_scrap_without_work_order(tmp_path):
    """无工单：不写拒收数与 outbox，追溯工单号为空。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _scrap(client, "k1", workOrderNumber=None)
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "SCRAPPED"

        wo = _query(tmp_path, "SELECT rejected_quantity FROM production_work_orders WHERE work_order_number='WO-A'")
        assert wo[0][0] == 0

        outbox = _query(tmp_path, "SELECT count(*) FROM integration_outbox_messages")
        assert outbox[0][0] == 0

        traces = _query(tmp_path, "SELECT related_work_order_number FROM trace_events")
        assert traces[0][0] == ""


def test_scrap_disposition_default(tmp_path):
    """处置方式：空/省略/非字符串回退 DISPOSE，其他原样保存。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _scrap(client, "k1", disposition="")
        assert resp.status_code == 200
        disp = _query(tmp_path, "SELECT disposition FROM production_scrap_records")
        assert disp[0][0] == "DISPOSE"

        resp = _scrap(client, "k2", disposition=None)
        assert resp.status_code == 200
        disp = _query(tmp_path, "SELECT disposition FROM production_scrap_records ORDER BY rowid DESC LIMIT 1")
        assert disp[0][0] == "DISPOSE"

        resp = _scrap(client, "k3", disposition="RECYCLE")
        assert resp.status_code == 200
        disp = _query(tmp_path, "SELECT disposition FROM production_scrap_records ORDER BY rowid DESC LIMIT 1")
        assert disp[0][0] == "RECYCLE"


def test_scrap_unknown_serial_ok(tmp_path):
    """未知序列号仍成功，报废记录保留。"""
    with _make_client(tmp_path) as client:
        resp = _scrap(client, "k1", serialNumber="SN-NOPE")
        assert resp.status_code == 200
        scraps = _query(tmp_path, "SELECT count(*) FROM production_scrap_records")
        assert scraps[0][0] == 1


def test_scrap_idempotent(tmp_path):
    """幂等重放：cached=true，不重复落库。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        first = _scrap(client, "k1")
        replay = _scrap(client, "k1", serialNumber="SN-OTHER")
        assert replay.status_code == 200
        data = replay.json()["data"]
        assert data["serialNumber"] == "SN-1"
        assert data["cached"] is True

        scraps = _query(tmp_path, "SELECT count(*) FROM production_scrap_records")
        assert scraps[0][0] == 1


def test_scrap_validation(tmp_path):
    """缺幂等键/序列号 → 422。"""
    with _make_client(tmp_path) as client:
        resp = client.post(
            "/api/v1/executions/scrap",
            json={"serialNumber": "SN-1"},
        )
        assert resp.status_code == 422

        resp = _scrap(client, "k1", serialNumber="")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
