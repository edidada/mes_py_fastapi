# mes-py-fastapi

MES 制造执行系统 HTTP API —— Python 实现（Poetry + FastAPI + Wireup）。

接口契约以 C++ 权威实现（`mes_cpp`）为准，本服务逐条保持兼容。接口文档见 [`docs/http-api.md`](docs/http-api.md)。

## 技术栈

- Python 3.12+
- [Poetry](https://python-poetry.org/) 依赖管理
- [FastAPI](https://fastapi.tiangolo.com/) + Uvicorn
- [Wireup](https://github.com/maldoinc/wireup) 类型驱动依赖注入
- SQLAlchemy 2.0（async）+ aiosqlite

## 快速开始

```bash
poetry install
poetry run uvicorn app.main:app --host 0.0.0.0 --port 8080
```

访问 `http://127.0.0.1:8080/health` 验证服务状态。

## 测试

```bash
poetry run pytest
```

## 项目结构

```
app/
  main.py        # FastAPI 应用入口与路由
  container.py   # Wireup 容器装配
  config.py      # 应用配置（pydantic-settings）
  database.py    # SQLAlchemy 异步引擎与健康检查
  services.py    # 业务服务（Wireup 注入）
tests/           # 接口契约测试
```
