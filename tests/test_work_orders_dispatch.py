"""``POST /api/v1/work-orders/{workOrderNumber}/dispatch`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/work-orders/{workOrderNumber}/dispatch`` 一节。
"""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import text as sqltext

from app.config import Settings
from app.database import create_db_engine
from app.main import create_app

ADMIN = {"X-Actor-Role": "MES_ADMIN"}
SUPERVISOR = {"X-Actor-Role": "MES_SUPERVISOR"}
PLANNER = {"X-Actor-Role": "PLANNER"}


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


def _setup(client: TestClient, tmp_path) -> str:
    """创建工厂/物料/路线/工单/站点/员工，返回工单号。"""
    client.post(
        "/api/v1/master/plants",
        json={"plantCode": "PLANT-A", "plantName": "Plant A", "timezone": "Asia/Shanghai"},
        headers={**ADMIN, "X-Idempotency-Key": "wd-plant"},
    )
    client.post(
        "/api/v1/master/materials",
        json={"materialCode": "MAT-L1"},
        headers={**ADMIN, "X-Idempotency-Key": "wd-mat"},
    )
    client.post(
        "/api/v1/master/routings",
        json={
            "routingCode": "RT-L1",
            "version": "1",
            "materialCode": "MAT-L1",
            "operations": [{"sequence": 10, "operationCode": "OP10", "workCenterCode": "WC-1"}],
        },
        headers={**ADMIN, "X-Idempotency-Key": "wd-rt"},
    )
    client.post(
        "/api/v1/master/workers",
        json={"workerId": "W-001", "teamCode": "TEAM-A"},
        headers={**ADMIN, "X-Idempotency-Key": "wd-worker"},
    )
    _exec(tmp_path, "UPDATE master_routings SET status='EFFECTIVE', effective_from='2026-01-01T00:00:00Z'")
    _exec(
        tmp_path,
        "INSERT INTO master_stations (station_code, station_name, plant_code, work_center_code, active) "
        "VALUES ('ST-1', 'Station 1', 'PLANT-A', 'WC-1', 1)",
    )
    resp = client.post(
        "/api/v1/work-orders",
        json={
            "workOrderNumber": "WO-T1",
            "materialCode": "MAT-L1",
            "routingCode": "RT-L1",
            "routingVersion": "1",
            "plannedQuantity": 50,
        },
        headers={**ADMIN, "X-Idempotency-Key": "wd-wo"},
    )
    assert resp.status_code == 200
    return "WO-T1"


def _dispatch(client: TestClient, key: str, wo: str, **overrides):
    body = {"operationSequence": 10, "stationCode": "ST-1", "workerId": "W-001"}
    body.update(overrides)
    return client.post(
        f"/api/v1/work-orders/{wo}/dispatch",
        json=body,
        headers={**SUPERVISOR, "X-Idempotency-Key": key},
    )


def test_dispatch_ok(tmp_path):
    """200：返回 DSP-... 派工 ID 与 DISPATCHED，派工记录与审计落库。"""
    with _make_client(tmp_path) as client:
        wo = _setup(client, tmp_path)
        resp = _dispatch(client, "d1", wo)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["assignmentId"].startswith("DSP-")
    assert data["status"] == "DISPATCHED"
    assert "cached" not in data

    rows = _query(
        tmp_path,
        "SELECT work_order_number, operation_sequence, station_code, worker_id, status, priority "
        "FROM production_dispatch_assignments",
    )
    assert len(rows) == 1
    assert rows[0][0] == wo
    assert rows[0][1] == 10
    assert rows[0][2] == "ST-1"
    assert rows[0][3] == "W-001"
    assert rows[0][4] == "DISPATCHED"
    assert rows[0][5] == 0

    audit = _query(tmp_path, "SELECT action, resource_type FROM audit_events WHERE action='DISPATCH'")
    assert len(audit) == 1
    assert audit[0][1] == "WORK_ORDER"


