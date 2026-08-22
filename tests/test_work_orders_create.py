"""``POST /api/v1/work-orders`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/work-orders`` 一节。
"""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import text as sqltext

from app.config import Settings
from app.database import create_db_engine
from app.main import create_app

PLANNER = {"X-Actor-Role": "PLANNER"}
ADMIN = {"X-Actor-Role": "MES_ADMIN"}
OPERATOR = {"X-Actor-Role": "MES_OPERATOR"}


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


def _setup_master(client: TestClient, tmp_path):
    """创建 PLANT-A、物料 MAT-L1、含两道工序的路线并置为 EFFECTIVE。"""
    client.post(
        "/api/v1/master/plants",
        json={"plantCode": "PLANT-A", "plantName": "Plant A", "timezone": "Asia/Shanghai"},
        headers={**ADMIN, "X-Idempotency-Key": "woc-plant"},
    )
    client.post(
        "/api/v1/master/materials",
        json={"materialCode": "MAT-L1"},
        headers={**ADMIN, "X-Idempotency-Key": "woc-mat"},
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
        headers={**ADMIN, "X-Idempotency-Key": "woc-rt"},
    )
    _exec(
        tmp_path,
        "UPDATE master_routings SET status='EFFECTIVE', effective_from='2026-01-01T00:00:00Z'",
    )


def _create(client: TestClient, key: str, **overrides):
    body = {
        "workOrderNumber": "WO-T1",
        "materialCode": "MAT-L1",
        "routingCode": "RT-L1",
        "routingVersion": "1",
        "plannedQuantity": 50,
    }
    body.update(overrides)
    return client.post(
        "/api/v1/work-orders",
        json=body,
        headers={**PLANNER, "X-Idempotency-Key": key},
    )


