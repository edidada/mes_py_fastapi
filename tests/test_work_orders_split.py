"""工单拆分 / 合并接口契约测试。

契约依据：docs/http-api.md 中（2709）split 与（2731）merge 小节。
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
            rows = (await conn.execute(sqltext(sql), params)).fetchall()
        await engine.dispose()
        return rows

    return asyncio.new_event_loop().run_until_complete(_run())


def _seed_wo(tmp_path, wo, status="RELEASED", qty=100):
    _exec(
        tmp_path,
        "INSERT INTO production_work_orders "
        "(work_order_number, plant_code, material_code, routing_code, routing_version, status, planned_quantity) "
        "VALUES (:wo, 'PLANT-A', 'MAT-PROD', 'RT-1', '1', :st, :qty)",
        wo=wo, st=status, qty=qty,
    )
    _exec(
        tmp_path,
        "INSERT INTO production_work_order_operations "
        "(work_order_number, sequence, operation_code, work_center_code, quality_gate, allow_skip, standard_cycle_seconds, status) "
        "VALUES (:wo, 10, 'OP10', 'WC1', 0, 0, 60, 'PENDING')",
        wo=wo,
    )


def _h(role="PLANNER", key="k"):
    return {"X-Idempotency-Key": key, "X-Actor-Role": role, "X-Actor-Id": "P"}


# ---------------- split ----------------

def test_split_ok(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_wo(tmp_path, "WO-1", status="RELEASED")
        resp = client.post(
            "/api/v1/work-orders/WO-1/split",
            json={"splitQuantities": [40, 60]},
            headers=_h(),
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["children"] == ["WO-1-1", "WO-1-2"]
    # 子单复制工序
    ops = _query(tmp_path, "SELECT COUNT(*) FROM production_work_order_operations WHERE work_order_number='WO-1-1'")
    assert ops[0][0] == 1
    # 谱系写入
    lin = _query(tmp_path, "SELECT event_type FROM production_work_order_lineage WHERE source_work_order_number='WO-1'")
    assert lin[0][0] == "SPLIT"


def test_split_bad_state(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_wo(tmp_path, "WO-1", status="DRAFT")
        resp = client.post("/api/v1/work-orders/WO-1/split", json={"splitQuantities": [40, 60]}, headers=_h())

    assert resp.status_code == 409


def test_split_zero_qty(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_wo(tmp_path, "WO-1", status="RELEASED")
        resp = client.post("/api/v1/work-orders/WO-1/split", json={"splitQuantities": [0, 60]}, headers=_h())

    assert resp.status_code == 409


def test_split_role_forbidden(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_wo(tmp_path, "WO-1", status="RELEASED")
        resp = client.post("/api/v1/work-orders/WO-1/split", json={"splitQuantities": [40, 60]}, headers=_h(role="MES_OPERATOR"))

    assert resp.status_code == 403


def test_split_not_found(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.post("/api/v1/work-orders/NOPE/split", json={"splitQuantities": [40, 60]}, headers=_h())

    assert resp.status_code == 404


# ---------------- merge ----------------

def test_merge_ok(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_wo(tmp_path, "WO-A", status="COMPLETED", qty=30)
        _seed_wo(tmp_path, "WO-B", status="CLOSED", qty=70)
        resp = client.post(
            "/api/v1/work-orders/MERGED-1/merge",
            json={"sourceOrderNumbers": ["WO-A", "WO-B"]},
            headers=_h(),
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["workOrderNumber"] == "MERGED-1"
    assert data["plannedQuantity"] == 100
    lin = _query(tmp_path, "SELECT target_work_order_number FROM production_work_order_lineage WHERE event_type='MERGED'")
    assert lin[0][0] == "MERGED-1"


def test_merge_source_not_completed(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_wo(tmp_path, "WO-A", status="RELEASED", qty=30)
        _seed_wo(tmp_path, "WO-B", status="CLOSED", qty=70)
        resp = client.post(
            "/api/v1/work-orders/MERGED-1/merge",
            json={"sourceOrderNumbers": ["WO-A", "WO-B"]},
            headers=_h(),
        )

    assert resp.status_code == 409


def test_merge_source_not_found(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.post(
            "/api/v1/work-orders/MERGED-1/merge",
            json={"sourceOrderNumbers": ["WO-NOPE"]},
            headers=_h(),
        )

    assert resp.status_code == 404


def test_merge_role_forbidden(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_wo(tmp_path, "WO-A", status="COMPLETED", qty=30)
        _seed_wo(tmp_path, "WO-B", status="CLOSED", qty=70)
        resp = client.post(
            "/api/v1/work-orders/MERGED-1/merge",
            json={"sourceOrderNumbers": ["WO-A", "WO-B"]},
            headers=_h(role="MES_OPERATOR"),
        )

    assert resp.status_code == 403
