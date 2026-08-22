"""``POST /api/v1/production-plans/import`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/production-plans/import`` 一节。
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


def _setup_master(client: TestClient):
    """创建 PLANT-A、物料、有效路线。"""
    client.post(
        "/api/v1/master/plants",
        json={"plantCode": "PLANT-A", "plantName": "Plant A", "timezone": "Asia/Shanghai"},
        headers={**ADMIN, "X-Idempotency-Key": "pp-plant"},
    )
    client.post(
        "/api/v1/master/materials",
        json={"materialCode": "MAT-P1"},
        headers={**ADMIN, "X-Idempotency-Key": "pp-mat"},
    )
    # 直接 SQL 建有效路线（创建接口只支持 DRAFT）
    client.post(
        "/api/v1/master/routings",
        json={
            "routingCode": "RT-P1",
            "version": "1",
            "materialCode": "MAT-P1",
            "operations": [
                {"sequence": 10, "operationCode": "OP10", "workCenterCode": "WC-1"},
                {"sequence": 20, "operationCode": "OP20", "workCenterCode": "WC-2"},
            ],
        },
        headers={**ADMIN, "X-Idempotency-Key": "pp-rt"},
    )


def _make_effective_routing(tmp_path, routing_code="RT-P1", version="1", status="EFFECTIVE"):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    async def _update():
        engine = await create_db_engine(db_url)
        async with engine.begin() as conn:
            await conn.execute(
                sqltext("UPDATE master_routings SET status=:s, effective_from='2026-01-01T00:00:00Z' WHERE routing_code=:rc AND version=:v"),
                {"s": status, "rc": routing_code, "v": version},
            )
        await engine.dispose()

    asyncio.new_event_loop().run_until_complete(_update())


def _post(client: TestClient, key: str, body: dict, **headers):
    return client.post(
        "/api/v1/production-plans/import",
        json=body,
        headers={"X-Idempotency-Key": key, **headers},
    )


def test_import_plan_ok(tmp_path):
    """正常导入返回 202，planId/status/workOrderNumber/workOrderStatus。"""
    with _make_client(tmp_path) as client:
        _setup_master(client)
        _make_effective_routing(tmp_path)
        resp = _post(
            client,
            "pl1",
            {"sourceSystem": "ERP", "externalReference": "REF-001", "plantCode": "PLANT-A", "materialCode": "MAT-P1", "quantity": 100, "priority": 5, "dueAt": "2026-09-01T00:00:00Z"},
        )
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["planId"].startswith("PLN-")
    assert data["status"] == "VALIDATED"
    assert data["workOrderNumber"].startswith("WO-")
    assert data["workOrderStatus"] == "DRAFT"


def test_import_plan_creates_work_order_with_operations(tmp_path):
    """工单与路线工序快照被创建。"""
    with _make_client(tmp_path) as client:
        _setup_master(client)
        _make_effective_routing(tmp_path)
        resp = _post(
            client,
            "pl2",
            {"sourceSystem": "ERP", "externalReference": "REF-002", "plantCode": "PLANT-A", "materialCode": "MAT-P1", "quantity": 50},
        )
        wo = resp.json()["data"]["workOrderNumber"]

        from sqlalchemy import text as sqltext2

        db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

        async def _check():
            engine = await create_db_engine(db_url)
            async with engine.connect() as conn:
                order = (
                    await conn.execute(
                        sqltext2("SELECT status, material_code, routing_code, routing_version, planned_quantity, bom_code FROM production_work_orders WHERE work_order_number=:wo"),
                        {"wo": wo},
                    )
                ).first()
                ops = (
                    await conn.execute(
                        sqltext2("SELECT sequence, operation_code, status FROM production_work_order_operations WHERE work_order_number=:wo ORDER BY sequence"),
                        {"wo": wo},
                    )
                ).all()
            await engine.dispose()
            return order, ops

        order, ops = asyncio.new_event_loop().run_until_complete(_check())
    assert order[0] == "DRAFT"
    assert order[1] == "MAT-P1"
    assert order[2] == "RT-P1"
    assert order[3] == "1"
    assert order[4] == 50
    assert order[5] == ""
    assert [(o[0], o[1], o[2]) for o in ops] == [(10, "OP10", "PENDING"), (20, "OP20", "PENDING")]


def test_import_plan_no_role_check(tmp_path):
    """任意角色或省略角色均可调用。"""
    with _make_client(tmp_path) as client:
        _setup_master(client)
        _make_effective_routing(tmp_path)
        resp = _post(
            client,
            "pl3",
            {"sourceSystem": "ERP", "externalReference": "REF-003", "plantCode": "PLANT-A", "materialCode": "MAT-P1", "quantity": 10},
            **{"X-Actor-Role": "SOME_OTHER_ROLE"},
        )
    assert resp.status_code == 202


def test_import_plan_idempotent(tmp_path):
    """幂等命中返回 200 + planId + cached=true。"""
    with _make_client(tmp_path) as client:
        _setup_master(client)
        _make_effective_routing(tmp_path)
        body = {"sourceSystem": "ERP", "externalReference": "REF-004", "plantCode": "PLANT-A", "materialCode": "MAT-P1", "quantity": 10}
        _post(client, "pl-dup", body)
        resp = _post(client, "pl-dup", body)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["planId"].startswith("PLN-")
    assert data["cached"] is True


def test_import_plan_material_not_found(tmp_path):
    """物料不存在返回 404 NOT_FOUND。"""
    with _make_client(tmp_path) as client:
        resp = _post(
            client,
            "pl4",
            {"sourceSystem": "ERP", "externalReference": "REF-005", "plantCode": "PLANT-A", "materialCode": "NO-SUCH", "quantity": 10},
        )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_import_plan_no_effective_routing(tmp_path):
    """物料无有效路线返回 422 VALIDATION_ERROR，计划被回滚。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    with _make_client(tmp_path) as client:
        _setup_master(client)  # 路线仍是 DRAFT
        resp = _post(
            client,
            "pl5",
            {"sourceSystem": "ERP", "externalReference": "REF-006", "plantCode": "PLANT-A", "materialCode": "MAT-P1", "quantity": 10},
        )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    async def _count():
        engine = await create_db_engine(db_url)
        async with engine.connect() as conn:
            n = (await conn.execute(sqltext("SELECT COUNT(*) FROM production_production_plans"))).scalar()
        await engine.dispose()
        return n

    assert asyncio.new_event_loop().run_until_complete(_count()) == 0


