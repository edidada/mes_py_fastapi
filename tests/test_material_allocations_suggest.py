"""``GET /api/v1/material/allocations/suggest`` 接口契约测试。

契约依据：docs/http-api.md 中 ``GET /api/v1/material/allocations/suggest`` 一节。
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
        "INSERT INTO material_inventory_balances "
        "(plant_code, location_code, material_code, lot_number, batch_number, "
        " on_hand_quantity, reserved_quantity, status, expiry_at) VALUES "
        "('PLANT-A', 'RAW-STORE', 'M-100', 'LOT-LATE', 'B-1', 5.0, 1.0, 'AVAILABLE', '2026-10-01T00:00:00Z'),"
        "('PLANT-A', 'RAW-STORE', 'M-100', 'LOT-NULL', 'B-2', 3.0, 0.0, 'AVAILABLE', NULL),"
        "('PLANT-A', 'RAW-STORE', 'M-100', 'LOT-EARLY', 'B-3', 7.0, 2.0, 'AVAILABLE', '2026-08-01T00:00:00Z'),"
        "('PLANT-A', 'RAW-STORE', 'M-100', 'LOT-FULL', 'B-4', 2.0, 2.0, 'AVAILABLE', '2026-07-01T00:00:00Z'),"
        "('PLANT-A', 'RAW-STORE', 'M-200', 'LOT-OTHER', 'B-5', 9.0, 0.0, 'AVAILABLE', '2026-08-01T00:00:00Z')",
    )


def test_suggest_success_fifo_rank(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.get(
            "/api/v1/material/allocations/suggest",
            params={"materialCode": "M-100"},
            headers={"X-Correlation-Id": "suggest-1"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["correlationId"] == "suggest-1"
    data = body["data"]
    # NULL 失效日期排最前；然后 2026-08-01 → 2026-10-01；LOT-FULL 无可用被排除
    assert [d["lotNumber"] for d in data] == ["LOT-NULL", "LOT-EARLY", "LOT-LATE"]
    assert [d["fifoRank"] for d in data] == [1, 2, 3]
    assert data[0]["availableQuantity"] == 3.0
    assert data[0]["expiryAt"] == ""
    assert data[0]["locationCode"] == "RAW-STORE"
    assert data[1]["availableQuantity"] == 5.0
    assert data[1]["expiryAt"] == "2026-08-01T00:00:00Z"


def test_suggest_missing_material_code(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.get("/api/v1/material/allocations/suggest")

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert resp.json()["error"]["message"] == "materialCode required"


def test_suggest_empty_string_material_code(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.get("/api/v1/material/allocations/suggest", params={"materialCode": ""})

    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_suggest_unknown_material(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.get("/api/v1/material/allocations/suggest", params={"materialCode": "M-NOPE"})

    assert resp.status_code == 200
    assert resp.json()["data"] == []
