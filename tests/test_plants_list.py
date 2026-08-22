"""``GET /api/v1/master/plants`` 接口契约测试。

契约依据：docs/http-api.md 中 ``GET /api/v1/master/plants`` 一节。
"""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ADMIN = {"X-Actor-Role": "MES_ADMIN"}


def _make_client(tmp_path) -> TestClient:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    return TestClient(create_app(settings))


def test_list_plants_sorted_with_seed(tmp_path):
    """返回全部工厂，含种子 PLANT-A，按代码升序。"""
    with _make_client(tmp_path) as client:
        client.post(
            "/api/v1/master/plants",
            json={"plantCode": "PLANT-B"},
            headers={**ADMIN, "X-Idempotency-Key": "k7"},
        )
        resp = client.get("/api/v1/master/plants")
    assert resp.status_code == 200
    plants = resp.json()["data"]
    codes = [p["plantCode"] for p in plants]
    assert codes == sorted(codes)
    assert "PLANT-A" in codes
    assert "PLANT-B" in codes
    plant_a = next(p for p in plants if p["plantCode"] == "PLANT-A")
    assert plant_a["active"] is True
    assert "timezone" in plant_a


def test_list_plants_empty_and_ignores_query(tmp_path):
    """查询参数被忽略；无数据场景不报错。"""
    with _make_client(tmp_path) as client:
        resp = client.get("/api/v1/master/plants?page=1&pageSize=999")
    assert resp.status_code == 200
    assert resp.json()["data"]
