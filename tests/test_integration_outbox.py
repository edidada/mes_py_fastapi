"""``GET /api/v1/integration/outbox`` 接口契约测试。

契约依据：docs/http-api.md 中 ``GET /api/v1/integration/outbox`` 一节。
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


def _seed(tmp_path):
    _exec(
        tmp_path,
        "INSERT INTO integration_outbox_messages "
        "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, status, "
        " payload, created_at, published_at) VALUES "
        "('OUT-1', 'WORK_ORDER', 'WO-1', 'work_order.completion', 'ERP', 'PUBLISHED', '{}', '2026-08-22T01:00:00Z', '2026-08-22T01:00:05Z'),"
        "('OUT-2', 'WORK_ORDER', 'WO-2', 'work_order.scrap', 'ERP', 'FAILED', '{}', '2026-08-22T02:00:00Z', NULL),"
        "('OUT-3', 'WORK_ORDER', 'WO-3', 'work_order.state.changed', 'ERP', 'PENDING', '{}', '2026-08-21T00:00:00Z', NULL)",
    )
    _exec(
        tmp_path,
        "INSERT INTO integration_outbox_delivery_attempts (outbox_id, attempted_at) VALUES "
        "('OUT-2', '2026-08-22T02:00:01Z'),"
        "('OUT-2', '2026-08-22T02:00:02Z'),"
        "('OUT-3', '2026-08-21T00:00:01Z')",
    )
    _exec(
        tmp_path,
        "INSERT INTO integration_outbox_dead_letters "
        "(outbox_id, retry_count, last_error, failed_at, replayed_at) VALUES "
        "('OUT-2', 7, 'timeout', '2026-08-22T02:00:03Z', NULL)",
    )


def test_outbox_list_aggregation(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = client.get(
            "/api/v1/integration/outbox",
            headers={"X-Actor-Role": "MES_ADMIN", "X-Correlation-Id": "ob-1"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["correlationId"] == "ob-1"
    inner = body["data"]
    assert inner["meta"]["page"] == 1
    assert inner["meta"]["pageSize"] == 100
    assert inner["meta"]["total"] == 3

    items = inner["data"]
    # created_at 倒序
    assert [i["outboxId"] for i in items] == ["OUT-2", "OUT-1", "OUT-3"]

    out2 = items[0]
    # retryCount 来自尝试表行数（2），不是死信表 retry_count（7）
    assert out2["retryCount"] == 2
    assert out2["lastAttemptAt"] == "2026-08-22T02:00:02Z"
    assert out2["lastError"] == "timeout"
    assert out2["failedAt"] == "2026-08-22T02:00:03Z"
    assert out2["replayedAt"] == ""
    assert out2["publishedAt"] == ""

    out1 = items[1]
    assert out1["status"] == "PUBLISHED"
    assert out1["publishedAt"] == "2026-08-22T01:00:05Z"
    assert out1["retryCount"] == 0
    assert out1["lastAttemptAt"] == ""
    assert out1["lastError"] == ""
    assert out1["failedAt"] == ""
    assert out1["replayedAt"] == ""


def test_outbox_status_filter(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = client.get(
            "/api/v1/integration/outbox",
            params={"status": "FAILED"},
            headers={"X-Actor-Role": "MES_ADMIN"},
        )
        empty = client.get(
            "/api/v1/integration/outbox",
            params={"status": ""},
            headers={"X-Actor-Role": "MES_ADMIN"},
        )

    assert resp.status_code == 200
    items = resp.json()["data"]["data"]
    assert [i["outboxId"] for i in items] == ["OUT-2"]
    assert resp.json()["data"]["meta"]["total"] == 1
    assert empty.status_code == 200
    assert empty.json()["data"]["data"] == []


def test_outbox_forbidden(tmp_path):
    with _make_client(tmp_path) as client:
        default_role = client.get("/api/v1/integration/outbox")
        operator = client.get(
            "/api/v1/integration/outbox", headers={"X-Actor-Role": "QUALITY_ENGINEER"}
        )
        alt_header = client.get(
            "/api/v1/integration/outbox", headers={"X-Role": "MES_ADMIN"}
        )

    assert default_role.status_code == 403
    assert default_role.json()["error"]["message"] == "MES_ADMIN required"
    assert operator.status_code == 403
    assert alt_header.status_code == 200
