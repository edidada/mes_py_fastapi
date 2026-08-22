"""``POST /api/v1/integration/inbox`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/integration/inbox`` 一节。
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


def _seed_routing(tmp_path):
    _exec(
        tmp_path,
        "INSERT INTO master_routings (routing_code, version, material_code, description, status) "
        "VALUES ('R-1', '1', 'M-100', 'routing', 'EFFECTIVE')",
    )
    _exec(
        tmp_path,
        "INSERT INTO master_routing_operations "
        "(routing_code, version, sequence, operation_code, work_center_code, quality_gate, "
        " allow_skip, standard_cycle_seconds) VALUES "
        "('R-1', '1', 10, 'OP-10', 'WC-1', 1, 0, 60),"
        "('R-1', '1', 20, 'OP-20', 'WC-2', 0, 1, 30)",
    )


def _post(client: TestClient, **overrides):
    body = {
        "eventId": "EVENT-001",
        "sourceSystem": "ERP",
        "eventType": "erp.plan.pushed",
        "payload": {"materialCode": "M-100", "quantity": 50},
    }
    body.update(overrides)
    return client.post("/api/v1/integration/inbox", json=body)


def test_inbox_plan_pushed_creates_work_order(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_routing(tmp_path)
        resp = _post(client)

    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["messageId"] == "EVENT-001"
    assert data["status"] == "PROCESSED"

    wo = _query(
        tmp_path,
        "SELECT work_order_number, plant_code, material_code, routing_code, routing_version, "
        "priority, status, planned_quantity FROM production_work_orders",
    )
    assert len(wo) == 1
    wo_number = wo[0][0]
    assert wo_number.startswith("WO-") and len(wo_number) == 18  # WO-YY-MM-DD-xxxxxx
    assert wo[0][1] == "PLANT-A"
    assert wo[0][2] == "M-100"
    assert wo[0][3] == "R-1"
    assert wo[0][4] == "1"
    assert wo[0][5] == 0
    assert wo[0][6] == "DRAFT"
    assert wo[0][7] == 50.0

    ops = _query(
        tmp_path,
        "SELECT sequence, operation_code, status FROM production_work_order_operations "
        "WHERE work_order_number = :w ORDER BY sequence",
        w=wo_number,
    )
    assert len(ops) == 2
    assert ops[0] == (10, "OP-10", "PENDING")
    assert ops[1] == (20, "OP-20", "PENDING")

    msg = _query(
        tmp_path,
        "SELECT status, processed_at, error_message FROM integration_inbox_messages "
        "WHERE message_id = 'EVENT-001'",
    )
    assert msg[0][0] == "PROCESSED"
    assert msg[0][1]
    assert msg[0][2] == ""


def test_inbox_plan_pushed_no_routing_failed(tmp_path):
    with _make_client(tmp_path) as client:
        resp = _post(client, eventId="EVENT-002", payload={"materialCode": "M-NOPE"})

    assert resp.status_code == 202
    assert resp.json()["data"]["status"] == "FAILED"
    msg = _query(
        tmp_path,
        "SELECT status, error_message FROM integration_inbox_messages "
        "WHERE message_id = 'EVENT-002'",
    )
    assert msg[0][0] == "FAILED"
    assert msg[0][1] == "no effective routing"


def test_inbox_equipment_status_projection(tmp_path):
    with _make_client(tmp_path) as client:
        _exec(
            tmp_path,
            "INSERT INTO asset_equipment (equipment_code, plant_code, current_status, last_heartbeat_at) "
            "VALUES ('EQP-1', 'PLANT-A', 'RUNNING', '2026-08-21T00:00:00Z')",
        )
        resp = _post(
            client,
            eventId="EVENT-003",
            eventType="equipment.status.changed",
            payload={"equipmentCode": "EQP-1", "status": "DOWN"},
        )

    assert resp.status_code == 202
    assert resp.json()["data"]["status"] == "PROCESSED"
    events = _query(
        tmp_path,
        "SELECT equipment_code, status, occurred_at FROM asset_equipment_events",
    )
    assert len(events) == 1
    assert events[0][0] == "EQP-1"
    assert events[0][1] == "DOWN"
    assert events[0][2]  # occurredAt 缺省当前时间
    eq = _query(
        tmp_path,
        "SELECT current_status, last_heartbeat_at FROM asset_equipment WHERE equipment_code = 'EQP-1'",
    )
    assert eq[0][0] == "DOWN"
    assert eq[0][1] == events[0][2]


def test_inbox_equipment_status_missing_fields_no_projection(tmp_path):
    with _make_client(tmp_path) as client:
        resp = _post(
            client,
            eventId="EVENT-004",
            eventType="equipment.status.changed",
            payload={"equipmentCode": "", "status": "DOWN"},
        )

    assert resp.status_code == 202
    assert resp.json()["data"]["status"] == "PROCESSED"
    events = _query(tmp_path, "SELECT event_id FROM asset_equipment_events")
    assert len(events) == 0


def test_inbox_wms_inventory_projection(tmp_path):
    with _make_client(tmp_path) as client:
        _exec(
            tmp_path,
            "INSERT INTO material_inventory_balances "
            "(plant_code, location_code, material_code, lot_number, batch_number, "
            " on_hand_quantity, reserved_quantity, status, expiry_at) VALUES "
            "('PLANT-A', 'RAW-STORE', 'M-100', 'LOT-A', 'B-1', 5.0, 0.0, 'AVAILABLE', NULL)",
        )
        resp = _post(
            client,
            eventId="EVENT-005",
            eventType="wms.inventory.adjusted",
            payload={"materialCode": "M-100", "lotNumber": "LOT-A", "quantity": 3},
        )

    assert resp.status_code == 202
    assert resp.json()["data"]["status"] == "PROCESSED"
    bal = _query(
        tmp_path,
        "SELECT on_hand_quantity FROM material_inventory_balances WHERE lot_number = 'LOT-A'",
    )
    assert bal[0][0] == 8.0


def test_inbox_wms_inventory_non_number_quantity_zero(tmp_path):
    with _make_client(tmp_path) as client:
        _exec(
            tmp_path,
            "INSERT INTO material_inventory_balances "
            "(plant_code, location_code, material_code, lot_number, batch_number, "
            " on_hand_quantity, reserved_quantity, status, expiry_at) VALUES "
            "('PLANT-A', 'RAW-STORE', 'M-100', 'LOT-B', 'B-2', 5.0, 0.0, 'AVAILABLE', NULL)",
        )
        resp = _post(
            client,
            eventId="EVENT-006",
            eventType="wms.inventory.adjusted",
            payload={"materialCode": "M-100", "lotNumber": "LOT-B", "quantity": "many"},
        )

    assert resp.status_code == 202
    bal = _query(
        tmp_path,
        "SELECT on_hand_quantity FROM material_inventory_balances WHERE lot_number = 'LOT-B'",
    )
    assert bal[0][0] == 5.0


def test_inbox_unknown_event_type_processed(tmp_path):
    with _make_client(tmp_path) as client:
        resp = _post(client, eventId="EVENT-007", eventType="thing.happened", payload={})

    assert resp.status_code == 202
    assert resp.json()["data"] == {"messageId": "EVENT-007", "status": "PROCESSED"}


def test_inbox_duplicate_event_id_cached(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_routing(tmp_path)
        first = _post(client)
        # 重复：不消费投影、不新建工单
        second = _post(client, payload={"materialCode": "M-OTHER"})

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["data"] == {"messageId": "EVENT-001", "status": "PROCESSED", "cached": True}
    wo = _query(tmp_path, "SELECT work_order_number FROM production_work_orders")
    assert len(wo) == 1


def test_inbox_validation(tmp_path):
    with _make_client(tmp_path) as client:
        bad_json = client.post(
            "/api/v1/integration/inbox", content="{bad"
        )
        missing = _post(client, eventId="", eventType=None)
        missing_fields = client.post(
            "/api/v1/integration/inbox", json={"eventId": "E", "sourceSystem": ""}
        )

    assert bad_json.status_code == 422
    assert bad_json.json()["error"]["message"] == "invalid json"
    assert missing.status_code == 422
    assert missing.json()["error"]["message"] == "eventId/sourceSystem/eventType required"
    assert missing_fields.status_code == 422
