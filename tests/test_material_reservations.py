"""``POST /api/v1/material/reservations`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/material/reservations`` 一节。
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
        "INSERT INTO production_work_orders (work_order_number, plant_code, status) "
        "VALUES ('WO-1', 'PLANT-A', 'RELEASED')",
    )
    _exec(
        tmp_path,
        "INSERT INTO material_inventory_balances "
        "(plant_code, location_code, material_code, lot_number, batch_number, "
        " on_hand_quantity, reserved_quantity, status, expiry_at) VALUES "
        "('PLANT-A', 'RAW-STORE', 'M-100', 'LOT-A', 'B-1', 5.0, 1.0, 'AVAILABLE', '2026-09-01T00:00:00Z'),"
        "('PLANT-A', 'RAW-STORE', 'M-100', 'LOT-B', 'B-2', 3.0, 0.0, 'AVAILABLE', '2026-10-01T00:00:00Z'),"
        "('PLANT-A', 'RAW-STORE', 'M-100', 'LOT-FULL', 'B-3', 2.0, 2.0, 'AVAILABLE', '2026-08-01T00:00:00Z')",
    )


def _post(client: TestClient, key: str, role: str = "PLANNER", **overrides):
    body = {
        "materialCode": "M-100",
        "reservedQuantity": 5,
        "workOrderNumber": "WO-1",
        "operationSequence": 10,
        "expiresAt": "2026-12-31T00:00:00Z",
    }
    body.update(overrides)
    return client.post(
        "/api/v1/material/reservations",
        json=body,
        headers={"X-Idempotency-Key": key, "X-Actor-Role": role, "X-Actor-Id": "P-1"},
    )


def test_reservation_fifo_allocation(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-1")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "ACTIVE"
    # FIFO：LOT-A 先(可用4) 再 LOT-B(可用3)；LOT-FULL 无可用被跳过
    assert data["allocatedLots"] == [
        {"lotNumber": "LOT-A", "quantity": 4.0, "reservationId": data["allocatedLots"][0]["reservationId"]},
        {"lotNumber": "LOT-B", "quantity": 1.0, "reservationId": data["allocatedLots"][1]["reservationId"]},
    ]
    assert data["allocatedLots"][0]["reservationId"].startswith("RSV-")

    rows = _query(tmp_path, "SELECT lot_number, quantity, status FROM material_inventory_reservations")
    assert len(rows) == 2
    balances = _query(
        tmp_path,
        "SELECT lot_number, reserved_quantity FROM material_inventory_balances ORDER BY lot_number",
    )
    assert balances == [("LOT-A", 5.0), ("LOT-B", 1.0), ("LOT-FULL", 2.0)]


def test_reservation_zero_quantity(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-2", reservedQuantity=0)

    assert resp.status_code == 200
    assert resp.json()["data"] == {"status": "ACTIVE", "allocatedLots": []}
    rows = _query(tmp_path, "SELECT reservation_id FROM material_inventory_reservations")
    assert len(rows) == 0


def test_reservation_insufficient_rollback(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-3", reservedQuantity=99)

    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "CONFLICT"
    assert err["message"].startswith("insufficient inventory, short ")
    assert len(err["message"].split(".")[1]) == 6  # N.NNNNNN
    rows = _query(tmp_path, "SELECT reservation_id FROM material_inventory_reservations")
    assert len(rows) == 0
    balances = _query(
        tmp_path,
        "SELECT lot_number, reserved_quantity FROM material_inventory_balances ORDER BY lot_number",
    )
    assert balances == [("LOT-A", 1.0), ("LOT-B", 0.0), ("LOT-FULL", 2.0)]


def test_reservation_unknown_work_order_still_updates_balance(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-4", workOrderNumber="WO-NOPE")

    assert resp.status_code == 200
    assert resp.json()["data"]["allocatedLots"][0]["reservationId"].startswith("RSV-")
    # 预留主记录因未知工单外键失败，但余额仍更新
    rows = _query(tmp_path, "SELECT reservation_id FROM material_inventory_reservations")
    assert len(rows) == 0
    balances = _query(
        tmp_path,
        "SELECT lot_number, reserved_quantity FROM material_inventory_balances ORDER BY lot_number",
    )
    assert balances == [("LOT-A", 5.0), ("LOT-B", 1.0), ("LOT-FULL", 2.0)]


def test_reservation_operation_sequence_string(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-5", operationSequence="20")

    assert resp.status_code == 200
    rows = _query(tmp_path, "SELECT operation_sequence FROM material_inventory_reservations LIMIT 1")
    assert rows[0][0] == 20


def test_reservation_idempotency(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        first = _post(client, "k-6")
        second = _post(client, "k-6")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["data"]["reservationId"] == first.json()["data"]["allocatedLots"][0]["reservationId"]
    assert second.json()["data"]["cached"] is True
    assert "allocatedLots" not in second.json()["data"]


def test_reservation_forbidden_role(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-7", role="MES_OPERATOR")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_reservation_validation(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        bad_json = client.post(
            "/api/v1/material/reservations",
            content="{bad",
            headers={"X-Idempotency-Key": "k-8", "X-Actor-Role": "PLANNER"},
        )
        empty_key = client.post(
            "/api/v1/material/reservations",
            json={},
            headers={"X-Actor-Role": "PLANNER"},
        )

    assert bad_json.status_code == 422
    assert empty_key.status_code == 422
    assert empty_key.json()["error"]["code"] == "VALIDATION_ERROR"
