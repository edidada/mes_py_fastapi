"""``POST /api/v1/stations/{stationCode}/sessions`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/stations/{stationCode}/sessions`` 一节。
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


def _setup(client: TestClient, tmp_path) -> str:
    """创建站点 ST-1，返回站点代码。"""
    _exec(
        tmp_path,
        "INSERT INTO master_stations (station_code, station_name, plant_code, work_center_code, active) "
        "VALUES ('ST-1', 'Station 1', 'PLANT-A', 'WC-1', 1)",
    )
    return "ST-1"


def _login(client: TestClient, key: str, station: str = "ST-1", **overrides):
    body = {"userId": "U-1"}
    body.update(overrides)
    return client.post(
        f"/api/v1/stations/{station}/sessions",
        json=body,
        headers={"X-Idempotency-Key": key},
    )


def _logout(client: TestClient, key: str, station: str = "ST-1", **overrides):
    body = {"action": "LOGOUT", "userId": "U-1"}
    body.update(overrides)
    return client.post(
        f"/api/v1/stations/{station}/sessions",
        json=body,
        headers={"X-Idempotency-Key": key},
    )


def test_session_login_ok(tmp_path):
    """登录成功：返回 SES-... 会话 ID 与 ACTIVE，落库并写审计。"""
    with _make_client(tmp_path) as client:
        station = _setup(client, tmp_path)
        resp = _login(client, "s1", station)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["sessionId"].startswith("SES-")
        assert data["status"] == "ACTIVE"
        assert "cached" not in data

        rows = _query(
            tmp_path,
            "SELECT station_code, user_id, status, shift_code, logged_out_at "
            "FROM production_station_sessions",
        )
        assert len(rows) == 1
        assert rows[0][0] == station
        assert rows[0][1] == "U-1"
        assert rows[0][2] == "ACTIVE"
        assert rows[0][4] is None

        audit = _query(tmp_path, "SELECT action, resource_type, resource_id FROM audit_events WHERE action='STATION_LOGIN'")
        assert len(audit) == 1
        assert audit[0][1] == "STATION"
        assert audit[0][2] == station


def test_session_login_closes_old_active(tmp_path):
    """同站点同用户再次登录会关闭旧 ACTIVE 会话并填写 logged_out_at。"""
    with _make_client(tmp_path) as client:
        station = _setup(client, tmp_path)
        first = _login(client, "s1", station)
        second = _login(client, "s2", station)
        assert first.status_code == 200
        assert second.status_code == 200

        rows = _query(
            tmp_path,
            "SELECT status, logged_out_at FROM production_station_sessions "
            "WHERE session_id=:id",
            id=first.json()["data"]["sessionId"],
        )
        assert rows[0][0] == "CLOSED"
        assert rows[0][1] is not None


def test_session_login_defaults(tmp_path):
    """action 省略/非字符串、userId 省略均按默认值登录。"""
    with _make_client(tmp_path) as client:
        _setup(client, tmp_path)
        resp = _login(client, "s1", **{"action": None, "userId": None})
        assert resp.status_code == 200
        rows = _query(tmp_path, "SELECT user_id, status FROM production_station_sessions")
        assert rows[0][0] == ""
        assert rows[0][1] == "ACTIVE"


def test_session_logout_ok(tmp_path):
    """退出：返回 status=CLOSED，关闭匹配 ACTIVE 会话，不写审计。"""
    with _make_client(tmp_path) as client:
        _setup(client, tmp_path)
        assert _login(client, "s1").status_code == 200
        resp = _logout(client, "s2")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "CLOSED"
        assert "sessionId" not in data

        rows = _query(tmp_path, "SELECT status, logged_out_at FROM production_station_sessions")
        assert rows[0][0] == "CLOSED"
        assert rows[0][1] is not None

        audit = _query(tmp_path, "SELECT count(*) FROM audit_events")
        assert audit[0][0] == 1  # 仅登录审计，退出不写


def test_session_logout_no_match_ok(tmp_path):
    """没有匹配的 ACTIVE 会话时退出仍成功。"""
    with _make_client(tmp_path) as client:
        _setup(client, tmp_path)
        resp = _logout(client, "s1", station="ST-UNKNOWN")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "CLOSED"


def test_session_idempotent_login(tmp_path):
    """幂等重放登录：200、cached=true，返回首次会话 ID。"""
    with _make_client(tmp_path) as client:
        _setup(client, tmp_path)
        first = _login(client, "s1")
        replay = _login(client, "s1", userId="U-OTHER")
        assert replay.status_code == 200
        data = replay.json()["data"]
        assert data["sessionId"] == first.json()["data"]["sessionId"]
        assert data["cached"] is True
        assert "status" not in data


def test_session_idempotent_logout(tmp_path):
    """幂等重放退出：200、cached=true、sessionId 为空字符串，无 status。"""
    with _make_client(tmp_path) as client:
        _setup(client, tmp_path)
        assert _login(client, "s1").status_code == 200
        first = _logout(client, "s2")
        assert first.status_code == 200
        replay = _logout(client, "s2")
        assert replay.status_code == 200
        data = replay.json()["data"]
        assert data["sessionId"] == ""
        assert data["cached"] is True
        assert "status" not in data


def test_session_unknown_station_login_422(tmp_path):
    """登录到不存在的站点：插入违反外键 → 422。"""
    with _make_client(tmp_path) as client:
        resp = _login(client, "s1", station="ST-NOPE")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_session_validation(tmp_path):
    """缺幂等键/非法 JSON → 422。"""
    with _make_client(tmp_path) as client:
        station = _setup(client, tmp_path)
        resp = client.post(
            f"/api/v1/stations/{station}/sessions",
            json={"userId": "U-1"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

        resp = client.post(
            f"/api/v1/stations/{station}/sessions",
            content="{bad",
            headers={"X-Idempotency-Key": "s9", "Content-Type": "application/json"},
        )
        assert resp.status_code == 422