def test_dispatch_updates_existing_task(tmp_path):
    """存在匹配任务时更新站点/员工/状态为 DISPATCHED。"""
    with _make_client(tmp_path) as client:
        wo = _setup(client, tmp_path)
        _exec(
            tmp_path,
            "INSERT INTO production_operation_tasks (task_id, work_order_number, operation_sequence, status, created_at) "
            "VALUES ('TSK-1', :wo, 10, 'PENDING', '2026-08-22T00:00:00Z')",
            wo=wo,
        )
        resp = _dispatch(client, "d2", wo)
        assert resp.status_code == 200
    task = _query(
        tmp_path,
        "SELECT station_code, worker_id, status FROM production_operation_tasks WHERE task_id='TSK-1'",
    )
    assert task[0][0] == "ST-1"
    assert task[0][1] == "W-001"
    assert task[0][2] == "DISPATCHED"


def test_dispatch_no_matching_task_ok(tmp_path):
    """没有匹配任务时影响零行，但派工仍成功。"""
    with _make_client(tmp_path) as client:
        wo = _setup(client, tmp_path)
        resp = _dispatch(client, "d3", wo, operationSequence=99)
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "DISPATCHED"


def test_dispatch_idempotent(tmp_path):
    """幂等重放：200、cached=true，返回首次派工 ID。"""
    with _make_client(tmp_path) as client:
        wo = _setup(client, tmp_path)
        first = _dispatch(client, "d4", wo)
        assert first.status_code == 200
        replay = _dispatch(client, "d4", wo, stationCode="ST-X", workerId="W-X")
    assert replay.status_code == 200
    data = replay.json()["data"]
    assert data["assignmentId"] == first.json()["data"]["assignmentId"]
    assert data["cached"] is True
    assert "status" not in data


def test_dispatch_role_required(tmp_path):
    """角色校验先于幂等：PLANNER/无角色返回 403，固定文案 MES_SUPERVISOR required。"""
    with _make_client(tmp_path) as client:
        wo = _setup(client, tmp_path)
        resp = client.post(
            f"/api/v1/work-orders/{wo}/dispatch",
            json={"stationCode": "ST-1"},
            headers={"X-Idempotency-Key": "d5"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"
        assert resp.json()["error"]["message"] == "MES_SUPERVISOR required"

        resp = client.post(
            f"/api/v1/work-orders/{wo}/dispatch",
            json={"stationCode": "ST-1"},
            headers={**PLANNER, "X-Idempotency-Key": "d5"},
        )
        assert resp.status_code == 403

        # 成功创建后 PLANNER 重放仍 403
        assert _dispatch(client, "d5", wo).status_code == 200
        resp = client.post(
            f"/api/v1/work-orders/{wo}/dispatch",
            json={"stationCode": "ST-1"},
            headers={**PLANNER, "X-Idempotency-Key": "d5"},
        )
        assert resp.status_code == 403


def test_dispatch_constraint_failures(tmp_path):
    """违反外键/缺失字段 → 422，消息保留 SQLite 错误。"""
    with _make_client(tmp_path) as client:
        wo = _setup(client, tmp_path)
        # 工单不存在
        resp = _dispatch(client, "d6", "WO-NOPE")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

        # 未知站点
        resp = _dispatch(client, "d7", wo, stationCode="ST-X")
        assert resp.status_code == 422

        # 未知员工
        resp = _dispatch(client, "d8", wo, workerId="W-X")
        assert resp.status_code == 422

        # 站点省略 → 空串违反外键
        resp = _dispatch(client, "d9", wo, stationCode=None)
        assert resp.status_code == 422

        # 员工省略 → 空串违反外键
        resp = _dispatch(client, "d10", wo, workerId=None)
        assert resp.status_code == 422


def test_dispatch_validation(tmp_path):
    """缺幂等键/非法 JSON → 422。"""
    with _make_client(tmp_path) as client:
        wo = _setup(client, tmp_path)
        resp = client.post(
            f"/api/v1/work-orders/{wo}/dispatch",
            json={"stationCode": "ST-1", "workerId": "W-001"},
            headers=SUPERVISOR,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

        resp = client.post(
            f"/api/v1/work-orders/{wo}/dispatch",
            content="{bad",
            headers={**SUPERVISOR, "X-Idempotency-Key": "d11", "Content-Type": "application/json"},
        )
        assert resp.status_code == 422
