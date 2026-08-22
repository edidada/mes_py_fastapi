"""``POST /api/v1/master/workers`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/master/workers`` 一节。
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


def _post(client: TestClient, key: str, body: dict, **headers):
    return client.post(
        "/api/v1/master/workers",
        json=body,
        headers={**ADMIN, "X-Idempotency-Key": key, **headers},
    )


def test_create_worker_ok(tmp_path):
    """正常创建返回 workerId/displayName。"""
    with _make_client(tmp_path) as client:
        resp = _post(client, "w1", {"workerId": "W-001", "teamCode": "TEAM-A", "displayName": "张三"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["workerId"] == "W-001"
    assert data["displayName"] == "张三"


def test_create_worker_qualifications(tmp_path):
    """qualifications 去重写入 master_worker_qualifications，granted_by 为请求 teamCode。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    with _make_client(tmp_path) as client:
        _post(
            client,
            "w2",
            {"workerId": "W-Q1", "teamCode": "TEAM-B", "qualifications": ["Q-MILL", "Q-MILL", "Q-LATHE"]},
        )

    async def _rows():
        engine = await create_db_engine(db_url)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    sqltext("SELECT qualification, granted_by FROM master_worker_qualifications WHERE worker_id='W-Q1' ORDER BY qualification")
                )
            ).all()
        await engine.dispose()
        return rows

    rows = asyncio.new_event_loop().run_until_complete(_rows())
    quals = [r[0] for r in rows]
    assert quals == ["Q-LATHE", "Q-MILL"]
    assert all(r[1] == "TEAM-B" for r in rows)


def test_create_worker_team_required(tmp_path):
    """teamCode 空或缺失返回 409 CONFLICT。"""
    with _make_client(tmp_path) as client:
        resp = _post(client, "w3", {"workerId": "W-X", "teamCode": ""})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"

        resp = _post(client, "w4", {"workerId": "W-Y"})
        assert resp.status_code == 409


def test_create_worker_idempotent(tmp_path):
    """相同幂等键返回 workerId + cached=true。"""
    with _make_client(tmp_path) as client:
        _post(client, "w-dup", {"workerId": "W-DUP", "teamCode": "TEAM-A"})
        resp = _post(client, "w-dup", {"workerId": "W-DUP", "teamCode": "TEAM-A"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["workerId"] == "W-DUP"
    assert data["cached"] is True


def test_create_worker_forbidden(tmp_path):
    """非 MES_ADMIN 返回 403。"""
    with _make_client(tmp_path) as client:
        resp = client.post(
            "/api/v1/master/workers",
            json={"workerId": "W-X", "teamCode": "TEAM-A"},
            headers={"X-Actor-Role": "MES_OPERATOR", "X-Idempotency-Key": "w5"},
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_create_worker_validation(tmp_path):
    """缺少幂等键或 workerId 返回 422。"""
    with _make_client(tmp_path) as client:
        resp = client.post("/api/v1/master/workers", json={"workerId": "W-X", "teamCode": "TEAM-A"}, headers=ADMIN)
        assert resp.status_code == 422

        resp = _post(client, "w6", {"teamCode": "TEAM-A"})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_worker_conflict(tmp_path):
    """重复 workerId 返回 409。"""
    with _make_client(tmp_path) as client:
        _post(client, "w7", {"workerId": "W-C", "teamCode": "TEAM-A"})
        resp = _post(client, "w8", {"workerId": "W-C", "teamCode": "TEAM-A"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"
