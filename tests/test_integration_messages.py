"""``GET /api/v1/integration/messages`` 接口契约测试。

契约依据：docs/http-api.md 中 ``GET /api/v1/integration/messages`` 一节。
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
        "INSERT INTO integration_inbox_messages "
        "(message_id, source_system, message_type, status, retry_count, payload, "
        " received_at, processed_at, error_message) VALUES "
        "('E-1', 'ERP', 'erp.plan.pushed', 'PROCESSED', 0, '{}', '2026-08-22T01:00:00Z', '2026-08-22T01:00:01Z', NULL),"
        "('E-2', 'WMS', 'wms.inventory.adjusted', 'FAILED', 2, '{}', '2026-08-22T02:00:00Z', NULL, 'no effective routing'),"
        "('E-3', 'MES', 'thing.happened', 'PROCESSED', 1, '{}', '2026-08-21T00:00:00Z', '2026-08-21T00:00:05Z', NULL)",
    )


def test_messages_list_double_envelope(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = client.get(
            "/api/v1/integration/messages",
            headers={"X-Actor-Role": "MES_ADMIN", "X-Correlation-Id": "msg-1"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["correlationId"] == "msg-1"
    inner = body["data"]
    assert inner["meta"]["correlationId"] == "msg-1"
    assert inner["meta"]["page"] == 1
    assert inner["meta"]["pageSize"] == 100
    assert inner["meta"]["total"] == 3

    items = inner["data"]
    # received_at 倒序
    assert [i["messageId"] for i in items] == ["E-2", "E-1", "E-3"]
    assert items[0]["sourceSystem"] == "WMS"
    assert items[0]["status"] == "FAILED"
    assert items[0]["retryCount"] == 2
    assert items[0]["processedAt"] == ""
    assert items[0]["errorMessage"] == "no effective routing"
    assert items[1]["processedAt"] == "2026-08-22T01:00:01Z"
    assert items[1]["errorMessage"] == ""


def test_messages_status_filter(tmp_path):
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = client.get(
            "/api/v1/integration/messages",
            params={"status": "FAILED"},
            headers={"X-Actor-Role": "MES_ADMIN"},
        )

    assert resp.status_code == 200
    items = resp.json()["data"]["data"]
    assert [i["messageId"] for i in items] == ["E-2"]
    assert resp.json()["data"]["meta"]["total"] == 1


def test_messages_empty_status_filter(tmp_path):
    """参数存在但值为空 → 匹配空状态（无行）。"""
    with _make_client(tmp_path) as client:
        _seed(tmp_path)
        resp = client.get(
            "/api/v1/integration/messages",
            params={"status": ""},
            headers={"X-Actor-Role": "MES_ADMIN"},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["data"] == []
    assert resp.json()["data"]["meta"]["total"] == 0


def test_messages_forbidden(tmp_path):
    with _make_client(tmp_path) as client:
        default_role = client.get("/api/v1/integration/messages")
        operator = client.get(
            "/api/v1/integration/messages", headers={"X-Actor-Role": "MES_OPERATOR"}
        )
        alt_header = client.get(
            "/api/v1/integration/messages", headers={"X-Role": "MES_ADMIN"}
        )

    assert default_role.status_code == 403
    assert default_role.json()["error"]["message"] == "MES_ADMIN required"
    assert operator.status_code == 403
    assert alt_header.status_code == 200
