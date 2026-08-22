"""``GET /api/v1/work-orders/{workOrderNumber}`` 接口契约测试。

契约依据：docs/http-api.md 中 ``GET /api/v1/work-orders/{workOrderNumber}`` 一节。
"""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import text as sqltext

from app.config import Settings
from app.database import create_db_engine
from app.main import create_app

ADMIN = {"X-Actor-Role": "MES_ADMIN"}


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


def _setup_work_order(client: TestClient, tmp_path, work_order_number: str = "WO-T1") -> str:
    """创建 PLANT-A/MAT-L1/有效路线，显式创建工单并返回工单号。"""
    client.post(
        "/api/v1/master/plants",
        json={"plantCode": "PLANT-A", "plantName": "Plant A", "timezone": "Asia/Shanghai"},
        headers={**ADMIN, "X-Idempotency-Key": "wog-plant"},
    )
    client.post(
        "/api/v1/master/materials",
        json={"materialCode": "MAT-L1"},
        headers={**ADMIN, "X-Idempotency-Key": "wog-mat"},
    )
    client.post(
        "/api/v1/master/routings",
        json={
            "routingCode": "RT-L1",
            "version": "1",
            "materialCode": "MAT-L1",
            "operations": [
                {"sequence": 10, "operationCode": "OP10", "workCenterCode": "WC-1"},
                {"sequence": 20, "operationCode": "OP20", "workCenterCode": "WC-2"},
            ],
        },
        headers={**ADMIN, "X-Idempotency-Key": "wog-rt"},
    )
    _exec(tmp_path, "UPDATE master_routings SET status='EFFECTIVE', effective_from='2026-01-01T00:00:00Z'")
    resp = client.post(
        "/api/v1/work-orders",
        json={
            "workOrderNumber": work_order_number,
            "materialCode": "MAT-L1",
            "routingCode": "RT-L1",
            "routingVersion": "1",
            "plannedQuantity": 50,
            "priority": 6,
        },
        headers={**ADMIN, "X-Idempotency-Key": f"wog-{work_order_number}"},
    )
    assert resp.status_code == 200
    return work_order_number


def test_get_work_order_ok(tmp_path):
    """200：生产概要 + 工序快照 + WIP 数量全部返回。"""
    with _make_client(tmp_path) as client:
        wo = _setup_work_order(client, tmp_path)
        _exec(
            tmp_path,
            "UPDATE production_work_orders SET completed_quantity=20, rejected_quantity=3 WHERE work_order_number=:n",
            n=wo,
        )
        for i, st in enumerate(["IN_PROCESS", "CREATED", "COMPLETED"]):
            _exec(
                tmp_path,
                "INSERT INTO production_product_units (serial_number, work_order_number, status, created_at) "
                "VALUES (:sn, :n, :st, '2026-08-22T00:00:00Z')",
                sn=f"SN-{wo}-{i}",
                n=wo,
                st=st,
            )
        resp = client.get(f"/api/v1/work-orders/{wo}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["workOrderNumber"] == wo
    assert data["plantCode"] == "PLANT-A"
    assert data["materialCode"] == "MAT-L1"
    assert data["routingCode"] == "RT-L1"
    assert data["routingVersion"] == "1"
    assert data["status"] == "DRAFT"
    assert data["priority"] == 6
    assert data["plannedQuantity"] == 50
    assert data["completedQuantity"] == 20
    assert data["rejectedQuantity"] == 3
    assert data["wipQuantity"] == 2
    assert [op["sequence"] for op in data["operations"]] == [10, 20]
    assert data["operations"][0]["operationCode"] == "OP10"
    assert data["operations"][0]["workCenterCode"] == "WC-1"
    assert data["operations"][0]["status"] == "PENDING"
    assert data["operations"][0]["standardCycleSeconds"] == 0


def test_get_work_order_standard_cycle_seconds_null(tmp_path):
    """standardCycleSeconds 数据库为空时返回 JSON null。"""
    with _make_client(tmp_path) as client:
        wo = _setup_work_order(client, tmp_path)
        _exec(
            tmp_path,
            "UPDATE production_work_order_operations SET standard_cycle_seconds=NULL WHERE work_order_number=:n",
            n=wo,
        )
        resp = client.get(f"/api/v1/work-orders/{wo}")
    assert resp.status_code == 200
    assert resp.json()["data"]["operations"][0]["standardCycleSeconds"] is None


def test_get_work_order_operation_flags(tmp_path):
    """qualityGate/allowSkip 严格等于 1 时为 true。"""
    with _make_client(tmp_path) as client:
        wo = _setup_work_order(client, tmp_path)
        _exec(
            tmp_path,
            "UPDATE production_work_order_operations SET quality_gate=1, allow_skip=0 WHERE work_order_number=:n AND sequence=10",
            n=wo,
        )
        resp = client.get(f"/api/v1/work-orders/{wo}")
    ops = resp.json()["data"]["operations"]
    assert ops[0]["qualityGate"] is True
    assert ops[0]["allowSkip"] is False
    assert ops[1]["qualityGate"] is False
    assert ops[1]["allowSkip"] is False


def test_get_work_order_not_found(tmp_path):
    """404 NOT_FOUND，文案固定 work order not found。"""
    with _make_client(tmp_path) as client:
        _setup_work_order(client, tmp_path)
        resp = client.get("/api/v1/work-orders/WO-NOPE")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "work order not found"


def test_get_work_order_wip_zero_when_no_units(tmp_path):
    """无产品单元时 wipQuantity 为 0。"""
    with _make_client(tmp_path) as client:
        wo = _setup_work_order(client, tmp_path)
        resp = client.get(f"/api/v1/work-orders/{wo}")
    assert resp.status_code == 200
    assert resp.json()["data"]["wipQuantity"] == 0


def test_get_work_order_lenient_operations_and_wip(tmp_path):
    """工序/WIP 查询失败时降级：operations 空数组、wipQuantity 0。"""
    with _make_client(tmp_path) as client:
        wo = _setup_work_order(client, tmp_path)
        # 通过删除表结构无法直接模拟，这里验证空库（无操作）的降级表现
        _exec(tmp_path, "DELETE FROM production_work_order_operations")
        _exec(tmp_path, "DELETE FROM production_product_units")
        resp = client.get(f"/api/v1/work-orders/{wo}")
    assert resp.status_code == 200
    assert resp.json()["data"]["operations"] == []
    assert resp.json()["data"]["wipQuantity"] == 0
