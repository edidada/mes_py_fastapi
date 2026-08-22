"""``GET /api/v1/work-orders`` 接口契约测试。

契约依据：docs/http-api.md 中 ``GET /api/v1/work-orders`` 一节。
"""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import text as sqltext

from app.config import Settings
from app.database import create_db_engine
from app.main import create_app

ADMIN = {"X-Actor-Role": "MES_ADMIN"}


def _make_client(tmp_path) -> TestClient:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    return TestClient(create_app(settings))


def _db_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


def _exec(tmp_path, sql, **params):
    async def _run():
        engine = await create_db_engine(_db_url(tmp_path))
        async with engine.begin() as conn:
            await conn.execute(sqltext(sql), params)
        await engine.dispose()

    asyncio.new_event_loop().run_until_complete(_run())


def _setup_master(client: TestClient):
    """创建 PLANT-A、物料、有效路线。"""
    client.post(
        "/api/v1/master/plants",
        json={"plantCode": "PLANT-A", "plantName": "Plant A", "timezone": "Asia/Shanghai"},
        headers={**ADMIN, "X-Idempotency-Key": "wl-plant"},
    )
    client.post(
        "/api/v1/master/materials",
        json={"materialCode": "MAT-L1"},
        headers={**ADMIN, "X-Idempotency-Key": "wl-mat"},
    )
    client.post(
        "/api/v1/master/routings",
        json={
            "routingCode": "RT-L1",
            "version": "1",
            "materialCode": "MAT-L1",
            "operations": [{"sequence": 10, "operationCode": "OP10", "workCenterCode": "WC-1"}],
        },
        headers={**ADMIN, "X-Idempotency-Key": "wl-rt"},
    )


def _make_effective_routing(tmp_path):
    async def _update():
        engine = await create_db_engine(_db_url(tmp_path))
        async with engine.begin() as conn:
            await conn.execute(
                sqltext("UPDATE master_routings SET status='EFFECTIVE', effective_from='2026-01-01T00:00:00Z'")
            )
        await engine.dispose()

    asyncio.new_event_loop().run_until_complete(_update())


def _import(client: TestClient, key: str, quantity: int):
    resp = client.post(
        "/api/v1/production-plans/import",
        json={
            "sourceSystem": "ERP",
            "externalReference": f"REF-{key}",
            "plantCode": "PLANT-A",
            "materialCode": "MAT-L1",
            "quantity": quantity,
        },
        headers={"X-Idempotency-Key": key},
    )
    assert resp.status_code == 202
    return resp.json()["data"]["workOrderNumber"]


def test_list_work_orders_empty_double_envelope(tmp_path):
    """空库返回 200，双层信封结构正确。"""
    with _make_client(tmp_path) as client:
        resp = client.get("/api/v1/work-orders")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["data"] == []
    assert body["data"]["meta"]["page"] == 1
    assert body["data"]["meta"]["pageSize"] == 50
    assert body["data"]["meta"]["total"] == 0
    assert "correlationId" in body["data"]["meta"]
    assert "generatedAt" in body["data"]["meta"]
    assert "correlationId" in body["meta"]
    assert body["meta"]["correlationId"] == body["data"]["meta"]["correlationId"]


def test_list_work_orders_fields_and_progress(tmp_path):
    """返回全部字段，含 completionPercent 与 wipQuantity。"""
    with _make_client(tmp_path) as client:
        _setup_master(client)
        _make_effective_routing(tmp_path)
        wo = _import(client, "wl1", 100)
        _exec(
            tmp_path,
            "UPDATE production_work_orders SET completed_quantity=25, rejected_quantity=5, priority=7 WHERE work_order_number=:wo",
            wo=wo,
        )
        for i, st in enumerate(["IN_PROCESS", "REWORK", "COMPLETED"]):
            _exec(
                tmp_path,
                "INSERT INTO production_product_units (serial_number, work_order_number, status, created_at) VALUES (:sn, :wo, :st, '2026-08-22T00:00:00Z')",
                sn=f"SN-{wo}-{i}",
                wo=wo,
                st=st,
            )
        resp = client.get("/api/v1/work-orders")
    assert resp.status_code == 200
    item = resp.json()["data"]["data"][0]
    assert item["workOrderNumber"] == wo
    assert item["plantCode"] == "PLANT-A"
    assert item["materialCode"] == "MAT-L1"
    assert item["status"] == "DRAFT"
    assert item["priority"] == 7
    assert item["plannedQuantity"] == 100
    assert item["completedQuantity"] == 25
    assert item["rejectedQuantity"] == 5
    assert item["completionPercent"] == 25.0
    assert item["wipQuantity"] == 2


def test_list_work_orders_zero_planned_percent(tmp_path):
    """计划数量非正时 completionPercent 为 0。"""
    with _make_client(tmp_path) as client:
        _setup_master(client)
        _make_effective_routing(tmp_path)
        wo = _import(client, "wl2", 100)
        _exec(
            tmp_path,
            "UPDATE production_work_orders SET planned_quantity=0, completed_quantity=50 WHERE work_order_number=:wo",
            wo=wo,
        )
        resp = client.get("/api/v1/work-orders")
    item = resp.json()["data"]["data"][0]
    assert item["completionPercent"] == 0
    assert item["plannedQuantity"] == 0


def test_list_work_orders_filters_and_sorting(tmp_path):
    """状态/工厂/物料过滤；priority DESC、工单号 ASC 排序。"""
    with _make_client(tmp_path) as client:
        _setup_master(client)
        _make_effective_routing(tmp_path)
        wo1 = _import(client, "wl3a", 10)
        wo2 = _import(client, "wl3b", 10)
        wo3 = _import(client, "wl3c", 10)
        for new_num, prio, st, old_num in [
            ("WO-B", 30, "COMPLETED", wo1),
            ("WO-A", 30, "IN_PROCESS", wo2),
            ("WO-C", 10, "COMPLETED", wo3),
        ]:
            _exec(
                tmp_path,
                "UPDATE production_work_orders SET work_order_number=:n, priority=:p, status=:s WHERE work_order_number=:o",
                n=new_num,
                p=prio,
                s=st,
                o=old_num,
            )

        resp = client.get("/api/v1/work-orders")
        nums = [i["workOrderNumber"] for i in resp.json()["data"]["data"]]
        assert nums == ["WO-A", "WO-B", "WO-C"]

        # 状态区分大小写精确匹配
        resp = client.get("/api/v1/work-orders?status=COMPLETED")
        assert [i["workOrderNumber"] for i in resp.json()["data"]["data"]] == ["WO-B", "WO-C"]
        assert resp.json()["data"]["meta"]["total"] == 2

        resp = client.get("/api/v1/work-orders?status=completed")
        assert resp.json()["data"]["data"] == []

        # 工厂 + 物料组合过滤
        resp = client.get("/api/v1/work-orders?plantCode=PLANT-A&materialCode=MAT-L1")
        assert resp.json()["data"]["meta"]["total"] == 3
