"""``POST /api/v1/integration/outbox/{id}/replay`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/integration/outbox/{id}/replay``。
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


def _seed_failed(tmp_path, outbox_id="OUT-1"):
    _exec(
        tmp_path,
        "INSERT INTO integration_outbox_messages "
        "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, status, "
        " payload, created_at, published_at) VALUES "
        f"('{outbox_id}', 'WORK_ORDER', 'WO-1', 'work_order.scrap', 'ERP', 'FAILED', '{{}}', '2026-08-22T01:00:00Z', NULL)",
    )
    _exec(
        tmp_path,
        "INSERT INTO integration_outbox_dead_letters "
        f"(outbox_id, retry_count, last_error, failed_at, replayed_at) VALUES "
        f"('{outbox_id}', 3, 'timeout', '2026-08-22T01:00:01Z', NULL)",
    )


def _post(client: TestClient, outbox_id: str, key: str, role: str = "MES_ADMIN"):
    return client.post(
        f"/api/v1/integration/outbox/{outbox_id}/replay",
        json={},
        headers={"X-Idempotency-Key": key, "X-Actor-Role": role, "X-Correlation-Id": "rp-1"},
    )


def test_replay_success(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_failed(tmp_path)
        resp = _post(client, "OUT-1", "k-1")

    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data == {"outboxId": "OUT-1", "status": "PENDING"}
    assert resp.json()["meta"]["correlationId"] == "rp-1"

    msg = _query(
        tmp_path,
        "SELECT status, published_at FROM integration_outbox_messages WHERE outbox_id = 'OUT-1'",
    )
    assert msg[0][0] == "PENDING"
    assert msg[0][1] is None

    dl = _query(
        tmp_path,
        "SELECT replayed_at FROM integration_outbox_dead_letters WHERE outbox_id = 'OUT-1'",
    )
    assert dl[0][0]  # replayed_at 已填


def test_replay_idempotency_returns_200(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_failed(tmp_path)
        first = _post(client, "OUT-1", "k-2")
        second = _post(client, "OUT-1", "k-2")

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["data"] == {"outboxId": "OUT-1", "status": "PENDING", "cached": True}


def test_replay_role_before_idempotency(tmp_path):
    """角色校验先于幂等：用已成功键+无权限仍 403。"""
    with _make_client(tmp_path) as client:
        _seed_failed(tmp_path)
        _post(client, "OUT-1", "k-3")
        forbidden = _post(client, "OUT-1", "k-3", role="MES_OPERATOR")

    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["message"] == "MES_ADMIN required"


def test_replay_not_found(tmp_path):
    with _make_client(tmp_path) as client:
        resp = _post(client, "OUT-NOPE", "k-4")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert resp.json()["error"]["message"] == "outbox message not found"


def test_replay_invalid_state(tmp_path):
    with _make_client(tmp_path) as client:
        _exec(
            tmp_path,
            "INSERT INTO integration_outbox_messages "
            "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, status, "
            " payload, created_at, published_at) VALUES "
            "('OUT-PUB', 'WORK_ORDER', 'WO-2', 'work_order.completion', 'ERP', 'PUBLISHED', '{}', '2026-08-22T02:00:00Z', '2026-08-22T02:00:01Z')",
        )
        resp = _post(client, "OUT-PUB", "k-5")

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "INVALID_STATE"
    assert resp.json()["error"]["message"] == "only FAILED messages may be replayed"


def test_replay_no_dead_letter_still_success(tmp_path):
    """无死信行：UPDATE 影响 0 行但不视为错误。"""
    with _make_client(tmp_path) as client:
        _exec(
            tmp_path,
            "INSERT INTO integration_outbox_messages "
            "(outbox_id, aggregate_type, aggregate_id, event_type, target_system, status, "
            " payload, created_at, published_at) VALUES "
            "('OUT-NODL', 'WORK_ORDER', 'WO-3', 'work_order.scrap', 'ERP', 'FAILED', '{}', '2026-08-22T03:00:00Z', NULL)",
        )
        resp = _post(client, "OUT-NODL", "k-6")

    assert resp.status_code == 202
    assert resp.json()["data"] == {"outboxId": "OUT-NODL", "status": "PENDING"}


def test_replay_validation(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_failed(tmp_path)
        empty_key = client.post(
            "/api/v1/integration/outbox/OUT-1/replay",
            json={},
            headers={"X-Actor-Role": "MES_ADMIN"},
        )

    assert empty_key.status_code == 422
    assert empty_key.json()["error"]["code"] == "VALIDATION_ERROR"