def test_create_work_order_ok(tmp_path):
    """成功创建：200、DRAFT 状态、默认工厂、工序快照复制、审计落库。"""
    with _make_client(tmp_path) as client:
        _setup_master(client, tmp_path)
        resp = _create(client, "k1", workOrderNumber="WO-T1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["workOrderNumber"] == "WO-T1"
    assert body["data"]["status"] == "DRAFT"
    assert "cached" not in body["data"]
    assert body["meta"]["correlationId"].startswith("corr-")
    assert "generatedAt" in body["meta"]

    rows = _query(
        tmp_path,
        "SELECT plant_code, material_code, routing_code, routing_version, status, planned_quantity "
        "FROM production_work_orders WHERE work_order_number='WO-T1'",
    )
    assert len(rows) == 1
    assert rows[0][0] == "PLANT-A"
    assert rows[0][5] == 50

    ops = _query(
        tmp_path,
        "SELECT sequence, status FROM production_work_order_operations WHERE work_order_number='WO-T1' ORDER BY sequence",
    )
    assert [(r[0], r[1]) for r in ops] == [(10, "PENDING"), (20, "PENDING")]

    audit = _query(tmp_path, "SELECT action, resource_type, actor_id FROM audit_events WHERE resource_id='WO-T1'")
    assert audit[0][0] == "WORK_ORDER_CREATE"
    assert audit[0][2] == "system"


def test_create_work_order_idempotent(tmp_path):
    """幂等重放：200、cached=true、不含 status，工单号不变。"""
    with _make_client(tmp_path) as client:
        _setup_master(client, tmp_path)
        first = _create(client, "k2", workOrderNumber="WO-T2")
        assert first.status_code == 200
        replay = _create(client, "k2", workOrderNumber="WO-OTHER", quantity=999)
    assert replay.status_code == 200
    body = replay.json()
    assert body["data"]["workOrderNumber"] == "WO-T2"
    assert body["data"]["cached"] is True
    assert "status" not in body["data"]


def test_create_work_order_role_required(tmp_path):
    """角色校验：403 FORBIDDEN（固定文案 PLANNER required），且先于幂等查询。"""
    with _make_client(tmp_path) as client:
        _setup_master(client, tmp_path)
        # 无角色头
        resp = client.post(
            "/api/v1/work-orders",
            json={"workOrderNumber": "WO-X1"},
            headers={"X-Idempotency-Key": "k3"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"
        assert resp.json()["error"]["message"] == "PLANNER required"

        # MES_OPERATOR 不允许
        resp = client.post(
            "/api/v1/work-orders",
            json={"workOrderNumber": "WO-X1"},
            headers={**OPERATOR, "X-Idempotency-Key": "k3"},
        )
        assert resp.status_code == 403

        # 先以 PLANNER 创建成功，再以 OPERATOR 重放同样必须 403
        assert _create(client, "k3", workOrderNumber="WO-X1").status_code == 200
        resp = client.post(
            "/api/v1/work-orders",
            json={"workOrderNumber": "WO-X1"},
            headers={**OPERATOR, "X-Idempotency-Key": "k3"},
        )
        assert resp.status_code == 403


def test_create_work_order_validation(tmp_path):
    """校验错误：422 VALIDATION_ERROR。"""
    with _make_client(tmp_path) as client:
        _setup_master(client, tmp_path)
        # 缺幂等键
        resp = client.post(
            "/api/v1/work-orders",
            json={"workOrderNumber": "WO-X2"},
            headers=PLANNER,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

        # 空工单号
        resp = _create(client, "k5", workOrderNumber="   ")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

        # 路线版本不存在 / 非 EFFECTIVE
        resp = _create(client, "k6", routingVersion="9")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

        # 非法 JSON
        resp = client.post(
            "/api/v1/work-orders",
            content="{not-json",
            headers={**PLANNER, "X-Idempotency-Key": "k7", "Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_work_order_conflicts(tmp_path):
    """冲突：409 CONFLICT。"""
    with _make_client(tmp_path) as client:
        _setup_master(client, tmp_path)
        assert _create(client, "k9", workOrderNumber="WO-T9").status_code == 200

        # 工单号重复 → 409，消息保留 SQLite 错误
        resp = _create(client, "k10", workOrderNumber="WO-T9")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"
        assert "UNIQUE constraint failed" in resp.json()["error"]["message"]

        # 未知工厂
        resp = _create(client, "k11", workOrderNumber="WO-T11", plantCode="PLANT-X")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"

        # 未知物料
        resp = _create(client, "k12", workOrderNumber="WO-T12", materialCode="MAT-X")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"

        # 计划数量非正
        resp = _create(client, "k13", workOrderNumber="WO-T13", plannedQuantity=0)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"


def test_create_work_order_field_coercion(tmp_path):
    """字段宽松兼容：priority 截断/字符串转换、bom/时间默认空串、工厂默认 PLANT-A。"""
    with _make_client(tmp_path) as client:
        _setup_master(client, tmp_path)
        resp = _create(
            client,
            "k14",
            workOrderNumber="WO-T14",
            plantCode=None,
            priority="7",
            bomCode=None,
            bomVersion=123,
            plannedStartAt=None,
            plannedEndAt="2026-08-30T00:00:00Z",
        )
        assert resp.status_code == 200
        resp = _create(
            client,
            "k15",
            workOrderNumber="WO-T15",
            priority=3.9,
        )
        assert resp.status_code == 200
        resp = _create(
            client,
            "k16",
            workOrderNumber="WO-T16",
            priority="abc",
        )
        assert resp.status_code == 200

    rows = _query(tmp_path, "SELECT work_order_number, priority, bom_code, bom_version FROM production_work_orders ORDER BY work_order_number")
    by_num = {r[0]: r for r in rows}
    assert by_num["WO-T14"][1] == 7
    assert by_num["WO-T14"][2] == ""
    assert by_num["WO-T15"][1] == 3
    assert by_num["WO-T16"][1] == 0
