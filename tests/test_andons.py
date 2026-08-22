"""``/api/v1/andons`` 三个 URL（创建/列表/关闭）契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/andons``、``GET /api/v1/andons``、
``POST /api/v1/andons/{andonId}/close`` 三节。
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


def _seed_andons(tmp_path):
    _exec(
        tmp_path,
        "INSERT INTO trace_andon_events "
        "(andon_id, plant_code, severity, category, resource_type, resource_code, "
        " related_work_order_number, related_serial_number, message, raised_at, "
        " acknowledged_at, acknowledged_by, closed_at, closed_by) VALUES "
        "('AND-1', 'PLANT-A', 'WARNING', 'QUALITY', 'PRODUCT_UNIT', 'U-1', 'WO-1', 'U-1', 'a', '2026-08-20T01:00:00Z', NULL, NULL, NULL, NULL),"
        "('AND-2', 'PLANT-A', 'CRITICAL', 'QUALITY', 'PRODUCT_UNIT', 'U-2', 'WO-2', 'U-2', 'b', '2026-08-21T01:00:00Z', '2026-08-21T02:00:00Z', 'W-1', '2026-08-21T02:00:00Z', 'W-1'),"
        "('AND-3', 'PLANT-A', 'INFO', 'QUALITY', 'PRODUCT_UNIT', 'U-3', 'WO-3', 'U-3', 'c', '2026-08-22T01:00:00Z', NULL, NULL, NULL, NULL)",
    )


def test_andon_create_defaults(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.post(
            "/api/v1/andons",
            json={"message": "hello"},
            headers={"X-Idempotency-Key": "k-1"},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["andonId"].startswith("AND-")
    assert data["status"] == "ACTIVE"
    assert data["raisedAt"]

    rows = _query(
        tmp_path,
        "SELECT plant_code, severity, category, resource_type, resource_code, message "
        "FROM trace_andon_events",
    )
    assert len(rows) == 1
    assert rows[0][0] == "PLANT-A"
    assert rows[0][1] == "WARNING"
    assert rows[0][2] == "QUALITY"
    assert rows[0][3] == ""
    assert rows[0][4] == ""
    assert rows[0][5] == "hello"


def test_andon_create_invalid_severity_no_row(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.post(
            "/api/v1/andons",
            json={"severity": "BOGUS"},
            headers={"X-Idempotency-Key": "k-2"},
        )

    assert resp.status_code == 200
    rows = _query(tmp_path, "SELECT andon_id FROM trace_andon_events")
    assert len(rows) == 0


def test_andon_create_no_role_check(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.post(
            "/api/v1/andons",
            json={},
            headers={"X-Idempotency-Key": "k-3", "X-Actor-Role": "MES_OPERATOR"},
        )

    assert resp.status_code == 200


def test_andon_create_idempotency(tmp_path):
    with _make_client(tmp_path) as client:
        first = client.post("/api/v1/andons", json={}, headers={"X-Idempotency-Key": "k-4"})
        second = client.post("/api/v1/andons", json={}, headers={"X-Idempotency-Key": "k-4"})

    assert first.json()["data"]["andonId"] == second.json()["data"]["andonId"]
    assert second.json()["data"]["cached"] is True


def test_andon_create_validation(tmp_path):
    with _make_client(tmp_path) as client:
        bad_json = client.post(
            "/api/v1/andons", content="{bad", headers={"X-Idempotency-Key": "k-5"}
        )
        empty_key = client.post("/api/v1/andons", json={})

    assert bad_json.status_code == 422
    assert empty_key.status_code == 422
    assert empty_key.json()["error"]["code"] == "VALIDATION_ERROR"


def test_andon_list_active_default(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_andons(tmp_path)
        resp = client.get("/api/v1/andons", headers={"X-Correlation-Id": "andons-active"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["correlationId"] == "andons-active"
    assert body["data"]["meta"]["correlationId"] == "andons-active"
    items = body["data"]["data"]
    # 只含未关闭：AND-2 已关闭 → 排除；剩余按 CRITICAL 优先，同级按 raised_at 升序
    assert [i["andonId"] for i in items] == ["AND-1", "AND-3"]
    assert body["data"]["meta"]["total"] == 2
    assert body["data"]["meta"]["page"] == 1
    assert body["data"]["meta"]["pageSize"] == 100
    assert "relatedSerialNumber" not in items[0]
    assert "closedAt" not in items[0]
    assert items[0]["acknowledgedAt"] == ""
    assert items[0]["acknowledgedBy"] == ""
    assert isinstance(items[0]["durationSeconds"], int)


def test_andon_list_non_active(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_andons(tmp_path)
        resp = client.get("/api/v1/andons", params={"status": "CLOSED"})

    assert resp.status_code == 200
    items = resp.json()["data"]["data"]
    # 全部安灯按 raised_at 降序：AND-3、AND-2、AND-1
    assert [i["andonId"] for i in items] == ["AND-3", "AND-2", "AND-1"]
    assert resp.json()["data"]["meta"]["total"] == 3
    # 已关闭的 AND-2：acknowledgedAt/By 填充、duration 由关闭时间-提升时间
    and2 = next(i for i in items if i["andonId"] == "AND-2")
    assert and2["acknowledgedAt"] == "2026-08-21T02:00:00Z"
    assert and2["acknowledgedBy"] == "W-1"
    assert and2["durationSeconds"] == 3600


def test_andon_list_empty_string_status_uses_all(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_andons(tmp_path)
        resp = client.get("/api/v1/andons", params={"status": ""})

    assert resp.status_code == 200
    items = resp.json()["data"]["data"]
    assert len(items) == 3


def test_andon_close_success(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_andons(tmp_path)
        resp = client.post(
            "/api/v1/andons/AND-1/close",
            json={"note": "closed"},
            headers={"X-Idempotency-Key": "k-c1", "X-Actor-Role": "MES_SUPERVISOR", "X-Actor-Id": "S-1"},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["andonId"] == "AND-1"
    assert data["status"] == "CLOSED"
    assert data["closedAt"]

    row = _query(
        tmp_path,
        "SELECT closed_at, closed_by FROM trace_andon_events WHERE andon_id = 'AND-1'",
    )
    assert row[0][0] == data["closedAt"]
    assert row[0][1] == "S-1"


def test_andon_close_idempotency_and_forbidden(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_andons(tmp_path)
        first = client.post(
            "/api/v1/andons/AND-1/close",
            json={},
            headers={"X-Idempotency-Key": "k-c2", "X-Actor-Role": "MES_SUPERVISOR"},
        )
        second = client.post(
            "/api/v1/andons/AND-1/close",
            json={},
            headers={"X-Idempotency-Key": "k-c2", "X-Actor-Role": "MES_SUPERVISOR"},
        )
        # 角色不足时不能读取缓存结果
        forbidden = client.post(
            "/api/v1/andons/AND-1/close",
            json={},
            headers={"X-Idempotency-Key": "k-c2", "X-Actor-Role": "MES_OPERATOR"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["data"]["cached"] is True
    assert second.json()["data"]["andonId"] == "AND-1"
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"


def test_andon_close_not_found(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.post(
            "/api/v1/andons/AND-NOPE/close",
            json={},
            headers={"X-Idempotency-Key": "k-c3", "X-Actor-Role": "MES_SUPERVISOR"},
        )

    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "andon not found"


def test_andon_close_reopen_with_new_key(tmp_path):
    """C++ 不检查 closed_at：用新幂等键可再次关闭同一安灯并覆盖关闭人。"""
    with _make_client(tmp_path) as client:
        _seed_andons(tmp_path)
        first = client.post(
            "/api/v1/andons/AND-2/close",
            json={},
            headers={"X-Idempotency-Key": "k-c4", "X-Actor-Role": "MES_SUPERVISOR", "X-Actor-Id": "S-1"},
        )
        second = client.post(
            "/api/v1/andons/AND-2/close",
            json={},
            headers={"X-Idempotency-Key": "k-c5", "X-Actor-Role": "QUALITY_ENGINEER", "X-Actor-Id": "Q-2"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    row = _query(
        tmp_path,
        "SELECT closed_by FROM trace_andon_events WHERE andon_id = 'AND-2'",
    )
    assert row[0][0] == "Q-2"
