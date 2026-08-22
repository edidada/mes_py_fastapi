"""``GET /api/v1/execution-context`` 接口契约测试。

契约依据：docs/http-api.md 中 ``GET /api/v1/execution-context`` 一节。
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


def _setup_work_order(tmp_path, wo="WO-A", material="MAT-A"):
    """创建工单 WO-A + 工序 10 + 参数/SOP/BOM 聚合主数据。"""
    _exec(
        tmp_path,
        "INSERT INTO production_work_orders "
        "(work_order_number, material_code, planned_quantity, status) "
        "VALUES (:w, :m, 100, 'RELEASED')",
        w=wo,
        m=material,
    )
    _exec(
        tmp_path,
        "INSERT INTO production_work_order_operations "
        "(work_order_number, sequence, operation_code, work_center_code, sop_id) "
        "VALUES (:w, 10, 'OP-10', 'WC-1', 'SOP-1')",
        w=wo,
    )
    _exec(
        tmp_path,
        "INSERT INTO master_parameter_specifications "
        "(material_code, operation_code, code, unit_code, lower_limit, target_value, upper_limit, required) "
        "VALUES (:m, 'OP-10', 'TEMP', 'C', 10.0, 25.0, 40.0, 1)",
        m=material,
    )
    _exec(
        tmp_path,
        "INSERT INTO master_parameter_specifications "
        "(material_code, operation_code, code, unit_code, lower_limit, target_value, upper_limit, required) "
        "VALUES (:m, 'OP-10', 'PRESS', 'bar', NULL, 2.5, NULL, 0)",
        m=material,
    )
    _exec(
        tmp_path,
        "INSERT INTO master_sop_documents (sop_id, document_uri, version) "
        "VALUES ('SOP-1', '/sops/sop-1.pdf', 'v1')",
    )
    _exec(
        tmp_path,
        "INSERT INTO master_materials (material_code, material_name, unit_code) "
        "VALUES ('MAT-1', 'Resin', 'kg'), ('MAT-2', 'Glass', 'kg')",
    )
    _exec(
        tmp_path,
        "INSERT INTO master_boms (bom_code, version, material_code, status) "
        "VALUES ('BOM-A', '1', 'MAT-A', 'EFFECTIVE')",
    )
    _exec(
        tmp_path,
        "INSERT INTO master_bom_components (bom_code, version, material_code, quantity_per, unit_code) "
        "VALUES ('BOM-A', '1', 'MAT-1', 0.5, 'kg'), ('BOM-A', '1', 'MAT-2', 1.5, 'kg')",
    )


def _get(client: TestClient, **params):
    return client.get("/api/v1/execution-context", params=params)


def test_context_serial_required(tmp_path):
    """缺少 serialNumber → 422 VALIDATION_ERROR。"""
    with _make_client(tmp_path) as client:
        resp = _get(client)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_context_new_serial_default_wo_missing(tmp_path):
    """新序列号且默认工单不存在 → 404。"""
    with _make_client(tmp_path) as client:
        resp = _get(client, serialNumber="SN-NEW")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_context_new_serial_auto_create(tmp_path):
    """新序列号 + 指定工单 → 自动创建 CREATED 单元，当前工序 10。"""
    with _make_client(tmp_path) as client:
        _setup_work_order(tmp_path)
        resp = _get(client, serialNumber="SN-NEW", workOrderNumber="WO-A")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["serialNumber"] == "SN-NEW"
        assert data["workOrderNumber"] == "WO-A"
        assert data["materialCode"] == "MAT-A"
        assert data["currentOperationSequence"] == 10
        assert data["unitStatus"] == "CREATED"
        assert data["currentStationCode"] == ""

        rows = _query(
            tmp_path,
            "SELECT status, current_operation_sequence FROM production_product_units WHERE serial_number='SN-NEW'",
        )
        assert rows[0][0] == "CREATED"
        assert rows[0][1] == 10


def test_context_existing_unit(tmp_path):
    """已存在单元 → 返回已有状态与站点。"""
    with _make_client(tmp_path) as client:
        _setup_work_order(tmp_path)
        _exec(
            tmp_path,
            "INSERT INTO production_product_units "
            "(serial_number, work_order_number, status, current_operation_sequence, current_station_code) "
            "VALUES ('SN-EXIST', 'WO-A', 'IN_PROGRESS', 20, 'ST-2')",
        )
        resp = _get(client, serialNumber="SN-EXIST")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["unitStatus"] == "IN_PROGRESS"
        assert data["currentOperationSequence"] == 20
        assert data["currentStationCode"] == "ST-2"


def test_context_aggregate(tmp_path):
    """聚合返回参数规格、SOP 与生效 BOM。"""
    with _make_client(tmp_path) as client:
        _setup_work_order(tmp_path)
        resp = _get(client, serialNumber="SN-AGG", workOrderNumber="WO-A")
        assert resp.status_code == 200
        data = resp.json()["data"]

        assert data["sop"] == {"sopId": "SOP-1", "documentUri": "/sops/sop-1.pdf", "version": "v1"}

        params = {p["code"]: p for p in data["parameters"]}
        assert set(params) == {"TEMP", "PRESS"}
        assert params["TEMP"]["unit"] == "C"
        assert params["TEMP"]["lowerLimit"] == 10.0
        assert params["TEMP"]["target"] == 25.0
        assert params["TEMP"]["upperLimit"] == 40.0
        assert params["TEMP"]["required"] is True
        assert params["PRESS"]["required"] is False
        assert params["PRESS"]["lowerLimit"] is None

        mats = {m["materialCode"]: m for m in data["materials"]}
        assert set(mats) == {"MAT-1", "MAT-2"}
        assert mats["MAT-1"]["quantityPer"] == 0.5
        assert mats["MAT-1"]["unit"] == "kg"
        assert mats["MAT-1"]["materialName"] == "Resin"

        assert data["validations"] == {"operationMatch": True, "equipmentReady": True}


def test_context_no_current_operation(tmp_path):
    """无当前工序时参数为空数组、SOP 为 null，BOM 仍返回。"""
    with _make_client(tmp_path) as client:
        _setup_work_order(tmp_path)
        _exec(
            tmp_path,
            "DELETE FROM production_work_order_operations WHERE work_order_number='WO-A'",
        )
        resp = _get(client, serialNumber="SN-NOOP", workOrderNumber="WO-A")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["parameters"] == []
        assert data["sop"] is None
        assert len(data["materials"]) == 2
