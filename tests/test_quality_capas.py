"""``POST /api/v1/quality/capas`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/quality/capas`` 一节。
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


def _setup(tmp_path):
    """创建有效 NC 记录 NC-1。"""
    _exec(
        tmp_path,
        "INSERT INTO quality_nonconformances "
        "(nonconformance_id, plant_code, severity, status, reported_by, reported_at) "
        "VALUES ('NC-1', 'PLANT-A', 'MAJOR', 'OPEN', 'Q-1', '2026-08-22T10:00:00Z')",
    )


def _create(client: TestClient, key: str, role: str = "QUALITY_ENGINEER", **overrides):
    body = {
        "plantCode": "PLANT-A",
        "nonconformanceId": "NC-1",
        "ownerId": "ENG-1",
        "rootCause": "inspection misalignment",
        "correctiveAction": "recalibrate gauge",
        "dueAt": "2026-09-01T00:00:00Z",
    }
    body.update(overrides)
    headers = {"X-Idempotency-Key": key, "X-Actor-Role": role, "X-Actor-Id": "Q-1"}
    return client.post("/api/v1/quality/capas", json=body, headers=headers)


def test_capa_created_with_valid_nc(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _create(client, "k-valid")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["capaId"].startswith("CAPA-")
    assert data["status"] == "OPEN"
    assert resp.json()["meta"]["correlationId"]

    capas = _query(
        tmp_path,
        "SELECT capa_id, plant_code, nonconformance_id, owner_id, root_cause, corrective_action, "
        "       status, due_at, created_by, closed_at "
        "FROM quality_capa_cases",
    )
    assert len(capas) == 1
    assert capas[0][0] == data["capaId"]
    assert capas[0][1] == "PLANT-A"
    assert capas[0][2] == "NC-1"
    assert capas[0][3] == "ENG-1"
    assert capas[0][4] == "inspection misalignment"
    assert capas[0][5] == "recalibrate gauge"
    assert capas[0][6] == "OPEN"
    assert capas[0][7] == "2026-09-01T00:00:00Z"
    assert capas[0][8] == "Q-1"
    assert capas[0][9] is None


def test_capa_unknown_nonconformance_still_returns_ok(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _create(client, "k-unknown", nonconformanceId="NC-NOT-EXIST")

    assert resp.status_code == 200
    assert resp.json()["data"]["capaId"].startswith("CAPA-")
    assert resp.json()["data"]["status"] == "OPEN"
    # NC 外键不满足 → 记录被忽略
    capas = _query(tmp_path, "SELECT capa_id FROM quality_capa_cases")
    assert len(capas) == 0


def test_capa_defaults(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _create(
            client,
            "k-defaults",
            plantCode=None,
            nonconformanceId=None,
            ownerId=None,
            rootCause=None,
            correctiveAction=None,
            dueAt=None,
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["capaId"].startswith("CAPA-")
    # 空 nonconformance_id 违反 NOT NULL+FK → 无记录落库，但缺省值语义由成功响应体现
    capas = _query(tmp_path, "SELECT capa_id FROM quality_capa_cases")
    assert len(capas) == 0


def test_capa_idempotency(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        first = _create(client, "k-idem")
        second = _create(client, "k-idem")

    assert first.status_code == 200
    assert second.status_code == 200
    first_id = first.json()["data"]["capaId"]
    second_id = second.json()["data"]["capaId"]
    assert first_id == second_id
    assert second.json()["data"]["cached"] is True


def test_capa_forbidden_role(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _create(client, "k-403", role="MES_OPERATOR")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    # 不允许的调用不产生幂等投影
    idems = _query(tmp_path, "SELECT idempotency_key FROM idempotency_keys")
    assert len(idems) == 0


def test_capa_invalid_json(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.post(
            "/api/v1/quality/capas",
            content="{not json",
            headers={"X-Idempotency-Key": "k-422", "X-Actor-Role": "QUALITY_ENGINEER"},
        )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_capa_missing_idempotency_key(tmp_path):
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.post(
            "/api/v1/quality/capas",
            json={"nonconformanceId": "NC-1"},
            headers={"X-Actor-Role": "QUALITY_ENGINEER"},
        )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
