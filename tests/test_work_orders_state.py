"""``POST /api/v1/work-orders/{workOrderNumber}/state`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/work-orders/{workOrderNumber}/state`` 一节。
"""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import text as sqltext

from app.config import Settings
from app.database import create_db_engine
from app.main import create_app

ADMIN = {"X-Actor-Role": "MES_ADMIN"}
PLANNER = {"X-Actor-Role": "PLANNER"}
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


def _setup_work_order(client: TestClient, tmp_path, work_order_number: str = "WO-T1") -> str:
    client.post(
        "/api/v1/master/plants",
        json={"plantCode": "PLANT-A", "plantName": "Plant A", "timezone": "Asia/Shanghai"},
        headers={**ADMIN, "X-Idempotency-Key": "wost-plant"},
    )
    client.post(
        "/api/v1/master/materials",
        json={"materialCode": "MAT-L1"},
        headers={**ADMIN, "X-Idempotency-Key": "wost-mat"},
    )
    client.post(
        "/api/v1/master/routings",
        json={
            "routingCode": "RT-L1",
            "version": "1",
            "materialCode": "MAT-L1",
            "operations": [{"sequence": 10, "operationCode": "OP10", "workCenterCode": "WC-1"}],
        },
        headers={**ADMIN, "X-Idempotency-Key": "wost-rt"},
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
        },
        headers={**ADMIN, "X-Idempotency-Key": f"wost-{work_order_number}"},
    )
    assert resp.status_code == 200
    return work_order_number


def _transition(client: TestClient, key: str, wo: str, **overrides):
    body = {"targetStatus": "RELEASED"}
    body.update(overrides)
    return client.post(
        f"/api/v1/work-orders/{wo}/state",
        json=body,
        headers={**PLANNER, "X-Idempotency-Key": key},
    )


def test_transition_draft_to_released_ok(tmp_path):
    """200：状态更新、工序置 READY、审计与 outbox 落库。"""
    with _make_client(tmp_path) as client:
        wo = _setup_work_order(client, tmp_path)
        resp = _transition(client, "st1", wo)
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["workOrderNumber"] == wo
    assert body["data"]["status"] == "RELEASED"
    assert "cached" not in body["data"]

    row = _query(tmp_path, "SELECT status, updated_at FROM production_work_orders WHERE work_order_number=:n", n=wo)
    assert row[0][0] == "RELEASED"
    assert row[0][1]
    op = _query(
        tmp_path, "SELECT status FROM production_work_order_operations WHERE work_order_number=:n", n=wo
    )
    assert op[0][0] == "READY"
    audit = _query(
        tmp_path,
        "SELECT action, before_data, after_data FROM audit_events "
        "WHERE resource_id=:n AND action='WORK_ORDER_TRANSITION'",
        n=wo,
    )
    assert len(audit) == 1
    assert '"from": "DRAFT"' in audit[0][1]
    assert '"to": "RELEASED"' in audit[0][2]
    outbox = _query(
        tmp_path,
        "SELECT aggregate_type, aggregate_id, event_type, status FROM integration_outbox_messages WHERE aggregate_id=:n",
        n=wo,
    )
    assert len(outbox) == 1
    assert outbox[0][0] == "WORK_ORDER"
    assert outbox[0][2] == "work_order.state.changed"
    assert outbox[0][3] == "PENDING"


def test_transition_full_chain(tmp_path):
    """完整合法状态链 DRAFT→RELEASED→IN_PROGRESS→SUSPENDED→RELEASED→IN_PROGRESS→COMPLETED→CLOSED。"""
    with _make_client(tmp_path) as client:
        wo = _setup_work_order(client, tmp_path)
        for key, target in [
            ("st2a", "RELEASED"),
            ("st2b", "IN_PROGRESS"),
            ("st2c", "SUSPENDED"),
            ("st2d", "RELEASED"),
            ("st2e", "IN_PROGRESS"),
            ("st2f", "COMPLETED"),
            ("st2g", "CLOSED"),
        ]:
            resp = _transition(client, key, wo, targetStatus=target)
            assert resp.status_code == 200, f"{target} failed"
            assert resp.json()["data"]["status"] == target
    row = _query(tmp_path, "SELECT status FROM production_work_orders WHERE work_order_number=:n", n=wo)
    assert row[0][0] == "CLOSED"


def test_transition_invalid(tmp_path):
    """非法迁移：409 CONFLICT，文案 invalid transition {from}->{target}。"""
    with _make_client(tmp_path) as client:
        wo = _setup_work_order(client, tmp_path)
        resp = _transition(client, "st3", wo, targetStatus="IN_PROGRESS")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "CONFLICT"
    assert body["error"]["message"] == "invalid transition DRAFT->IN_PROGRESS"


def test_transition_missing_target(tmp_path):
    """缺失 targetStatus：空目标 → 409。"""
    with _make_client(tmp_path) as client:
        wo = _setup_work_order(client, tmp_path)
        resp = client.post(
            f"/api/v1/work-orders/{wo}/state",
            json={},
            headers={**PLANNER, "X-Idempotency-Key": "st4"},
        )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_transition_not_found(tmp_path):
    """工单不存在：404 NOT_FOUND。"""
    with _make_client(tmp_path) as client:
        _setup_work_order(client, tmp_path)
        resp = client.post(
            "/api/v1/work-orders/WO-NOPE/state",
            json={"targetStatus": "RELEASED"},
            headers={**PLANNER, "X-Idempotency-Key": "st5"},
        )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_transition_role_and_idempotent(tmp_path):
    """角色校验先于幂等；重放返回 200 + cached=true。"""
    with _make_client(tmp_path) as client:
        wo = _setup_work_order(client, tmp_path)
        # 无角色
        resp = client.post(
            f"/api/v1/work-orders/{wo}/state",
            json={"targetStatus": "RELEASED"},
            headers={"X-Idempotency-Key": "st6"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["message"] == "PLANNER required"
        # OPERATOR 不允许
        resp = client.post(
            f"/api/v1/work-orders/{wo}/state",
            json={"targetStatus": "RELEASED"},
            headers={**OPERATOR, "X-Idempotency-Key": "st6"},
        )
        assert resp.status_code == 403
        # PLANNER 成功
        assert _transition(client, "st6", wo).status_code == 200
        # 重放
        resp = _transition(client, "st6", wo, targetStatus="IN_PROGRESS")
        assert resp.status_code == 200
        assert resp.json()["data"]["workOrderNumber"] == wo
        assert resp.json()["data"]["cached"] is True
        assert "status" not in resp.json()["data"]
