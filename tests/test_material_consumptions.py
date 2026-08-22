"""``POST /api/v1/material/consumptions`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/material/consumptions`` 一节。
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
        "('PLANT-A', 'RAW-STORE', 'M-100', 'LOT-A', 'B-1', 10.0, 4.0, 'AVAILABLE', '2026-09-01T00:00:00Z'),"
        "('PLANT-A', 'RAW-STORE', 'M-100', 'LOT-EQ', 'B-2', 10.0, 5.0, 'AVAILABLE', '2026-10-01T00:00:00Z')",
    )


def _post(client: TestClient, key: str, **overrides):
    body = {
        "serialNumber": "U-1",
        "materialCode": "M-100",
        "lotNumber": "LOT-A",
        "workOrderNumber": "WO-1",
        "quantity": 3,
    }
    body.update(overrides)
    return client.post(
        "/api/v1/material/consumptions",
        json=body,
        headers={"X-Idempotency-Key": key, "X-Actor-Id": "OP-1"},
    )


def test_consumption_success(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-1")

    assert resp.status_code == 200
    cid = resp.json()["data"]["consumptionId"]
    assert cid.startswith("MC-")

    cons = _query(
        tmp_path,
        "SELECT serial_number, material_code, lot_number, quantity, unit_code, consumed_by "
        "FROM production_material_consumptions",
    )
    assert len(cons) == 1
    assert cons[0][0] == "U-1"
    assert cons[0][1] == "M-100"
    assert cons[0][2] == "LOT-A"
    assert cons[0][3] == 3.0
    assert cons[0][4] == "EA"
    assert cons[0][5] == "OP-1"

    bal = _query(
        tmp_path,
        "SELECT on_hand_quantity, reserved_quantity FROM material_inventory_balances "
        "WHERE lot_number = 'LOT-A'",
    )
    assert bal[0][0] == 7.0
    # reserved_quantity=4, quantity=3, 4>3 → 预留减 3 → 1
    assert bal[0][1] == 1.0


def test_consumption_reserved_equal_not_reduced(tmp_path):
    """reserved_quantity 与 quantity 相等时预留保持不变。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-2", lotNumber="LOT-EQ", quantity=5)

    assert resp.status_code == 200
    bal = _query(
        tmp_path,
        "SELECT on_hand_quantity, reserved_quantity FROM material_inventory_balances "
        "WHERE lot_number = 'LOT-EQ'",
    )
    assert bal[0][0] == 5.0
    assert bal[0][1] == 5.0  # 相等 → 预留不变


def test_consumption_insufficient_on_hand(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-3", quantity=999)

    assert resp.status_code == 409
    assert resp.json()["error"]["message"] == "insufficient on-hand"
    bal = _query(
        tmp_path,
        "SELECT on_hand_quantity FROM material_inventory_balances WHERE lot_number = 'LOT-A'",
    )
    assert bal[0][0] == 10.0


def test_consumption_no_balance_row(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-4", lotNumber="LOT-NOPE")

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_consumption_default_quantity(tmp_path):
    """quantity 缺失/null/字符串 → 1.0；不校验正数。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-5", quantity="abc")

    assert resp.status_code == 200
    cons = _query(tmp_path, "SELECT quantity FROM production_material_consumptions")
    assert cons[0][0] == 1.0


def test_consumption_negative_quantity_partial_write(tmp_path):
    """负数通过库存比较（-1 < 10），但消耗主记录因 quantity>0 约束不落库，余额反而增加。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-6", quantity=-1)

    assert resp.status_code == 200
    cons = _query(tmp_path, "SELECT consumption_id FROM production_material_consumptions")
    assert len(cons) == 0
    bal = _query(
        tmp_path,
        "SELECT on_hand_quantity FROM material_inventory_balances WHERE lot_number = 'LOT-A'",
    )
    assert bal[0][0] == 11.0


def test_consumption_unknown_work_order_partial_write(tmp_path):
    """未知工单 → 消耗主记录不落库，但余额仍扣减。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _post(client, "k-7", workOrderNumber="WO-NOPE")

    assert resp.status_code == 200
    cons = _query(tmp_path, "SELECT consumption_id FROM production_material_consumptions")
    assert len(cons) == 0
    bal = _query(
        tmp_path,
        "SELECT on_hand_quantity FROM material_inventory_balances WHERE lot_number = 'LOT-A'",
    )
    assert bal[0][0] == 7.0


def test_consumption_idempotency_before_validation(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        first = _post(client, "k-8")
        # 幂等命中在 Body 字段校验之前：第二次缺 serialNumber 仍命中
        second = client.post(
            "/api/v1/material/consumptions",
            json={"materialCode": "M-100", "lotNumber": "LOT-A"},
            headers={"X-Idempotency-Key": "k-8"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["data"]["consumptionId"] == first.json()["data"]["consumptionId"]
    assert second.json()["data"]["cached"] is True


def test_consumption_validation(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        missing = client.post(
            "/api/v1/material/consumptions",
            json={"serialNumber": "U-1"},
            headers={"X-Idempotency-Key": "k-9"},
        )
        bad_json = client.post(
            "/api/v1/material/consumptions",
            content="{bad",
            headers={"X-Idempotency-Key": "k-10"},
        )
        empty_key = client.post(
            "/api/v1/material/consumptions",
            json={"serialNumber": "U-1", "materialCode": "M-100", "lotNumber": "LOT-A"},
        )

    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "VALIDATION_ERROR"
    assert bad_json.status_code == 422
    assert empty_key.status_code == 422
