"""批次查询 / 状态 / 投料 / 参数 / 处置 / EBR 接口契约测试。

契约依据：docs/http-api.md 中 batches 相关小节（2535-2702）。
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
            rows = (await conn.execute(sqltext(sql), params)).fetchall()
        await engine.dispose()
        return rows

    return asyncio.new_event_loop().run_until_complete(_run())


def _seed_batch(tmp_path, status="DRAFT"):
    _exec(tmp_path, "INSERT INTO master_materials (material_code, material_name, unit_code) VALUES ('MAT-PROD', 'Product', 'KG')")
    _exec(
        tmp_path,
        "INSERT INTO master_recipes "
        "(recipe_code, version, recipe_name, product_material_code, target_batch_size, "
        " unit_code, status, effective_from, approved_by, approved_at) VALUES "
        "('REC-1', '1', 'Mix', 'MAT-PROD', 100.0, 'KG', 'EFFECTIVE', '2026-01-01T00:00:00Z', 'A', '2026-01-01T00:00:00Z')",
    )
    _exec(tmp_path, "INSERT INTO asset_equipment (equipment_code, plant_code, current_status) VALUES ('EQP-1', 'PLANT-A', 'IDLE')")
    _exec(
        tmp_path,
        "INSERT INTO master_recipe_components "
        "(recipe_code, version, component_sequence, material_code, phase_code, target_quantity, lower_limit, upper_limit, unit_code) "
        "VALUES ('REC-1', '1', 10, 'MAT-COMP', 'PH1', 50, 10, 100, 'KG')",
    )
    _exec(
        tmp_path,
        "INSERT INTO master_recipe_parameters "
        "(recipe_code, version, step_sequence, parameter_code, target_value, lower_limit, upper_limit, unit_code) "
        "VALUES ('REC-1', '1', 20, 'TEMP', 80, 60, 100, 'C')",
    )
    _exec(
        tmp_path,
        "INSERT INTO material_inventory_balances "
        "(plant_code, material_code, batch_number, on_hand_quantity, reserved_quantity) "
        "VALUES ('PLANT-A', 'MAT-COMP', 'LOT-1', 200, 0)",
    )
    _exec(
        tmp_path,
        "INSERT INTO production_batches "
        "(batch_number, plant_code, product_material_code, recipe_code, recipe_version, "
        " equipment_code, planned_quantity, actual_quantity, unit_code, status, version) "
        "VALUES ('BATCH-001', 'PLANT-A', 'MAT-PROD', 'REC-1', '1', 'EQP-1', 100, 0, 'KG', :st, 1)",
        st=status,
    )


def _h(role="MES_SUPERVISOR", key="k", actor="U"):
    return {"X-Idempotency-Key": key, "X-Actor-Role": role, "X-Actor-Id": actor}


# ---------------- list / get ----------------

def test_list_batches(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path)
        resp = client.get("/api/v1/batches", headers={"X-Actor-Role": "MES_OPERATOR"})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["batches"][0]["batchNumber"] == "BATCH-001"
    assert data["batches"][0]["currentQty"] == 0
    assert data["batches"][0]["targetQty"] == 100


def test_list_batches_filter_status(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path)
        resp = client.get("/api/v1/batches?status=RELEASED", headers={"X-Actor-Role": "MES_OPERATOR"})

    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 0


def test_get_batch(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path)
        resp = client.get("/api/v1/batches/BATCH-001", headers={"X-Actor-Role": "MES_OPERATOR"})

    assert resp.status_code == 200
    assert resp.json()["data"]["batchNumber"] == "BATCH-001"


def test_get_batch_not_found(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.get("/api/v1/batches/NOPE", headers={"X-Actor-Role": "MES_OPERATOR"})

    assert resp.status_code == 404


# ---------------- state ----------------

def test_state_transition_ok(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path, status="DRAFT")
        resp = client.post("/api/v1/batches/BATCH-001/state", json={"targetStatus": "RELEASED"}, headers=_h())

    assert resp.status_code == 200
    assert resp.json()["data"] == {"batchNumber": "BATCH-001", "status": "RELEASED"}
    row = _query(tmp_path, "SELECT status FROM production_batches WHERE batch_number='BATCH-001'")
    assert row[0][0] == "RELEASED"


def test_state_invalid_transition(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path, status="DRAFT")
        resp = client.post("/api/v1/batches/BATCH-001/state", json={"targetStatus": "COMPLETED"}, headers=_h())

    assert resp.status_code == 409


def test_state_role_forbidden(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path, status="DRAFT")
        resp = client.post("/api/v1/batches/BATCH-001/state", json={"targetStatus": "RELEASED"}, headers=_h(role="MES_OPERATOR"))

    assert resp.status_code == 403


def test_state_not_found(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.post("/api/v1/batches/NOPE/state", json={"targetStatus": "RELEASED"}, headers=_h())

    assert resp.status_code == 404


# ---------------- charges ----------------

def test_charges_ok(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path, status="IN_PROGRESS")
        resp = client.post(
            "/api/v1/batches/BATCH-001/charges",
            json={"charges": [{"componentSequence": 10, "materialCode": "MAT-COMP", "lotNumber": "LOT-1", "quantity": 30, "unitCode": "KG"}]},
            headers=_h(role="MES_SUPERVISOR"),
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["chargesPosted"] == 1
    row = _query(tmp_path, "SELECT COUNT(*) FROM production_batch_charges WHERE batch_number='BATCH-001'")
    assert row[0][0] == 1


def test_charges_component_mismatch(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path, status="IN_PROGRESS")
        resp = client.post(
            "/api/v1/batches/BATCH-001/charges",
            json={"charges": [{"componentSequence": 99, "materialCode": "MAT-COMP", "quantity": 30, "unitCode": "KG"}]},
            headers=_h(role="MES_SUPERVISOR"),
        )

    assert resp.status_code == 409


def test_charges_insufficient_inventory(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path, status="IN_PROGRESS")
        resp = client.post(
            "/api/v1/batches/BATCH-001/charges",
            json={"charges": [{"componentSequence": 10, "materialCode": "MAT-COMP", "lotNumber": "LOT-1", "quantity": 9999, "unitCode": "KG"}]},
            headers=_h(role="MES_SUPERVISOR"),
        )

    assert resp.status_code == 409


# ---------------- parameters ----------------

def test_parameters_ok(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path, status="IN_PROGRESS")
        resp = client.post(
            "/api/v1/batches/BATCH-001/parameters",
            json={"parameters": [{"stepSequence": 20, "parameterCode": "TEMP", "value": 85, "unitCode": "C"}]},
            headers=_h(role="MES_OPERATOR"),
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["parametersRecorded"] == 1
    row = _query(tmp_path, "SELECT in_spec FROM production_batch_parameters WHERE batch_number='BATCH-001'")
    assert row[0][0] == 1


def test_parameters_out_of_spec(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path, status="IN_PROGRESS")
        resp = client.post(
            "/api/v1/batches/BATCH-001/parameters",
            json={"parameters": [{"stepSequence": 20, "parameterCode": "TEMP", "value": 200, "unitCode": "C"}]},
            headers=_h(role="MES_OPERATOR"),
        )

    assert resp.status_code == 200
    row = _query(tmp_path, "SELECT in_spec FROM production_batch_parameters WHERE batch_number='BATCH-001'")
    assert row[0][0] == 0


def test_parameters_not_in_recipe(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path, status="IN_PROGRESS")
        resp = client.post(
            "/api/v1/batches/BATCH-001/parameters",
            json={"parameters": [{"stepSequence": 20, "parameterCode": "NOPE", "value": 85, "unitCode": "C"}]},
            headers=_h(role="MES_OPERATOR"),
        )

    assert resp.status_code == 409


# ---------------- quality-disposition ----------------

def test_disposition_ok(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path, status="WAITING_QA")
        resp = client.post(
            "/api/v1/batches/BATCH-001/quality-disposition",
            json={"decision": "ACCEPT", "inspector": "QA1"},
            headers=_h(role="QA_INSPECTOR"),
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["decision"] == "ACCEPT"
    row = _query(tmp_path, "SELECT quality_disposition FROM production_batches WHERE batch_number='BATCH-001'")
    assert row[0][0] == "ACCEPT"


def test_disposition_bad_state(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path, status="DRAFT")
        resp = client.post(
            "/api/v1/batches/BATCH-001/quality-disposition",
            json={"decision": "ACCEPT", "inspector": "QA1"},
            headers=_h(role="QA_INSPECTOR"),
        )

    assert resp.status_code == 409


def test_disposition_invalid_decision(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path, status="WAITING_QA")
        resp = client.post(
            "/api/v1/batches/BATCH-001/quality-disposition",
            json={"decision": "MAYBE", "inspector": "QA1"},
            headers=_h(role="QA_INSPECTOR"),
        )

    assert resp.status_code == 422


# ---------------- ebr ----------------

def test_ebr_ok(tmp_path):
    with _make_client(tmp_path) as client:
        _seed_batch(tmp_path)
        _exec(
            tmp_path,
            "INSERT INTO production_batch_charges "
            "(charge_id, batch_number, component_sequence, material_code, lot_number, quantity, unit_code, charged_by, charged_at) "
            "VALUES ('CHG-1', 'BATCH-001', 10, 'MAT-COMP', 'LOT-1', 30, 'KG', 'U', '2026-01-01T00:00:00Z')",
        )
        _exec(
            tmp_path,
            "INSERT INTO production_batch_parameters "
            "(record_id, batch_number, step_sequence, parameter_code, value, lower_limit, upper_limit, unit_code, in_spec, recorded_by, recorded_at) "
            "VALUES ('PRM-1', 'BATCH-001', 20, 'TEMP', 85, 60, 100, 'C', 1, 'U', '2026-01-01T00:00:00Z')",
        )
        _exec(
            tmp_path,
            "INSERT INTO quality_batch_dispositions "
            "(disposition_id, batch_number, disposition, reason_code, reviewed_by, electronic_signature, reviewed_at) "
            "VALUES ('DSP-1', 'BATCH-001', 'ACCEPT', 'RC1', 'QA1', 'QA1', '2026-01-01T00:00:00Z')",
        )
        resp = client.get("/api/v1/batches/BATCH-001/ebr", headers={"X-Actor-Role": "MES_OPERATOR"})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["batchNumber"] == "BATCH-001"
    assert len(data["charges"]) == 1
    assert len(data["parameters"]) == 1
    assert data["qualityConclusion"] is not None


def test_ebr_not_found(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.get("/api/v1/batches/NOPE/ebr", headers={"X-Actor-Role": "MES_OPERATOR"})

    assert resp.status_code == 404
