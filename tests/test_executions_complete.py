"""``POST /api/v1/executions/complete`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/executions/complete`` 一节。
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

    应用端报表汇总按 ``utc_now()[:10]``（真实当天 UTC）更新 ``shift_date`` /
    ``summary_date``，因此测试种子必须使用同一个当天日期，否则 UPDATE 匹配不到行，
    计数保持初始值。写死日期会让测试只在那一天通过。
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


def _setup(tmp_path, with_next_op=True, quality_gate=0, today=None):
    """创建工单 WO-A（含可选下一工序）、单元 SN-1、规格、人工记录与库存。"""
    today = today or _utc_today()
    _exec(
        tmp_path,
        "INSERT INTO production_work_orders "
        "(work_order_number, material_code, plant_code, planned_quantity, status, "
        " completed_quantity, rejected_quantity) "
        "VALUES ('WO-A', 'MAT-A', 'PLANT-A', 100, 'RELEASED', 0, 0)",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_work_order_operations "
        "(work_order_number, sequence, operation_code, work_center_code, quality_gate, status) "
        "VALUES ('WO-A', 10, 'OP-10', 'WC-1', :qg, 'STARTED')",
        qg=quality_gate,
    )
    if with_next_op:
        _exec(
            tmp_path,
            "INSERT INTO production_work_order_operations "
            "(work_order_number, sequence, operation_code, work_center_code, quality_gate, status) "
            "VALUES ('WO-A', 20, 'OP-20', 'WC-2', 0, 'PENDING')",
        )
    _exec(
        tmp_path,
        "INSERT INTO production_product_units "
        "(serial_number, work_order_number, status, current_operation_sequence) "
        "VALUES ('SN-1', 'WO-A', 'IN_PROCESS', 10)",
    )
    _exec(
        tmp_path,
        "INSERT INTO master_parameter_specifications "
        "(material_code, operation_code, code, unit_code, lower_limit, target_value, upper_limit, required) "
        "VALUES ('MAT-A', 'OP-10', 'TEMP', 'C', 10.0, 25.0, 40.0, 1)",
    )
    _exec(
        tmp_path,
        "INSERT INTO master_parameter_specifications "
        "(material_code, operation_code, code, unit_code, lower_limit, target_value, upper_limit, required) "
        "VALUES ('MAT-A', 'OP-10', 'PRESS', 'bar', NULL, 2.5, NULL, 0)",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_labor_records "
        "(labor_id, serial_number, work_order_number, operation_sequence, worker_id, station_code, start_at, status) "
        "VALUES ('LAB-1', 'SN-1', 'WO-A', 10, 'W-1', 'ST-1', '2026-08-20T09:00:00Z', 'OPEN')",
    )
    _exec(
        tmp_path,
        "INSERT INTO material_inventory_balances "
        "(plant_code, material_code, lot_number, on_hand_quantity, reserved_quantity) "
        "VALUES ('PLANT-A', 'MAT-B', 'LOT-1', 100, 5)",
    )
    _exec(
        tmp_path,
        "INSERT INTO reporting_production_shift_summary "
        "(plant_code, shift_date, shift_code, planned_quantity, completed_quantity) "
        "VALUES ('PLANT-A', :d, 'A', 500, 5)",
        d=today,
    )
    _exec(
        tmp_path,
        "INSERT INTO reporting_andon_summary "
        "(plant_code, summary_date, severity, raised_count, closed_count) "
        "VALUES ('PLANT-A', :d, 'CRITICAL', 0, 0)",
        d=today,
    )


def _complete(client: TestClient, key: str, **overrides):
    body = {
        "serialNumber": "SN-1",
        "workOrderNumber": "WO-A",
        "operationSequence": 10,
        "stationCode": "ST-1",
        "equipmentCode": "EQ-1",
        "passed": True,
        "parameters": [{"code": "TEMP", "value": 25.0}],
        "materialConsumptions": [{"materialCode": "MAT-B", "lotNumber": "LOT-1", "quantity": 2}],
    }
    body.update(overrides)
    return client.post(
        "/api/v1/executions/complete",
        json=body,
        headers={"X-Idempotency-Key": key, "X-Actor-Id": "W-1"},
    )


def test_complete_passed_in_process(tmp_path):
    """通过且有下一工序 → IN_PROCESS：当前工序完成、下一工序启动、单元前移。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _complete(client, "k1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["unitStatus"] == "IN_PROCESS"
        assert data["workOrderCompleted"] is False
        assert data["operationSequence"] == 10

        unit = _query(tmp_path, "SELECT current_operation_sequence FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == 20

        ops = _query(tmp_path, "SELECT sequence, status FROM production_work_order_operations WHERE work_order_number='WO-A' ORDER BY sequence")
        assert ops[0][1] == "COMPLETED"
        assert ops[1][1] == "STARTED"

        wo = _query(tmp_path, "SELECT status, completed_quantity FROM production_work_orders WHERE work_order_number='WO-A'")
        assert wo[0][0] == "RELEASED"
        assert wo[0][1] == 0


def test_complete_passed_work_order_completed(tmp_path):
    """通过且无下一工序 → PASSED + workOrderCompleted：工单完成、outbox、全工序完成。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path, with_next_op=False)
        resp = _complete(client, "k1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["unitStatus"] == "PASSED"
        assert data["workOrderCompleted"] is True

        unit = _query(tmp_path, "SELECT status FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "PASSED"

        wo = _query(tmp_path, "SELECT status, completed_quantity FROM production_work_orders WHERE work_order_number='WO-A'")
        assert wo[0][0] == "COMPLETED"
        assert wo[0][1] == 1

        outbox = _query(tmp_path, "SELECT event_type, aggregate_type, status FROM integration_outbox_messages")
        assert len(outbox) == 1
        assert outbox[0][0] == "work_order.completion"
        assert outbox[0][1] == "WORK_ORDER"


def test_complete_failed(tmp_path):
    """人工判定失败 → FAILED：单元失败、工单 rejected_quantity+1。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _complete(client, "k1", passed=False, defectCode="SCRATCH")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["unitStatus"] == "FAILED"
        assert data["workOrderCompleted"] is False

        unit = _query(tmp_path, "SELECT status FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "FAILED"

        wo = _query(tmp_path, "SELECT rejected_quantity FROM production_work_orders WHERE work_order_number='WO-A'")
        assert wo[0][0] == 1

        evt = _query(tmp_path, "SELECT event_type, defect_code FROM production_execution_events")
        assert evt[0][0] == "FAIL"
        assert evt[0][1] == "SCRATCH"


def test_complete_hold_out_of_spec(tmp_path):
    """参数越界 → HOLD：安灯 CRITICAL/QUALITY、汇总累加、事件仍为 PASS。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _complete(client, "k1", parameters=[{"code": "TEMP", "value": 50.0}])
        assert resp.status_code == 200
        assert resp.json()["data"]["unitStatus"] == "HOLD"

        unit = _query(tmp_path, "SELECT status FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "HOLD"

        andons = _query(tmp_path, "SELECT severity, category, resource_type FROM trace_andon_events")
        assert len(andons) == 1
        assert andons[0][0] == "CRITICAL"
        assert andons[0][1] == "QUALITY"
        assert andons[0][2] == "PRODUCT_UNIT"

        summary = _query(tmp_path, "SELECT raised_count FROM reporting_andon_summary")
        assert summary[0][0] == 1

        evt = _query(tmp_path, "SELECT event_type FROM production_execution_events")
        assert evt[0][0] == "PASS"

        # 参数越界落库 in_spec=0
        records = _query(tmp_path, "SELECT code, value, lower_limit, upper_limit, in_spec FROM production_parameter_records")
        assert records[0][4] == 0


def test_complete_waiting_inspection(tmp_path):
    """质量门开启且无 ACCEPTED → WAITING_INSPECTION。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path, quality_gate=1)
        resp = _complete(client, "k1")
        assert resp.status_code == 200
        assert resp.json()["data"]["unitStatus"] == "WAITING_INSPECTION"

        unit = _query(tmp_path, "SELECT status FROM production_product_units WHERE serial_number='SN-1'")
        assert unit[0][0] == "WAITING_INSPECTION"


def test_complete_accepted_inspection_passes(tmp_path):
    """质量门开启且有 ACCEPTED 结果 → 继续流转到 IN_PROCESS。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path, quality_gate=1)
        _exec(
            tmp_path,
            "INSERT INTO quality_inspection_results "
            "(inspection_id, plant_code, serial_number, work_order_number, operation_sequence, disposition, inspector_id, inspected_at) "
            "VALUES ('INS-1', 'PLANT-A', 'SN-1', 'WO-A', 10, 'ACCEPTED', 'Q-1', '2026-08-22T10:00:00Z')",
        )
        resp = _complete(client, "k1")
        assert resp.status_code == 200
        assert resp.json()["data"]["unitStatus"] == "IN_PROCESS"


def test_complete_parameters_records(tmp_path):
    """参数记录：缺失规格侧写入 0，有规格侧写入实际值。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _complete(
            client,
            "k1",
            parameters=[{"code": "PRESS", "value": 2.5}],
        )
        assert resp.status_code == 200
        records = _query(tmp_path, "SELECT code, value, lower_limit, upper_limit, in_spec FROM production_parameter_records")
        assert records[0][0] == "PRESS"
        assert records[0][1] == 2.5
        assert records[0][2] == 0.0
        assert records[0][3] == 0.0
        assert records[0][4] == 1


def test_complete_material_consumption(tmp_path):
    """物料消耗：记录落库、库存扣减、追溯事件写入。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _complete(client, "k1")
        assert resp.status_code == 200

        cons = _query(tmp_path, "SELECT material_code, lot_number, quantity, unit_code FROM production_material_consumptions")
        assert cons[0][0] == "MAT-B"
        assert cons[0][1] == "LOT-1"
        assert cons[0][2] == 2
        assert cons[0][3] == "EA"

        inv = _query(tmp_path, "SELECT on_hand_quantity, reserved_quantity FROM material_inventory_balances")
        assert inv[0][0] == 98
        assert inv[0][1] == 3

        traces = _query(tmp_path, "SELECT event_type, resource_type FROM trace_events WHERE event_type IN ('MATERIAL_CONSUMED','MATERIAL_LOT')")
        assert len(traces) == 2
        assert set(t[0] for t in traces) == {"MATERIAL_CONSUMED", "MATERIAL_LOT"}


def test_complete_labor_closed_and_reporting(tmp_path):
    """人工记录关闭并写时长；当天班次汇总与执行完成追溯、审计落库。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _complete(client, "k1")
        assert resp.status_code == 200

        labor = _query(tmp_path, "SELECT status, duration_seconds FROM production_labor_records WHERE labor_id='LAB-1'")
        assert labor[0][0] == "CLOSED"
        assert labor[0][1] is not None and labor[0][1] >= 0

        summary = _query(tmp_path, "SELECT completed_quantity FROM reporting_production_shift_summary")
        assert summary[0][0] == 6

        trace = _query(tmp_path, "SELECT event_type, resource_type FROM trace_events WHERE event_type='EXECUTION_COMPLETE'")
        assert trace[0][0] == "EXECUTION_COMPLETE"
        assert trace[0][1] == "SERIAL_NUMBER"

        audit = _query(tmp_path, "SELECT action, resource_type FROM audit_events WHERE action='EXECUTION_COMPLETE'")
        assert audit[0][1] == "PRODUCT_UNIT"


def test_complete_idempotent(tmp_path):
    """幂等重放：cached=true，不重复落库。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        first = _complete(client, "k1")
        replay = _complete(client, "k1", passed=False)
        assert replay.status_code == 200
        data = replay.json()["data"]
        assert data["serialNumber"] == "SN-1"
        assert data["cached"] is True

        events = _query(tmp_path, "SELECT count(*) FROM production_execution_events")
        assert events[0][0] == 1
        wo = _query(tmp_path, "SELECT rejected_quantity FROM production_work_orders WHERE work_order_number='WO-A'")
        assert wo[0][0] == 0


def test_complete_validation(tmp_path):
    """缺幂等键/序列号/工单 → 422。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = client.post(
            "/api/v1/executions/complete",
            json={"serialNumber": "SN-1", "workOrderNumber": "WO-A"},
        )
        assert resp.status_code == 422

        resp = _complete(client, "k1", serialNumber="")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

        resp = _complete(client, "k2", workOrderNumber=None)
        assert resp.status_code == 422


def test_complete_unknown_serial_404(tmp_path):
    """序列号不存在 → 404。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _complete(client, "k1", serialNumber="SN-NOPE")
        assert resp.status_code == 404


def test_complete_sequence_mismatch_409(tmp_path):
    """工序与单元当前工序不匹配 → 409。"""
    with _make_client(tmp_path) as client:
        _setup(tmp_path)
        resp = _complete(client, "k1", operationSequence=20)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"
