"""``GET /ready`` 接口契约测试。

契约依据：docs/http-api.md 中 ``GET /ready`` 一节。
"""

import re

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

RFC3339_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _make_client(tmp_path) -> TestClient:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    return TestClient(create_app(settings))


def test_ready_ok(tmp_path):
    """正常启动后返回 200，data.ready=true。"""
    with _make_client(tmp_path) as client:
        resp = client.get("/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["ready"] is True
    assert body["meta"]["correlationId"] == "ready"
    assert RFC3339_SECONDS.match(body["meta"]["generatedAt"])


def test_ready_ignores_query_and_body(tmp_path):
    """查询参数与请求体被忽略。"""
    with _make_client(tmp_path) as client:
        resp = client.request("GET", "/ready?x=1", content=b"{}")

    assert resp.status_code == 200
    assert resp.json()["data"]["ready"] is True
