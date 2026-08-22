"""``GET /metrics`` 度量端点测试。

契约依据：docs/http-api.md 中 ``GET /metrics`` 一节。
"""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _make_client(tmp_path) -> TestClient:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    return TestClient(create_app(settings))


def test_metrics_reports_counts(tmp_path):
    """首次请求不包含自身；后续请求计入总量，4xx 计入客户端错误。"""
    with _make_client(tmp_path) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        first = resp.text
        assert "mes_http_requests_total 0" in first
        assert "mes_http_client_errors_total 0" in first
        assert "mes_http_server_errors_total 0" in first

        # 一次成功请求后总量为 1（此时 /metrics 自身的计数还未写入）
        client.get("/health")
        resp = client.get("/metrics")
        assert "mes_http_requests_total 2" in resp.text

        # 4xx 请求计入客户端错误
        client.get("/not-found")
        resp = client.get("/metrics")
        assert "mes_http_requests_total 4" in resp.text
        assert "mes_http_client_errors_total 1" in resp.text
