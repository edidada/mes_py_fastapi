"""``GET /health`` 接口契约测试。

契约依据：docs/http-api.md 中 ``GET /health`` 一节。
"""

import re
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

RFC3339_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _make_client(tmp_path) -> TestClient:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    return TestClient(create_app(settings))


def test_health_ok(tmp_path):
    """正常启动后返回 200，包含 data 与 meta 顶层字段。"""
    with _make_client(tmp_path) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["data"]["status"] == "UP"
    assert body["data"]["db"] == "OK"
    assert body["data"]["version"] == "1.0.0"
    assert body["meta"]["correlationId"] == "health"
    assert RFC3339_SECONDS.match(body["meta"]["generatedAt"])


def test_health_ignores_query_and_body(tmp_path):
    """查询参数与请求体被忽略。"""
    with _make_client(tmp_path) as client:
        resp = client.request("GET", "/health?foo=bar", content=b'{"x": 1}')

    assert resp.status_code == 200
    assert resp.json()["meta"]["correlationId"] == "health"


def test_health_generated_at_is_recent(tmp_path):
    """generatedAt 为当前 UTC 时间（精度到秒，允许整秒边界误差）。"""
    with _make_client(tmp_path) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
    generated_at = resp.json()["meta"]["generatedAt"]
    now = datetime.now(timezone.utc)
    generated = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((now - generated).total_seconds()) <= 2
