"""``POST /api/v1/master/routings`` 接口契约测试。

契约依据：docs/http-api.md 中 ``POST /api/v1/master/routings`` 一节。
"""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ADMIN = {"X-Actor-Role": "MES_ADMIN"}


def _make_client(tmp_path) -> TestClient:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    return TestClient(create_app(settings))


def _create_material(client: TestClient, code: str):
    client.post(
        "/api/v1/master/materials",
        json={"materialCode": code},
        headers={**ADMIN, "X-Idempotency-Key": f"m-{code}"},
    )


def _post(client: TestClient, key: str, body: dict, **headers):
    return client.post(
        "/api/v1/master/routings",
        json=body,
        headers={**ADMIN, "X-Idempotency-Key": key, **headers},
    )


def test_create_routing_ok(tmp_path):
    """正常创建返回 routingCode/version/status=DRAFT。"""
    with _make_client(tmp_path) as client:
        _create_material(client, "MAT-R1")
        resp = _post(
            client,
            "r1",
            {
                "routingCode": "RT-001",
                "version": "1",
                "materialCode": "MAT-R1",
                "operations": [
                    {"sequence": 10, "operationCode": "OP10", "workCenterCode": "WC-1", "qualityGate": True, "allowSkip": False, "standardCycleSeconds": 30},
                    {"sequence": 20, "operationCode": "OP20", "workCenterCode": "WC-2", "standardCycleSeconds": "not-a-number"},
                ],
            },
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["routingCode"] == "RT-001"
    assert data["version"] == "1"
    assert data["status"] == "DRAFT"


def test_create_routing_material_not_found(tmp_path):
    """materialCode 不存在返回 404 NOT_FOUND。"""
    with _make_client(tmp_path) as client:
        resp = _post(client, "r2", {"routingCode": "RT-X", "version": "1", "materialCode": "NO-SUCH"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert resp.json()["error"]["message"] == "material not found"


def test_create_routing_validation(tmp_path):
    """三个路线头字段缺失返回 422；sequence<=0 返回 422。"""
    with _make_client(tmp_path) as client:
        _create_material(client, "MAT-R2")
        resp = _post(client, "r3", {"routingCode": "RT-Y", "version": "1"})
        assert resp.status_code == 422

        resp = _post(
            client,
            "r4",
            {
                "routingCode": "RT-Z",
                "version": "1",
                "materialCode": "MAT-R2",
                "operations": [{"sequence": 0, "operationCode": "OP0", "workCenterCode": "WC"}],
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_routing_work_center_required(tmp_path):
    """workCenterCode 为空返回 422。"""
    with _make_client(tmp_path) as client:
        _create_material(client, "MAT-R3")
        resp = _post(
            client,
            "r5",
            {
                "routingCode": "RT-W",
                "version": "1",
                "materialCode": "MAT-R3",
                "operations": [{"sequence": 10, "operationCode": "OP10", "workCenterCode": ""}],
            },
        )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_routing_conflict(tmp_path):
    """routingCode+version 重复返回 409。"""
    with _make_client(tmp_path) as client:
        _create_material(client, "MAT-R4")
        body = {"routingCode": "RT-DUP", "version": "1", "materialCode": "MAT-R4"}
        _post(client, "r6", body)
        resp = _post(client, "r7", body)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_create_routing_idempotent(tmp_path):
    """相同幂等键返回 cached=true。"""
    with _make_client(tmp_path) as client:
        _create_material(client, "MAT-R5")
        body = {"routingCode": "RT-IDEM", "version": "1", "materialCode": "MAT-R5"}
        _post(client, "r-dup", body)
        resp = _post(client, "r-dup", body)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["routingCode"] == "RT-IDEM"
    assert data["cached"] is True
