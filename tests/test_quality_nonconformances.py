"""``POST /api/v1/quality/nonconformances`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/quality/nonconformances`` 一节。
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
    """创建有效源检验 INS-1 与产品单元 SN-1。"""
    _exec(
        tmp_path,
        "INSERT INTO quality_inspection_results "
        "(inspection_id, plant_code, serial_number, work_order_number, disposition, inspector_id, inspected_at) "
        "VALUES ('INS-1', 'PLANT-A', 'SN-1', 'WO-A', 'REJECTED', 'Q-1', '2026-08-22T10:00:00Z')",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_product_units "
        "(serial_number, work_order_number, status, current_operation_sequence) "
        "VALUES ('SN-1', 'WO-A', 'IN_PROCESS', 10)",
    )


def _create(client: TestClient, key: str, role: str = "QUALITY_ENGINEER", **overrides):
    body = {
        "plantCode": "PLANT-A",
        "sourceInspectionId": "INS-1",
        "severity": "MAJOR",
        "defectCode": "DEF-CRACK",
        "disposition": "OPEN",
        "affectedQuantity": 2.5,
        "resourceCode": "SN-1",
    }
    body.update(overrides)
    headers = {"X-Idempotency-Key": key, "X-Actor-Role": role, "X-Actor-Id": "Q-1"}
    return client.post("/api/v1/quality/nonconformances", json=body, headers=headers)


def test_nc_ok(tmp_path):
    """创建成功：NC/隔离记录/产品 HOLD/安灯/审计全部落库。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _create(client, "k1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["nonconformanceId"].startswith("NC-")
        assert data["status"] == "OPEN"
        assert "cached" not in data

        nc = _query(
            tmp_path,
            "SELECT source_inspection_id, plant_code, severity, status, defect_code, affected_qty "
            "FROM quality_nonconformances",
        )
        assert len(nc) == 1
        assert nc[0][0] == "INS-1"
        assert nc[0][1] == "PLANT-A"
        assert nc[0][2] == "MAJOR"
        assert nc[0][3] == "OPEN"
        assert nc[0][4] == "DEF-CRACK"
        assert nc[0][5] == 2.5

        qrt = _query(tmp_path, "SELECT nonconference_id, resource_type, resource_code FROM quality_quarantine_records")
        assert len(qrt) == 1
        assert qrt[0][1] == "SERIAL_NUMBER"
        assert qrt[0][2] == "SN-1"

        unit = _query(tmp_path, "SELECT status FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "HOLD"

        andon = _query(tmp_path, "SELECT severity, category, resource_type, resource_code, message FROM trace_andon_events")
        assert len(andon) == 1
        assert andon[0][0] == "MAJOR"
        assert andon[0][1] == "QUALITY"
        assert andon[0][2] == "NONCONFORMANCE"
        assert andon[0][3].startswith("NC-")
        assert andon[0][4] == "nonconformance created"

        audit = _query(tmp_path, "SELECT action, resource_type FROM audit_events")
        assert audit[0][0] == "NC_CREATE"
        assert audit[0][1] == "NONCONFORMANCE"


def test_nc_missing_source_inspection(tmp_path):
    """源检验缺失：NC/隔离失败，但产品 HOLD + 安灯仍产生，响应成功。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _create(client, "k1", sourceInspectionId="INS-NOPE")
        assert resp.status_code == 200
        assert resp.json()["data"]["nonconformanceId"].startswith("NC-")

        nc = _query(tmp_path, "SELECT count(*) FROM quality_nonconformances")
        assert nc[0][0] == 0
        qrt = _query(tmp_path, "SELECT count(*) FROM quality_quarantine_records")
        assert qrt[0][0] == 0

        unit = _query(tmp_path, "SELECT status FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "HOLD"

        andon = _query(tmp_path, "SELECT count(*) FROM trace_andon_events")
        assert andon[0][0] == 1

        audit = _query(tmp_path, "SELECT count(*) FROM audit_events")
        assert audit[0][0] == 1


def test_nc_defaults(tmp_path):
    """缺省：NC 严重度 MAJOR、安灯 WARNING、数量 1、处置 OPEN、工厂 PLANT-A。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _create(
            client,
            "k1",
            sourceInspectionId=None,
            severity=None,
            affectedQuantity=None,
            disposition=None,
            plantCode=None,
        )
        assert resp.status_code == 200

        nc = _query(tmp_path, "SELECT plant_code, severity, status, affected_qty FROM quality_nonconformances")
        assert nc[0][0] == "PLANT-A"
        assert nc[0][1] == "MAJOR"
        assert nc[0][2] == "OPEN"
        assert nc[0][3] == 1.0

        andon = _query(tmp_path, "SELECT severity FROM trace_andon_events")
        assert andon[0][0] == "WARNING"


def test_nc_affected_quantity_string(tmp_path):
    """影响数量为字符串 → 1.0。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _create(client, "k1", affectedQuantity="5")
        assert resp.status_code == 200
        qty = _query(tmp_path, "SELECT affected_qty FROM quality_nonconformances")
        assert qty[0][0] == 1.0


def test_nc_without_resource_code(tmp_path):
    """无资源代码：不置 HOLD、隔离记录资源代码为空。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _create(client, "k1", resourceCode=None)
        assert resp.status_code == 200
        unit = _query(tmp_path, "SELECT status FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "IN_PROCESS"
        qrt = _query(tmp_path, "SELECT resource_code FROM quality_quarantine_records")
        assert qrt[0][0] == ""


def test_nc_idempotent(tmp_path):
    """幂等重放：cached=true，返回首次 NC ID，不重复落库。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        first = _create(client, "k1")
        replay = _create(client, "k1", defectCode="OTHER")
        assert replay.status_code == 200
        data = replay.json()["data"]
        assert data["nonconformanceId"] == first.json()["data"]["nonconformanceId"]
        assert data["cached"] is True
        assert "status" not in data

        nc = _query(tmp_path, "SELECT count(*) FROM quality_nonconformances")
        assert nc[0][0] == 1


def test_nc_role_check(tmp_path):
    """非合格角色 → 403。"""
    with _make_client(tmp_path) as client:
        resp = _create(client, "k1", role="MES_OPERATOR")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_nc_validation(tmp_path):
    """缺幂等键 → 422。"""
    with _make_client(tmp_path) as client:
        resp = client.post(
            "/api/v1/quality/nonconformances",
            json={"defectCode": "X"},
            headers={"X-Actor-Role": "QUALITY_ENGINEER"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