def test_import_plan_invalid_plant(tmp_path):
    """工厂无效返回 409 duplicate external reference。"""
    with _make_client(tmp_path) as client:
        _setup_master(client)
        _make_effective_routing(tmp_path)
        resp = _post(
            client,
            "pl6",
            {"sourceSystem": "ERP", "externalReference": "REF-007", "plantCode": "PLANT-X", "materialCode": "MAT-P1", "quantity": 10},
        )
    assert resp.status_code == 409
    assert resp.json()["error"]["message"] == "duplicate external reference"


def test_import_plan_non_positive_quantity(tmp_path):
    """数量非正返回 409 duplicate external reference。"""
    with _make_client(tmp_path) as client:
        _setup_master(client)
        _make_effective_routing(tmp_path)
        resp = _post(
            client,
            "pl7",
            {"sourceSystem": "ERP", "externalReference": "REF-008", "plantCode": "PLANT-A", "materialCode": "MAT-P1", "quantity": 0},
        )
    assert resp.status_code == 409
    assert resp.json()["error"]["message"] == "duplicate external reference"


def test_import_plan_duplicate_reference(tmp_path):
    """计划唯一键重复返回 409 duplicate external reference。"""
    with _make_client(tmp_path) as client:
        _setup_master(client)
        _make_effective_routing(tmp_path)
        body = {"sourceSystem": "ERP", "externalReference": "REF-DUP", "plantCode": "PLANT-A", "materialCode": "MAT-P1", "quantity": 10}
        _post(client, "pl8", body)
        resp = _post(client, "pl9", body)
    assert resp.status_code == 409
    assert resp.json()["error"]["message"] == "duplicate external reference"


def test_import_plan_validation(tmp_path):
    """缺幂等键或显式必传字段返回 422。"""
    with _make_client(tmp_path) as client:
        resp = client.post("/api/v1/production-plans/import", json={"sourceSystem": "ERP"})
        assert resp.status_code == 422

        resp = _post(client, "pl10", {"sourceSystem": "ERP", "externalReference": "REF-010"})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
