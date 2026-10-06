"""``POST /api/v1/quality/inspections`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/quality/inspections`` 一节。
"""

import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text as sqltext

from app.config import Settings
from app.database import create_db_engine
from app.main import create_app


def _utc_today() -> str:
    """当前 UTC 日期（YYYY-MM-DD）。

    应用端报表汇总按 ``utc_now()[:10]``（真实当天 UTC）更新 ``shift_date``，
    因此测试种子必须使用同一个当天日期，否则 UPDATE 匹配不到行、计数保持初始值。
    写死日期会让测试只在那一天通过。
    """

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


def _setup(tmp_path, unit_status="WAITING_INSPECTION", with_next_op=True, today=None):
    today = today or _utc_today()
    _exec(
        tmp_path,
        "INSERT INTO production_work_orders "
        "(work_order_number, material_code, plant_code, planned_quantity, status, completed_quantity) "
        "VALUES ('WO-A', 'MAT-A', 'PLANT-A', 100, 'RELEASED', 0)",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_work_order_operations "
        "(work_order_number, sequence, operation_code, work_center_code, status) "
        "VALUES ('WO-A', 10, 'OP-10', 'WC-1', 'COMPLETED')",
    )
    if with_next_op:
        _exec(
            tmp_path,
            "INSERT INTO production_work_order_operations "
            "(work_order_number, sequence, operation_code, work_center_code, status) "
            "VALUES ('WO-A', 20, 'OP-20', 'WC-2', 'PENDING')",
        )
    _exec(
        tmp_path,
        "INSERT INTO production_product_units "
        "(serial_number, work_order_number, status, current_operation_sequence) "
        "VALUES ('SN-1', 'WO-A', :s, 10)",
        s=unit_status,
    )
    _exec(
        tmp_path,
        "INSERT INTO quality_inspection_lots "
        "(lot_id, plant_code, material_code, status, operator_id, created_at) "
        "VALUES ('ILOT-1', 'PLANT-A', 'MAT-A', 'PENDING', 'Q-1', '2026-08-22T10:00:00Z')",
    )
    _exec(
        tmp_path,
        "INSERT INTO reporting_quality_shift_summary "
        "(plant_code, shift_date, shift_code, inspection_count, accepted_count, rejected_count, hold_count) "
        "VALUES ('PLANT-A', :d, 'A', 0, 0, 0, 0)",
        d=today,
    )


def _inspect(client: TestClient, key: str, role: str = "QUALITY_ENGINEER", **overrides):
    body = {
        "serialNumber": "SN-1",
        "disposition": "ACCEPTED",
        "workOrderNumber": "WO-A",
        "inspectionLotId": "ILOT-1",
        "inspectionPlanCode": "IP-01",
        "operationSequence": 10,
        "defectCode": "",
    }
    body.update(overrides)
    headers = {"X-Idempotency-Key": key, "X-Actor-Role": role, "X-Actor-Id": "Q-1"}
    return client.post("/api/v1/quality/inspections", json=body, headers=headers)


