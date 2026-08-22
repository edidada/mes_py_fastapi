"""``POST /api/v1/equipment/{equipmentCode}/events`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/equipment/{equipmentCode}/events`` 一节。
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
    """创建设备 EQP-1（PLANT-A）与一条 OEE 小时桶记录。"""
    _exec(
        tmp_path,
        "INSERT INTO asset_equipment "
        "(equipment_code, plant_code, work_center_code, equipment_name, criticality, current_status, last_heartbeat_at) "
        "VALUES ('EQP-1', 'PLANT-A', 'WC-1', 'CNC-01', 'HIGH', 'RUNNING', '2026-08-22T09:00:00Z')",
    )
    _exec(
        tmp_path,
        "INSERT INTO reporting_equipment_oee_hourly "
        "(equipment_code, hour_bucket, actual_output, good_output, running_seconds, planned_seconds, ideal_cycle_seconds) "
        "VALUES ('EQP-1', '2026-08-22T10:00:00', 10.0, 9.0, 3600.0, 3600.0, 1.0)",
    )


def _post(client: TestClient, equipment_code: str, key: str, **overrides):
    body = {
        "status": "RUNNING",
        "reasonCode": "shift-start",
        "occurredAt": "2026-08-22T10:00:00Z",
        "sourceEventId": None,
        "counterCode": None,
        "counterValue": None,
    }
    body.update(overrides)
    return client.post(
        f"/api/v1/equipment/{equipment_code}/events",
        json=body,
        headers={"X-Idempotency-Key": key},
    )


def test_equipment_not_found(tmp_path):
    with _make_client(tmp_path) as client:
        resp = _post(client, "EQP-NOPE", "k-404")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_equipment_event_accepted(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "EQP-1", "k-ok")

    assert resp.status_code == 202
    assert resp.json()["data"] == {"accepted": True}

    events = _query(tmp_path, "SELECT event_id, status, reason_code, occurred_at, source_event_id FROM asset_equipment_events")
    assert len(events) == 1
    assert events[0][0].startswith("EEQ-")
    assert events[0][1] == "RUNNING"
    assert events[0][2] == "shift-start"
    assert events[0][3] == "2026-08-22T10:00:00Z"
    assert events[0][4] == "k-ok"  # sourceEventId 缺省 → 幂等键

    eq = _query(tmp_path, "SELECT current_status, last_heartbeat_at FROM asset_equipment WHERE equipment_code = 'EQP-1'")
    assert eq[0][0] == "RUNNING"
    assert eq[0][1]


def test_equipment_event_defaults(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(
            client,
            "EQP-1",
            "k-defaults",
            reasonCode=None,
            occurredAt=None,
            sourceEventId="",
        )

    assert resp.status_code == 202
    events = _query(
        tmp_path,
        "SELECT event_id, reason_code, occurred_at, source_event_id FROM asset_equipment_events",
    )
    assert events[0][1] == ""  # reasonCode 缺省空字符串
    assert events[0][2]  # occurredAt 缺省 → 当前 UTC
    assert events[0][3] == "k-defaults"  # sourceEventId 空 → 幂等键


def test_equipment_event_counter(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "EQP-1", "k-counter", counterCode="CNT-1", counterValue=5)

    assert resp.status_code == 202
    counters = _query(tmp_path, "SELECT counter_code, value FROM asset_equipment_counters")
    assert counters == [("CNT-1", 5.0)]
    oee = _query(
        tmp_path,
        "SELECT actual_output FROM reporting_equipment_oee_hourly "
        "WHERE equipment_code = 'EQP-1' AND hour_bucket = '2026-08-22T10:00:00'",
    )
    assert oee[0][0] == 15.0


def test_equipment_event_counter_default_code_out(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        # 只传 counterValue → counterCode 缺省 OUT；非数字按 0
        resp = _post(client, "EQP-1", "k-out", counterCode=None, counterValue="abc")

    assert resp.status_code == 202
    counters = _query(tmp_path, "SELECT counter_code, value FROM asset_equipment_counters")
    assert counters == [("OUT", 0.0)]


def test_equipment_event_fault(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "EQP-1", "k-fault", status="FAULT")

    assert resp.status_code == 202
    mwos = _query(
        tmp_path,
        "SELECT maintenance_work_order_id, maintenance_type, status FROM asset_maintenance_work_orders",
    )
    assert len(mwos) == 1
    assert mwos[0][0].startswith("MWO-")
    assert mwos[0][1] == "CORRECTIVE"
    assert mwos[0][2] == "OPEN"
    andons = _query(
        tmp_path,
        "SELECT severity, category, resource_type, resource_code, message FROM trace_andon_events",
    )
    assert andons[0][0] == "CRITICAL"
    assert andons[0][1] == "EQUIPMENT"
    assert andons[0][2] == "EQUIPMENT"
    assert andons[0][3] == "EQP-1"
    assert andons[0][4] == "equipment fault"


def test_equipment_event_unknown_status_conflict(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "EQP-1", "k-bad-status", status="NONEXISTENT")

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"
    assert resp.json()["error"]["message"] == "unknown status value"
    events = _query(tmp_path, "SELECT event_id FROM asset_equipment_events")
    assert len(events) == 0


def test_equipment_event_duplicate_source_event_id(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        first = _post(client, "EQP-1", "k-src-1", sourceEventId="SRC-X")
        second = _post(client, "EQP-1", "k-src-2", sourceEventId="SRC-X")

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["message"] == "duplicate sourceEventId"
    events = _query(tmp_path, "SELECT event_id FROM asset_equipment_events")
    assert len(events) == 1


def test_equipment_event_idempotency_before_validation(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        first = _post(client, "EQP-1", "k-idem")
        # 幂等命中发生在 JSON 解析之前：第二次传无效 JSON 仍命中
        second = client.post(
            "/api/v1/equipment/EQP-1/events",
            content="{not json",
            headers={"X-Idempotency-Key": "k-idem"},
        )
        # 且设备不存在也命中（幂等命中在路径设备校验之前）
        third = client.post(
            "/api/v1/equipment/EQP-NOPE/events",
            content="{}",
            headers={"X-Idempotency-Key": "k-idem"},
        )

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["data"]["cached"] is True
    assert second.json()["data"]["eventId"].startswith("EVT-")
    assert third.status_code == 200
    assert third.json()["data"]["cached"] is True


def test_equipment_event_validation(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        missing_status = client.post(
            "/api/v1/equipment/EQP-1/events",
            json={},
            headers={"X-Idempotency-Key": "k-v1"},
        )
        invalid_json = client.post(
            "/api/v1/equipment/EQP-1/events",
            content="{bad",
            headers={"X-Idempotency-Key": "k-v2"},
        )
        empty_key = client.post(
            "/api/v1/equipment/EQP-1/events",
            json={"status": "RUNNING"},
            headers={"X-Idempotency-Key": ""},
        )

    assert missing_status.status_code == 422
    assert missing_status.json()["error"]["code"] == "VALIDATION_ERROR"
    assert invalid_json.status_code == 422
    assert empty_key.status_code == 422