def test_inspect_accepted_in_process(tmp_path):
    """ACCEPTED + 下一工序 → IN_PROCESS：结果/批状态/产品前移/outbox/报表落库。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _inspect(client, "k1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["inspectionId"].startswith("INS-")
        assert data["lotStatus"] == "ACCEPTED"
        assert data["unitDisposition"] == "IN_PROCESS"
        assert "cached" not in data

        results = _query(tmp_path, "SELECT disposition, plant_code, inspector_id FROM quality_inspection_results")
        assert len(results) == 1
        assert results[0][0] == "ACCEPTED"
        assert results[0][1] == "PLANT-A"
        assert results[0][2] == "Q-1"

        lot = _query(tmp_path, "SELECT status FROM quality_inspection_lots WHERE lot_id='ILOT-1'")
        assert lot[0][0] == "ACCEPTED"

        unit = _query(tmp_path, "SELECT status, current_operation_sequence FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "IN_PROCESS"
        assert unit[0][1] == 20

        outbox = _query(tmp_path, "SELECT event_type, aggregate_id FROM integration_outbox_messages")
        assert len(outbox) == 1
        assert outbox[0][0] == "work_order.completion"
        assert outbox[0][1] == "WO-A"

        summary = _query(tmp_path, "SELECT inspection_count, accepted_count, rejected_count, hold_count FROM reporting_quality_shift_summary")
        assert summary[0][0] == 1
        assert summary[0][1] == 1
        assert summary[0][2] == 0
        assert summary[0][3] == 0

        trace = _query(tmp_path, "SELECT event_type, resource_type, related_work_order_number FROM trace_events")
        assert trace[0][0] == "INSPECTION"
        assert trace[0][1] == "SERIAL_NUMBER"
        assert trace[0][2] == "WO-A"

        audit = _query(tmp_path, "SELECT action, resource_type FROM audit_events")
        assert audit[0][0] == "INSPECTION_SUBMIT"
        assert audit[0][1] == "INSPECTION"


def test_inspect_accepted_passed_last_op(tmp_path):
    """ACCEPTED 末工序 → PASSED：工单完成数量 +1、全部工序完成则工单 COMPLETED。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path, with_next_op=False)
        resp = _inspect(client, "k1")
        assert resp.status_code == 200
        assert resp.json()["data"]["unitDisposition"] == "PASSED"

        unit = _query(tmp_path, "SELECT status FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "PASSED"

        wo = _query(tmp_path, "SELECT status, completed_quantity FROM production_work_orders WHERE work_order_number='WO-A'")
        assert wo[0][0] == "COMPLETED"
        assert wo[0][1] == 1


def test_inspect_accepted_not_waiting(tmp_path):
    """ACCEPTED 但产品不是 WAITING_INSPECTION → 保持当前状态，不改产品、无 outbox。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path, unit_status="IN_PROCESS")
        resp = _inspect(client, "k1")
        assert resp.status_code == 200
        assert resp.json()["data"]["unitDisposition"] == "IN_PROCESS"

        unit = _query(tmp_path, "SELECT status, current_operation_sequence FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "IN_PROCESS"
        assert unit[0][1] == 10

        outbox = _query(tmp_path, "SELECT count(*) FROM integration_outbox_messages")
        assert outbox[0][0] == 0


def test_inspect_accepted_unknown_unit(tmp_path):
    """ACCEPTED 产品不存在 → unitDisposition 为空字符串，无 outbox。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _inspect(client, "k1", serialNumber="SN-NOPE")
        assert resp.status_code == 200
        assert resp.json()["data"]["unitDisposition"] == ""


def test_inspect_rejected(tmp_path):
    """REJECTED → HOLD：不合格项/产品 HOLD/安灯/报表。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _inspect(client, "k1", disposition="REJECTED", defectCode="SCRATCH")
        assert resp.status_code == 200
        assert resp.json()["data"]["unitDisposition"] == "HOLD"

        nc = _query(
            tmp_path,
            "SELECT severity, status, affected_qty, defect_code FROM quality_nonconformances",
        )
        assert len(nc) == 1
        assert nc[0][0] == "MAJOR"
        assert nc[0][1] == "OPEN"
        assert nc[0][2] == 1
        assert nc[0][3] == "SCRATCH"

        unit = _query(tmp_path, "SELECT status FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "HOLD"

        andons = _query(tmp_path, "SELECT severity, category FROM trace_andon_events")
        assert andons[0][0] == "CRITICAL"
        assert andons[0][1] == "QUALITY"

        summary = _query(tmp_path, "SELECT inspection_count, rejected_count FROM reporting_quality_shift_summary")
        assert summary[0][0] == 1
        assert summary[0][1] == 1


def test_inspect_rejected_default_defect(tmp_path):
    """REJECTED 缺缺陷码 → 使用 DEF-UNKNOWN。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _inspect(client, "k1", disposition="REJECTED", defectCode=None)
        assert resp.status_code == 200
        nc = _query(tmp_path, "SELECT defect_code FROM quality_nonconformances")
        assert nc[0][0] == "DEF-UNKNOWN"


def test_inspect_other_disposition(tmp_path):
    """其他处置（PENDING）→ unitDisposition=请求值，不改产品。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _inspect(client, "k1", disposition="PENDING")
        assert resp.status_code == 200
        assert resp.json()["data"]["unitDisposition"] == "PENDING"

        unit = _query(tmp_path, "SELECT status FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "WAITING_INSPECTION"


def test_inspect_empty_body(tmp_path):
    """空 Body：结果因外键未落库，但报表/追溯/审计/幂等与成功响应仍产生。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.post(
            "/api/v1/quality/inspections",
            content=b"",
            headers={"X-Idempotency-Key": "k1", "X-Actor-Role": "QUALITY_ENGINEER"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["inspectionId"].startswith("INS-")
        assert data["unitDisposition"] == ""

        results = _query(tmp_path, "SELECT count(*) FROM quality_inspection_results")
        assert results[0][0] == 0

        summary = _query(tmp_path, "SELECT inspection_count FROM reporting_quality_shift_summary")
        assert summary[0][0] == 1

        trace = _query(tmp_path, "SELECT count(*) FROM trace_events")
        assert trace[0][0] == 1
        audit = _query(tmp_path, "SELECT count(*) FROM audit_events")
        assert audit[0][0] == 1


def test_inspect_idempotent(tmp_path):
    """幂等重放：cached=true，返回首次检验 ID，不重复落库。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        first = _inspect(client, "k1")
        replay = _inspect(client, "k1", disposition="REJECTED")
        assert replay.status_code == 200
        data = replay.json()["data"]
        assert data["inspectionId"] == first.json()["data"]["inspectionId"]
        assert data["cached"] is True
        assert "lotStatus" not in data

        results = _query(tmp_path, "SELECT count(*) FROM quality_inspection_results")
        assert results[0][0] == 1


def test_inspect_role_check(tmp_path):
    """非合格角色 → 403，且先于幂等。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _inspect(client, "k1", role="MES_OPERATOR")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

        resp = _inspect(client, "k1", role="MES_ADMIN")
        assert resp.status_code == 200


def test_inspect_validation(tmp_path):
    """缺幂等键 → 422。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.post(
            "/api/v1/quality/inspections",
            json={"disposition": "ACCEPTED"},
            headers={"X-Actor-Role": "QUALITY_ENGINEER"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
