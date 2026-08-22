# MES HTTP 接口文档

本文档按 URL 记录 `/Users/ibqo/Develop/git/github/cpp/mes_cpp` mac,win上是D:\develops\git\github\cpp\mes_cpp 的实际 HTTP 实现，Python 服务逐条保持兼容。每完成一个 URL，会同步补充实现、测试、本文档并创建独立 Git 提交。

## 通用约定

- 响应媒体类型：`application/json`，UTF-8。
- 成功响应使用 `data` 与 `meta` 两个顶层字段。
- `meta.generatedAt` 是 UTC RFC 3339 时间，精度到秒，例如 `2026-08-20T12:34:56Z`。
- 本文的“必传”依据 C++ 处理器的实际校验，而不是仅依据设计文档推断。

## `GET /health`

> 实现状态：✅ 已实现（Python 服务 `app/main.py` + `app/services.py`），契约测试见 `tests/test_health.py`。

### 功能

报告 HTTP 服务状态、数据库连接状态和兼容接口版本。对应 C++ 实现为 `backend/src/server/HttpServer.cpp` 中注册的 `/health` 路由。

### 请求

| 位置 | 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- | --- |
| Header | 无 | - | 否 | 不读取认证、关联 ID 或幂等键。 |
| Path | 无 | - | 否 | 固定路径。 |
| Query | 无 | - | 否 | 查询参数会被忽略。 |
| Body | 无 | - | 否 | 请求体会被忽略。 |

访问示例：

```bash
curl -i http://127.0.0.1:8080/health
```

### 成功响应

HTTP 状态码固定为 `200 OK`。

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.status` | string | 否 | 固定为 `UP`。 |
| `data.db` | string | 否 | 数据库连接池已打开时为 `OK`，已关闭时为 `DOWN`。 |
| `data.version` | string | 否 | 固定为 `1.0.0`，与 C++ 接口兼容。 |
| `meta.correlationId` | string | 否 | 固定为 `health`。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

```json
{
  "data": {
    "status": "UP",
    "db": "OK",
    "version": "1.0.0"
  },
  "meta": {
    "correlationId": "health",
    "generatedAt": "2026-08-20T12:34:56Z"
  }
}
```

### 数据库访问

该 URL 不执行 SQL。应用在 FastAPI lifespan 启动阶段按项目配置（`pyproject.toml`）通过 SQLAlchemy 异步引擎 + `aiosqlite` 创建 SQLite 连接池，并作为 FastAPI 依赖注入处理器；处理器检查连接池的健康状态（SQLAlchemy `engine` 连接检查），映射为 `OK` 或 `DOWN`。若初始数据库连接无法建立，应用启动失败，HTTP 服务不会开始监听，这与 C++ 服务启动时数据库打开失败即退出一致。

### 错误响应

处理器本身不产生业务错误响应。应用能够监听时正常返回 `200`；启动阶段无法建立数据库连接时不会开放该 URL。

## `GET /ready`

### 功能

报告服务是否已经具备接收业务流量的数据库条件。对应 C++ 实现为 `backend/src/server/HttpServer.cpp` 中注册的 `/ready` 路由。

### 请求

| 位置 | 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- | --- |
| Header | 无 | - | 否 | 不读取认证、关联 ID 或幂等键。 |
| Path | 无 | - | 否 | 固定路径。 |
| Query | 无 | - | 否 | 查询参数会被忽略。 |
| Body | 无 | - | 否 | 请求体会被忽略。 |

访问示例：

```bash
curl -i http://127.0.0.1:8080/ready
```

### 成功响应

HTTP 状态码固定为 `200 OK`。

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.ready` | boolean | 否 | 数据库连接池已打开时为 `true`，已关闭时为 `false`。 |
| `meta.correlationId` | string | 否 | 固定为 `ready`。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

```json
{
  "data": {
    "ready": true
  },
  "meta": {
    "correlationId": "ready",
    "generatedAt": "2026-08-20T12:34:56Z"
  }
}
```

### 数据库访问

该 URL 不执行 SQL。处理器读取由 FastAPI 依赖注入的 SQLAlchemy 连接池健康状态并取反，得到布尔型 `ready`。如果应用启动时无法创建数据库连接池，服务不会开始监听。

### 错误响应

处理器本身不产生业务错误响应；连接池已关闭时仍返回 HTTP `200`，并通过 `data.ready=false` 表示未就绪，这与 C++ 行为一致。

## `/api/v1/master/plants`

该 URL 提供工厂主数据的创建和列表读取。C++ 权威实现位于 `backend/src/server/HttpServer.cpp` 的 `srv.Post("/api/v1/master/plants", ...)` 与 `srv.Get("/api/v1/master/plants", ...)`。

### `POST /api/v1/master/plants`

#### 功能

创建一条工厂主数据，同时在同一事务写入审计事件和幂等响应索引。仅 `MES_ADMIN` 角色可调用。

#### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空；相同键再次调用返回首次创建的 `plantCode` 和 `cached=true`，不会重复写数据库。 |
| `X-Correlation-Id` | string | 否 | 省略或为空时服务生成 `corr-...`。C++ 错误文案称它必传，但实际 `correlation()` 会先生成，因此实际行为是非必传。 |
| `X-Actor-Role` | string | 条件必传 | 必须为 `MES_ADMIN`；也接受别名 `X-Role`。两者都省略时默认为 `MES_OPERATOR`，随后返回 `403`。 |
| `X-Actor-Id` | string | 否 | 审计操作人；也接受别名 `X-User-Id`，省略时为 `system`。 |
| `Content-Type` | string | 否 | C++ 不检查媒体类型，直接把原始请求体解析为 JSON；建议使用 `application/json`。 |

#### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `plantCode` | string | 是 | 否 | 必须是非空字符串；数据库主键。非字符串按缺失处理。 |
| `plantName` | string | 否 | 否 | 省略、`null` 或非字符串时存为空字符串。 |
| `timezone` | string | 否 | 否 | 省略、`null` 或非字符串时存为空字符串。处理器显式写入该值，因此不会使用表的 `Asia/Shanghai` 默认值。 |

```json
{
  "plantCode": "PLANT-Z",
  "plantName": "测试工厂Z",
  "timezone": "Asia/Shanghai"
}
```

#### 成功响应

首次创建返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.plantCode` | string | 否 | 已创建的工厂代码。 |
| `data.plantName` | string | 否 | 请求中的名称或空字符串。 |
| `data.active` | boolean | 否 | 固定为 `true`。 |
| `meta.correlationId` | string | 否 | 请求值或服务生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中也返回 `200 OK`，但 `data` 类型变为：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.plantCode` | string | 否 | 首次调用保存的资源代码。 |
| `data.cached` | boolean | 否 | 固定为 `true`。 |

#### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `403` | `FORBIDDEN` | 有效角色不是 `MES_ADMIN`。该校验发生在幂等查询之前。 |
| `422` | `VALIDATION_ERROR` | 缺少非空 `X-Idempotency-Key`、JSON 无法解析，或 `plantCode` 缺失/为空/不是字符串。 |
| `409` | `CONFLICT` | `plantCode` 已存在，或插入违反数据库约束；`error.message` 使用 SQLite 原始错误信息。 |

错误响应结构中的 `error.code`、`error.message` 和 `meta.correlationId` 均为 string，且错误 `meta` 不含 `generatedAt`。

#### 数据库访问

1. 查询 `idempotency_keys.idempotency_key`；命中即直接返回缓存资源标识。
2. 开启数据库事务并插入 `master_plants(plant_code, plant_name, timezone)`。
3. 插入 `audit_events`：`action=PLANT_CREATE`、`resource_type=PLANT`、`before_data={}`、`after_data` 为请求 JSON、操作人来自 Header。
4. 插入 `idempotency_keys`：保存状态 `200`、资源代码和 `plant created`；与 C++ 一致，`expires_at` 当前写入调用时间。
5. 提交事务。Python 实现将三项写入作为一个原子事务；任一步失败都会回滚，不会留下半完成主数据。

### `GET /api/v1/master/plants`

#### 功能

按工厂代码升序返回全部工厂主数据，不分页、不做角色或认证校验。

#### 请求

| 位置 | 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- | --- |
| Header | `X-Correlation-Id` | string | 否 | 原样写入响应；省略时生成 `corr-...`。其他 Header 不读取。 |
| Path | 无 | - | 否 | 固定路径。 |
| Query | 无 | - | 否 | 所有查询参数都会被忽略。 |
| Body | 无 | - | 否 | 请求体会被忽略。 |

#### 成功响应

固定返回 `200 OK`；`data` 类型为 array，数据库无记录时为 `[]`。每个数组元素为：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data[].plantCode` | string | 否 | 工厂代码。 |
| `data[].plantName` | string | 否 | 工厂名称。 |
| `data[].timezone` | string | 否 | 时区字符串。 |
| `data[].active` | boolean | 否 | 数据库整数值等于 `1` 时为 `true`，否则为 `false`。 |
| `meta.correlationId` | string | 否 | 请求值或服务生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

#### 数据库访问

执行只读 SQL：

```sql
SELECT plant_code, plant_name, timezone, active
FROM master_plants
ORDER BY plant_code;
```

使用 FastAPI 依赖注入的 SQLAlchemy 连接池，不开启显式事务。数据库由 `init_db()` 在 Web 服务监听前创建兼容表；默认 `reseed_on_start=true`（项目配置项），当前会装入与 C++ 种子一致的 `PLANT-A`。

## `POST /api/v1/master/materials`

### 功能

创建物料主数据，并在同一事务写入审计事件与幂等索引。仅 `MES_ADMIN` 可调用。C++ 权威实现位于 `backend/src/server/HttpServer.cpp` 的 `srv.Post("/api/v1/master/materials", ...)`。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空；相同键重试直接返回首次保存的物料代码。 |
| `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`；C++ 实际不会因省略该 Header 而失败。 |
| `X-Actor-Role` | string | 条件必传 | 必须为 `MES_ADMIN`；别名为 `X-Role`。省略后默认 `MES_OPERATOR`，会返回 `403`。 |
| `X-Actor-Id` | string | 否 | 审计操作人；别名为 `X-User-Id`，省略时为 `system`。 |
| `Content-Type` | string | 否 | 不做媒体类型校验，建议为 `application/json`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `materialCode` | string | 是 | 否 | 非空物料代码，数据库主键；非字符串按缺失处理。 |
| `materialName` | string | 否 | 否 | 省略、`null` 或非字符串时存为空字符串。 |
| `unitCode` | string | 否 | 否 | 省略、空、`null` 或非字符串时存为 `EA`。注意 C++ 不读取 `unit` 字段，传 `unit` 仍会回退到 `EA`。 |
| `lotControlled` | boolean/string/number | 否 | 否 | boolean 原值；string 只有 `true`/`TRUE` 为真；number 非零为真；其他情况为 `false`。 |
| `serialControlled` | boolean/string/number | 否 | 否 | 转换规则同 `lotControlled`。 |

### 成功响应

首次创建返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.materialCode` | string | 否 | 新物料代码。 |
| `data.materialName` | string | 否 | 请求名称或空字符串。 |
| `meta.correlationId` | string | 否 | 请求值或生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中仍返回 `200 OK`，`data` 包含 string 类型 `materialCode` 和固定 boolean `cached=true`，不再返回 `materialName`。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `403` | `FORBIDDEN` | 角色不是 `MES_ADMIN`。 |
| `422` | `VALIDATION_ERROR` | 缺少幂等键、JSON 非法或 `materialCode` 不为非空字符串。 |
| `409` | `CONFLICT` | 物料代码重复或其他插入约束失败。 |

### 数据库访问

1. 读取 `idempotency_keys`，命中时不解析请求体、不写数据库。
2. 在短事务中插入 `master_materials(material_code, material_name, unit_code, lot_controlled, serial_controlled)`；布尔值以 SQLite 整数 `0/1` 保存。
3. 插入 `audit_events`，动作和资源类型分别为 `MATERIAL_CREATE`、`MATERIAL`，`after_data` 保存完整请求 JSON。
4. 插入 `idempotency_keys`，状态为 `200`、消息为 `material created`。
5. 原子提交；失败回滚。

## `POST /api/v1/master/routings`

### 功能

创建 `DRAFT` 工艺路线版本，并在同一事务创建零到多个工序。仅 `MES_ADMIN` 可调用。C++ 权威实现位于 `backend/src/server/HttpServer.cpp` 的 `srv.Post("/api/v1/master/routings", ...)`。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空；相同键重试返回首次保存的路线代码与 `cached=true`。 |
| `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`。 |
| `X-Actor-Role` | string | 条件必传 | 必须为 `MES_ADMIN`；支持别名 `X-Role`。 |
| `X-Actor-Id` | string | 否 | 审计操作人；支持别名 `X-User-Id`，默认 `system`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `routingCode` | string | 是 | 否 | 非空路线代码，与 `version` 组成联合主键。 |
| `version` | string | 是 | 否 | 非空版本。 |
| `materialCode` | string | 是 | 否 | 必须已存在于 `master_materials`。 |
| `operations` | array | 否 | 否 | 省略或 `null` 时创建不含工序的路线；C++ 实际没有“至少一条”校验。 |
| `operations[].sequence` | number/string | 条件必传 | 否 | 数据库要求转换结果大于 `0`，并在同一路线内唯一。number 会截断为整数；整数字符串可转换；其他值为 `0` 并导致 `422`。C++ 不校验数组顺序必须递增。 |
| `operations[].operationCode` | string | 否 | 否 | 省略或非字符串时为空字符串，数据库允许。 |
| `operations[].workCenterCode` | string | 条件必传 | 否 | 必须引用已有工作中心；空值或未知值导致 `422`。 |
| `operations[].qualityGate` | boolean/string/number | 否 | 否 | boolean 原值；`true`/`TRUE` 字符串或非零 number 为真，默认 `false`。 |
| `operations[].allowSkip` | boolean/string/number | 否 | 否 | 转换规则同 `qualityGate`。 |
| `operations[].standardCycleSeconds` | number | 否 | 否 | 仅 JSON number 被读取；字符串即使内容为数字也保存为 `0.0`。 |

### 成功响应

首次创建固定返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.routingCode` | string | 否 | 路线代码。 |
| `data.version` | string | 否 | 路线版本。 |
| `data.status` | string | 否 | 固定为 `DRAFT`。 |
| `meta.correlationId` | string | 否 | 请求值或生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中返回 `200 OK`，`data` 仅包含 string `routingCode` 和 boolean `cached=true`。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `403` | `FORBIDDEN` | 角色不是 `MES_ADMIN`。 |
| `404` | `NOT_FOUND` | `materialCode` 不存在；错误消息固定为 `material not found`。 |
| `409` | `CONFLICT` | 路线代码与版本重复，或路线头插入违反约束。 |
| `422` | `VALIDATION_ERROR` | 缺少幂等键、非法 JSON、三个路线头字段任一缺失，或任一工序插入违反序号/唯一键/工作中心外键等约束。工序失败会回滚路线头和此前工序。 |

### 数据库访问

1. 查询 `idempotency_keys`；命中立即返回。
2. 查询 `master_materials` 验证物料存在。
3. 开启短事务，插入 `master_routings`，`status` 强制为 `DRAFT`。
4. 遍历 `operations`，逐条插入 `master_routing_operations`。
5. 插入 `audit_events`：`action=ROUTING_CREATE`、`resource_type=ROUTING`、资源 ID 为 `routingCode:version`。
6. 插入 `idempotency_keys`，保存路线代码、状态 `200` 和消息 `routing created`，随后原子提交。

## `POST /api/v1/master/equipment`

### 功能

创建设备主数据，初始状态强制为 `OFFLINE`，同时写审计事件与幂等索引。仅 `MES_ADMIN` 可调用。权威实现位于 C++ `backend/src/server/HttpServer.cpp`。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空；相同键返回首次保存的设备代码及 `cached=true`。 |
| `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`。 |
| `X-Actor-Role` | string | 条件必传 | 必须为 `MES_ADMIN`；支持 `X-Role` 别名。 |
| `X-Actor-Id` | string | 否 | 审计操作人；支持 `X-User-Id`，默认 `system`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `equipmentCode` | string | 是 | 否 | 非空设备代码，数据库主键；非字符串按缺失处理。 |
| `plantCode` | string | 否 | 否 | 省略、空、`null` 或非字符串时使用 `PLANT-A`；非空值必须引用已有工厂，否则插入返回 `409`。 |
| `workCenterCode` | string | 条件必传 | 否 | C++ 没有显式校验，但处理器总是写入字符串；空值或未知值违反外键并返回 `409`，因此实际调用必须提供已有工作中心。 |
| `equipmentName` | string | 否 | 否 | 省略或非字符串时存为空字符串。 |
| `criticality` | string | 否 | 否 | 省略、空或非字符串时为 `NORMAL`；数据库未限制枚举，任意非空字符串均被保存。 |

请求中的 `currentStatus`、`active`、`lastHeartbeatAt` 均被忽略。

### 成功响应

首次创建返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.equipmentCode` | string | 否 | 新设备代码。 |
| `data.currentStatus` | string | 否 | 固定为 `OFFLINE`。 |
| `meta.correlationId` | string | 否 | 请求值或生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中返回 `200 OK`，`data` 仅含 string `equipmentCode` 与 boolean `cached=true`。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `403` | `FORBIDDEN` | 角色不是 `MES_ADMIN`。 |
| `422` | `VALIDATION_ERROR` | 缺少幂等键、JSON 非法或 `equipmentCode` 不是非空字符串。 |
| `409` | `CONFLICT` | 设备代码重复、工厂/工作中心不存在或其他数据库插入约束失败。C++ 设计文档描述为显式工作中心校验，但实际处理器依赖外键并统一映射为 `409`。 |

### 数据库访问

1. 查询 `idempotency_keys`；命中时直接返回。
2. 开启短事务，插入 `asset_equipment`；`current_status=OFFLINE`，其余字段按上述默认规则映射。
3. 插入 `audit_events`：`action=EQUIPMENT_CREATE`、`resource_type=EQUIPMENT`，`after_data` 为请求 JSON。
4. 插入 `idempotency_keys`，状态 `200`、消息 `equipment created`。
5. 原子提交；任一步失败回滚。

## `POST /api/v1/master/workers`

### 功能

创建员工主数据，并可同时授予零到多个资质；写入审计事件和幂等索引。仅 `MES_ADMIN` 可调用。权威实现位于 C++ `backend/src/server/HttpServer.cpp`。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空；相同键返回首次保存的员工编号与 `cached=true`。 |
| `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`。 |
| `X-Actor-Role` | string | 条件必传 | 必须为 `MES_ADMIN`；支持别名 `X-Role`。 |
| `X-Actor-Id` | string | 否 | 只用于审计事件；支持别名 `X-User-Id`，默认 `system`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `workerId` | string | 是 | 否 | 非空员工编号，数据库主键。 |
| `teamCode` | string | 条件必传 | 否 | C++ 没有显式校验，但空值或未知值违反团队外键并返回 `409`；实际调用必须提供已有团队。 |
| `displayName` | string | 否 | 否 | 省略、`null` 或非字符串时存为空字符串。 |
| `qualifications` | array of string | 否 | 否 | 省略或 `null` 时不授予资质；重复字符串由 `INSERT OR IGNORE` 静默去重。 |

### 成功响应

首次创建返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.workerId` | string | 否 | 新员工编号。 |
| `data.displayName` | string | 否 | 请求名称或空字符串。 |
| `meta.correlationId` | string | 否 | 请求值或生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中返回 `200 OK`，`data` 仅含 string `workerId` 和 boolean `cached=true`。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `403` | `FORBIDDEN` | 角色不是 `MES_ADMIN`。 |
| `422` | `VALIDATION_ERROR` | 缺少幂等键、JSON 非法或 `workerId` 不是非空字符串。 |
| `409` | `CONFLICT` | 员工编号重复、团队不存在或员工头插入违反其他数据库约束。 |

### 数据库访问

1. 查询 `idempotency_keys`；命中立即返回。
2. 在短事务中插入 `master_workers(worker_id, team_code, display_name)`。
3. 遍历 `qualifications`，执行 `INSERT OR IGNORE` 写入 `master_worker_qualifications`。与常见审计语义不同，C++ 实际把请求 `teamCode` 写入 `granted_by`，Python 保持一致；它不是 `X-Actor-Id`。
4. 插入 `audit_events`：`action=WORKER_CREATE`、`resource_type=WORKER`，审计操作人仍来自 Actor Header。
5. 插入 `idempotency_keys`，状态 `200`、消息 `worker created`，随后提交事务。

资质单条插入错误会像 C++ 一样被忽略，员工、其他有效资质、审计和幂等记录仍可提交。

## `POST /api/v1/production-plans/import`

### 功能

接收 ERP/APS 生产计划，保存为 `VALIDATED`，选择物料最新的 `EFFECTIVE` 路线，自动创建一张 `DRAFT` 工单并复制路线工序快照。该处理器没有角色校验。权威实现位于 C++ `backend/src/server/HttpServer.cpp`。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空；命中时返回首次计划 ID。 |
| `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`。 |
| `X-Actor-Id` | string | 否 | 审计操作人，支持 `X-User-Id`，默认 `system`。 |
| `X-Actor-Role` | string | 否 | 实际处理器不读取，任意角色或省略均可调用。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `sourceSystem` | string | 是 | 否 | 非空来源系统。 |
| `externalReference` | string | 是 | 否 | 非空外部引用；与来源系统、工厂组成数据库唯一键。 |
| `plantCode` | string | 条件必传 | 否 | 代码未显式校验，但空值或未知值违反工厂约束并以 `409 duplicate external reference` 返回。没有 `PLANT-A` 默认值。 |
| `materialCode` | string | 是 | 否 | 必须存在，并且至少有一个 `EFFECTIVE` 路线。 |
| `quantity` | number/string | 条件必传 | 否 | 转换后必须大于 `0`；省略/无效为 `0`，数据库失败被统一映射为 `409 duplicate external reference`。number 截断为整数，整数字符串可转换。 |
| `priority` | number/string | 否 | 否 | 转换为整数，默认 `0`。 |
| `dueAt` | string | 否 | 否 | 省略、`null` 或非字符串时存为空字符串，不做时间格式校验。 |

### 成功响应

首次成功返回 `202 Accepted`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.planId` | string | 否 | `PLN-...` 计划 ID。 |
| `data.status` | string | 否 | 固定 `VALIDATED`。 |
| `data.workOrderNumber` | string | 否 | `WO-yy-MM-dd-xxxxxx` 格式的自动工单号。 |
| `data.workOrderStatus` | string | 否 | 固定 `DRAFT`。 |
| `meta.correlationId` | string | 否 | 请求值或生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中与首次响应不同，返回 `200 OK`，`data` 仅含 string `planId` 和 boolean `cached=true`。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 缺幂等键、非法 JSON、三个显式必传字段缺失，或物料没有 `EFFECTIVE` 路线。后者会回滚已插入计划。 |
| `404` | `NOT_FOUND` | 物料不存在。 |
| `409` | `CONFLICT` | 计划唯一键重复、工厂无效、数量非正或计划插入的其他约束失败时，消息一律为 `duplicate external reference`；自动工单插入失败时返回 SQLite 原始消息。 |

### 数据库访问

1. 查询 `idempotency_keys`；命中返回 `200`。
2. 查询 `master_materials` 验证物料。
3. 开启事务，插入 `production_production_plans`，保存完整请求 JSON 到 `payload`。
4. 按 `effective_from DESC` 查询最新 `master_routings` 有效版本；无结果则回滚。
5. 查询最新有效 `master_boms`；无 BOM 时工单的 `bom_code/bom_version` 保存空字符串。
6. 插入 `production_work_orders`，再读取 `master_routing_operations` 并逐条复制到 `production_work_order_operations`，状态为 `PENDING`。
7. 写 `audit_events`（`PLAN_IMPORT`/`PRODUCTION_PLAN`）和 `idempotency_keys`（保存 HTTP 状态 `200`、消息 `plan imported`），原子提交。

与 C++ 一致，复制单条工序失败会被忽略，不会终止整个计划事务。

## `GET /api/v1/work-orders`

### 功能

按状态、工厂和物料列出工单进度摘要。接口不要求认证角色，也不分页读取；页码和页大小固定为 `1`、`50`。权威实现位于 C++ `backend/src/server/HttpServer.cpp`。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`；同时出现在内、外两层 `meta`。 |

### Query 参数

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `status` | string | 否 | 是 | 提供时对 `status` 做区分大小写的精确匹配；空字符串也会参与过滤。 |
| `plantCode` | string | 否 | 是 | 提供时对 `plant_code` 做精确匹配。 |
| `materialCode` | string | 否 | 是 | 提供时对 `material_code` 做精确匹配。 |

### 成功响应

返回 `200 OK`。C++ 调用了 `respondOk(listEnvelope(...))`，因此响应保留双层封装：工单数组位于 `data.data`，固定分页信息位于 `data.meta`，通用响应元数据位于最外层 `meta`。

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.data[].workOrderNumber` | string | 否 | 工单号。 |
| `data.data[].plantCode` | string | 否 | 工厂代码。 |
| `data.data[].materialCode` | string | 否 | 物料代码。 |
| `data.data[].status` | string | 否 | 工单状态。 |
| `data.data[].priority` | integer | 否 | 优先级。 |
| `data.data[].plannedQuantity` | integer | 否 | 计划数量。 |
| `data.data[].completedQuantity` | integer | 否 | 完成数量。 |
| `data.data[].rejectedQuantity` | integer | 否 | 拒收数量。 |
| `data.data[].completionPercent` | number | 否 | `round(100 * completed / planned, 2)`；计划数量非正时为 `0`。 |
| `data.data[].wipQuantity` | integer | 否 | 状态为 `CREATED/IN_PROCESS/REWORK/HOLD/WAITING_INSPECTION` 的产品单元数。 |
| `data.meta.page` | integer | 否 | 固定 `1`。 |
| `data.meta.pageSize` | integer | 否 | 固定 `50`。 |
| `data.meta.total` | integer | 否 | 过滤后的实际行数。 |
| `data.meta.correlationId` | string | 否 | 请求值或生成值。 |
| `data.meta.generatedAt` | string | 否 | 内层 UTC RFC 3339 时间。 |
| `meta.correlationId` | string | 否 | 外层同一关联 ID。 |
| `meta.generatedAt` | string | 否 | 外层 UTC RFC 3339 时间。 |

结果按 `priority DESC, work_order_number ASC` 排序。数据库异常返回 `500 DATABASE_ERROR`。

### 数据库访问

从 `v_work_order_progress` 读取工单；该视图以 `production_work_orders` 为主表，并通过相关子查询统计 `production_product_units` 中的 WIP 数量。三个 Query 参数按 C++ 固定顺序绑定，未提供的条件不进入 SQL。

## `POST /api/v1/work-orders`

### 功能

从指定的 `EFFECTIVE` 路线显式创建一张 `DRAFT` 工单，并复制路线工序快照。`PLANNER`、`MES_SUPERVISOR`、`MES_ADMIN` 可调用。权威实现位于 C++ `backend/src/server/HttpServer.cpp`。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空；命中时返回首次工单号和 `cached=true`。 |
| `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`。C++ 的错误文案虽同时提到两个 Header，但实际生成关联 ID 后只会因幂等键为空失败。 |
| `X-Actor-Role` | string | 是 | `PLANNER`、`MES_SUPERVISOR`、`MES_ADMIN` 之一；支持别名 `X-Role`。角色检查发生在幂等查询前，重放请求也必须具有允许角色。 |
| `X-Actor-Id` | string | 否 | 审计操作人；支持别名 `X-User-Id`，默认 `system`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `workOrderNumber` | string | 是 | 否 | 非空工单号，数据库主键。 |
| `plantCode` | string | 否 | 否 | 省略、`null`、空字符串或非字符串时使用 `PLANT-A`；非空未知工厂最终返回 `409`。 |
| `materialCode` | string | 条件必传 | 否 | C++ 未显式校验，但必须是已有物料，否则工单插入返回 `409`。 |
| `routingCode` | string | 是 | 否 | 必须与 `routingVersion` 组成现有 `EFFECTIVE` 路线。 |
| `routingVersion` | string | 是 | 否 | 必须与 `routingCode` 组成现有 `EFFECTIVE` 路线。 |
| `bomCode` | string | 否 | 否 | 原样保存；省略、`null` 或非字符串时为空字符串。C++ 工单表未对 BOM 建外键。 |
| `bomVersion` | string | 否 | 否 | 原样保存，默认空字符串。 |
| `priority` | number/string | 否 | 否 | 转换为整数；小数截断，整数字符串可转换，无效值默认 `0`。 |
| `plannedQuantity` | number/string | 条件必传 | 否 | 转换后必须大于 `0`，否则数据库约束导致 `409`。 |
| `plannedStartAt` | string | 否 | 否 | 原样保存，默认空字符串，不校验时间格式。 |
| `plannedEndAt` | string | 否 | 否 | 原样保存，默认空字符串，不校验时间格式。 |

### 成功响应

首次和幂等重放均返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.workOrderNumber` | string | 否 | 创建或缓存的工单号。 |
| `data.status` | string | 条件存在 | 首次创建固定为 `DRAFT`；缓存响应不包含。 |
| `data.cached` | boolean | 条件存在 | 仅缓存响应存在，固定为 `true`。 |
| `meta.correlationId` | string | 否 | 请求值或生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `403` | `FORBIDDEN` | 角色不在允许集合，消息固定为 `PLANNER required`。 |
| `422` | `VALIDATION_ERROR` | 缺幂等键、JSON 非法、工单号为空，或路线版本不存在/不是 `EFFECTIVE`。 |
| `409` | `CONFLICT` | 工单号重复、工厂/物料无效、计划数量非正或其他工单插入约束失败；消息保留 SQLite 错误。 |

### 数据库访问

1. 角色通过后查询 `idempotency_keys`；命中立即返回。
2. 查询 `master_routings`，要求路线代码和版本精确匹配且状态为 `EFFECTIVE`。
3. 开启事务并插入 `production_work_orders`，状态固定 `DRAFT`。
4. 按工序序号读取 `master_routing_operations`，逐条复制到 `production_work_order_operations`，状态固定 `PENDING`。
5. 插入 `audit_events`（`WORK_ORDER_CREATE`/`WORK_ORDER`）和 `idempotency_keys`（状态 `200`、消息 `work order created`），原子提交。

与 C++ 一致，单条工序快照插入失败会被忽略，其他工序、工单、审计和幂等记录仍可提交。

## `GET /api/v1/work-orders/{workOrderNumber}`

### 功能

读取一张工单的生产概要、路线工序快照及当前 WIP 数量。接口不要求写请求 Header 或角色。权威实现位于 C++ `backend/src/server/HttpServer.cpp`。

### 请求参数

| 位置 | 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- | --- |
| Path | `workOrderNumber` | string | 是 | 否 | URL 路径中的工单号，按主键精确查询。 |
| Header | `X-Correlation-Id` | string | 否 | 否 | 省略时生成 `corr-...`。 |

该接口没有 Query 参数和请求体。

### 成功响应

返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.workOrderNumber` | string | 否 | 工单号。 |
| `data.plantCode` | string | 否 | 工厂代码。 |
| `data.materialCode` | string | 否 | 物料代码。 |
| `data.routingCode` | string | 否 | 工单路线代码。 |
| `data.routingVersion` | string | 否 | 工单路线版本。 |
| `data.status` | string | 否 | 当前工单状态。 |
| `data.priority` | integer | 否 | 优先级。 |
| `data.plannedQuantity` | integer | 否 | 计划数量。 |
| `data.completedQuantity` | integer | 否 | 完成数量。 |
| `data.rejectedQuantity` | integer | 否 | 拒收数量。 |
| `data.wipQuantity` | integer | 否 | 状态为 `CREATED/IN_PROCESS/REWORK/HOLD/WAITING_INSPECTION` 的产品单元数。 |
| `data.operations` | array | 否 | 按工序序号升序排列的工单工序快照。 |
| `data.operations[].sequence` | integer | 否 | 工序序号。 |
| `data.operations[].operationCode` | string | 否 | 工序代码。 |
| `data.operations[].workCenterCode` | string | 否 | 工作中心代码。 |
| `data.operations[].qualityGate` | boolean | 否 | 数据库存值严格等于 `1` 时为 `true`。 |
| `data.operations[].allowSkip` | boolean | 否 | 数据库存值严格等于 `1` 时为 `true`。 |
| `data.operations[].standardCycleSeconds` | number | 是 | 标准周期秒数；数据库为空时返回 JSON `null`。 |
| `data.operations[].status` | string | 否 | 工单工序状态。 |
| `meta.correlationId` | string | 否 | 请求值或生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

### 错误响应

工单不存在返回 `404 NOT_FOUND`，消息固定为 `work order not found`。为复刻 `SqliteStore::queryOne` 的实际语义，主工单查询发生数据库错误时也表现为同一个 `404`，而不是新增 `500`。

### 数据库访问

1. 从 `production_work_orders` 按主键查询工单头。
2. 从 `production_work_order_operations` 查询工序快照，按 `operation_sequence` 升序返回。
3. 从 `production_product_units` 统计五种在制状态。

接口只读且不创建事务。与 C++ 的宽松查询包装一致，工序查询失败时 `operations` 降级为空数组，WIP 查询失败时 `wipQuantity` 降级为 `0`。

## `POST /api/v1/work-orders/{workOrderNumber}/state`

### 功能

按照 C++ 固定状态机迁移工单状态；迁移到 `RELEASED` 时把所有 `PENDING` 工序改为 `READY`，并产生审计、ERP outbox 和幂等记录。`PLANNER`、`MES_SUPERVISOR`、`MES_ADMIN` 可调用。

### 请求 Header 与 Path

| 位置 | 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- | --- |
| Path | `workOrderNumber` | string | 是 | 否 | 待迁移工单号；幂等命中发生在工单查询前，因此重放时该路径值会被忽略。 |
| Header | `X-Idempotency-Key` | string | 是 | 否 | 相同键返回首次保存的工单号和 `cached=true`。 |
| Header | `X-Correlation-Id` | string | 否 | 否 | 省略时生成 `corr-...`。 |
| Header | `X-Actor-Role` | string | 是 | 否 | `PLANNER`、`MES_SUPERVISOR`、`MES_ADMIN` 之一；支持 `X-Role`。重放请求仍先检查角色。 |
| Header | `X-Actor-Id` | string | 否 | 否 | 审计操作人；支持 `X-User-Id`，默认 `system`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `targetStatus` | string | 条件必传 | 否 | C++ 没有独立必填校验；省略、`null` 或非字符串会转为空字符串，并以非法状态迁移返回 `409`。 |

允许的状态迁移只有：

| 当前状态 | 允许目标状态 |
| --- | --- |
| `DRAFT` | `RELEASED`、`CANCELLED` |
| `RELEASED` | `IN_PROGRESS`、`SUSPENDED`、`CANCELLED` |
| `SUSPENDED` | `RELEASED`、`CANCELLED` |
| `IN_PROGRESS` | `SUSPENDED`、`COMPLETED` |
| `COMPLETED` | `CLOSED` |
| `CLOSED`、`CANCELLED` | 无 |

### 成功响应

首次迁移返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.workOrderNumber` | string | 否 | 路径工单号。 |
| `data.status` | string | 否 | 目标状态。 |
| `meta.correlationId` | string | 否 | 请求值或生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中返回 `200 OK`，`data` 仅含 string `workOrderNumber` 和 boolean `cached=true`。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `403` | `FORBIDDEN` | 角色不在允许集合，消息固定为 `PLANNER required`。 |
| `422` | `VALIDATION_ERROR` | 缺少幂等键或 JSON 非法。 |
| `404` | `NOT_FOUND` | 工单不存在；与 C++ 查询包装一致，工单查询错误也映射到此响应。 |
| `409` | `CONFLICT` | 状态迁移不在允许表中；消息格式为 `invalid transition {from}->{target}`。 |

### 数据库访问与事件

1. 角色通过后查询 `idempotency_keys`。
2. 从 `production_work_orders` 读取当前 `status/version`；`version` 与 C++ 一样只读取、不参与乐观锁。
3. 事务内更新工单 `status/updated_at`；目标为 `RELEASED` 时，将该工单全部 `PENDING` 工序改为 `READY`。
4. 写 `audit_events`：`WORK_ORDER_TRANSITION`/`WORK_ORDER`，`before_data={"from":...}`、`after_data={"to":...}`。
5. 写 `integration_outbox_messages`：聚合类型 `WORK_ORDER`、事件 `work_order.state.changed`、目标系统 `ERP`、状态 `PENDING`，payload 包含工单号和新状态。
6. 写 `idempotency_keys`：HTTP 状态 `200`、消息 `transitioned`，然后提交。

C++ 没有检查上述事务内各条写语句和 `commit()` 的返回值。Python 为保持该接口的可观察行为也忽略这些返回值；正常数据库结构下它们仍位于同一事务中原子提交。

## `POST /api/v1/work-orders/{workOrderNumber}/dispatch`

### 功能

为工单工序创建一条 `DISPATCHED` 派工记录，并尝试同步更新已有的工序任务站点、员工和状态。仅 `MES_SUPERVISOR`、`MES_ADMIN` 可调用。权威实现位于 C++ `backend/src/server/HttpServer.cpp`。

### 请求 Header 与 Path

| 位置 | 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- | --- |
| Path | `workOrderNumber` | string | 是 | 否 | 派工所属工单；不存在时由外键失败并返回 `422`，没有单独的 `404`。幂等命中时路径值被忽略。 |
| Header | `X-Idempotency-Key` | string | 是 | 否 | 相同键返回首次保存的派工 ID 和 `cached=true`。 |
| Header | `X-Correlation-Id` | string | 否 | 否 | 省略时生成 `corr-...`。 |
| Header | `X-Actor-Role` | string | 是 | 否 | `MES_SUPERVISOR` 或 `MES_ADMIN`；支持别名 `X-Role`。`PLANNER` 无权派工。重放请求也检查角色。 |
| Header | `X-Actor-Id` | string | 否 | 否 | 审计操作人；支持 `X-User-Id`，默认 `system`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `operationSequence` | number/string | 条件必传 | 否 | 转换为整数，小数截断、整数字符串可转换、无效值为 `0`。C++ 表没有该字段到工单工序的外键，也没有正数约束，因此 `0` 或不存在的工序序号仍可能成功。 |
| `stationCode` | string | 条件必传 | 否 | C++ 未显式校验；数据库要求非空且站点存在，省略/未知值返回 `422`。 |
| `workerId` | string | 条件必传 | 否 | 列定义可空，但 C++ 对省略值绑定空字符串而不是 SQL `NULL`；启用外键时实际必须是已有员工，否则返回 `422`。 |
| `shiftCode` | string | 否 | 否 | 原样保存，默认空字符串，无班次外键。 |
| `priority` | number/string | 否 | 否 | 转换为整数，默认 `0`。 |
| `scheduledStartAt` | string | 否 | 否 | 原样保存，默认空字符串，不校验时间格式。 |

请求中的其他字段被数据库逻辑忽略，但仍原样保存在审计 `after_data` 中。

### 成功响应

首次派工返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.assignmentId` | string | 否 | 新建 `DSP-...` 派工 ID。 |
| `data.status` | string | 否 | 固定 `DISPATCHED`。 |
| `meta.correlationId` | string | 否 | 请求值或生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中返回 `200 OK`，`data` 仅含 string `assignmentId` 和 boolean `cached=true`。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `403` | `FORBIDDEN` | 角色不是 `MES_SUPERVISOR/MES_ADMIN`，消息固定为 `MES_SUPERVISOR required`。 |
| `422` | `VALIDATION_ERROR` | 缺少幂等键、JSON 非法，或插入派工记录违反工单/站点/员工等数据库约束；数据库错误消息原样返回。 |

### 数据库访问

1. 角色通过后查询 `idempotency_keys`。
2. 事务内插入 `production_dispatch_assignments`，状态固定 `DISPATCHED`；这是唯一检查执行结果的业务写入，失败即回滚并返回 `422`。
3. 更新匹配工单号和工序序号的 `production_operation_tasks`：站点、员工取请求值，状态改为 `DISPATCHED`。没有匹配任务时影响零行但派工仍成功。
4. 写 `audit_events`：`DISPATCH`/`WORK_ORDER`，资源 ID 为路径工单号。
5. 写 `idempotency_keys`：状态 `200`、资源 ID 为派工 ID、消息 `dispatched`，然后提交。

与 C++ 一致，第 3 至第 5 步及提交的返回值不参与 HTTP 成败判断。

## `POST /api/v1/stations/{stationCode}/sessions`

### 功能

登录或退出站点会话。`action` 为 `LOGIN`（或缺省）时创建 ACTIVE 会话；任何其他非空字符串都执行退出。接口不检查角色。权威实现位于 C++ `backend/src/server/HttpServer.cpp`。

### 请求 Header 与 Path

| 位置 | 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- | --- |
| Path | `stationCode` | string | 是 | 否 | 登录时必须是已有站点，否则插入返回 `422`；退出时仅作为 UPDATE 条件，未知站点也返回成功。幂等命中时路径值被忽略。 |
| Header | `X-Idempotency-Key` | string | 是 | 否 | 相同键命中时统一返回 `sessionId` 与 `cached=true`。退出记录保存的资源 ID 是空字符串。 |
| Header | `X-Correlation-Id` | string | 否 | 否 | 省略时生成 `corr-...`。 |
| Header | `X-Actor-Id` | string | 否 | 否 | 仅登录审计使用；支持 `X-User-Id`，默认 `system`。 |
| Header | `X-Actor-Role` | string | 否 | 是 | 处理器不读取，任意角色或省略均可。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `action` | string | 否 | 否 | 省略、`null`、非字符串或空字符串时默认为 `LOGIN`；严格等于大写 `LOGIN` 时登录，其他非空字符串全部退出。 |
| `userId` | string | 否 | 否 | 省略、`null` 或非字符串时为空字符串。登录表仅要求 NOT NULL，因此空字符串可成功保存；退出按该值匹配 ACTIVE 会话。 |
| `shiftCode` | string | 否 | 否 | 登录时原样保存，默认空字符串；退出时忽略。 |

### 登录成功响应

返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.sessionId` | string | 否 | 新建 `SES-...` 会话 ID。 |
| `data.status` | string | 否 | 固定 `ACTIVE`。 |
| `meta.correlationId` | string | 否 | 请求值或生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

登录前会在同一事务中把相同站点、相同用户的其他 ACTIVE 会话关闭并填写 `logged_out_at`。

### 退出与缓存响应

退出返回 `200 OK`，`data` 仅含 `status="CLOSED"`；即使没有匹配的 ACTIVE 会话也成功。

幂等命中返回 `200 OK`，`data` 仅含 string `sessionId` 和 boolean `cached=true`。重放登录时 `sessionId` 为首次登录 ID；重放退出时是空字符串，且不再返回 `status`。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 缺少幂等键、JSON 非法，或登录插入因站点不存在等数据库约束失败。 |

### 数据库访问

1. 查询 `idempotency_keys`；命中立即返回。
2. 登录分支开启事务，关闭旧 ACTIVE 会话，插入 `production_station_sessions`，状态 `ACTIVE`。
3. 登录写 `audit_events`：`STATION_LOGIN`/`STATION`，资源 ID 为站点；再写幂等记录（资源为会话 ID、消息 `logged in`）并提交。
4. 退出分支不开事务，只关闭匹配会话并写幂等记录（资源为空字符串、消息 `logged out`）；它不写审计事件。

与 C++ 一致，除了登录会话 INSERT 外，其余 UPDATE、审计、幂等和提交结果都不改变 HTTP 成功响应。

## `GET /api/v1/execution-context`

### 功能

按序列号读取执行上下文，聚合产品单元、工单、当前工序、SOP、参数规格和生效 BOM 组件。若序列号尚不存在，会基于请求工单（或默认演示工单）自动创建 `CREATED` 产品单元。接口不检查角色。权威实现位于 C++ `backend/src/server/HttpServer.cpp`。

### 请求 Header 与 Query

| 位置 | 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- | --- |
| Query | `serialNumber` | string | 是 | 是 | 参数必须出现；完全缺失返回 `422`。C++ 不拒绝空字符串，因此 `serialNumber=` 仍进入查询/自动创建流程。 |
| Query | `workOrderNumber` | string | 否 | 是 | 仅在序列号不存在时使用；省略则固定使用 `WO-20260801-001`，显式空字符串不会使用默认值。序列号已存在时忽略。 |
| Header | `X-Correlation-Id` | string | 否 | 否 | 省略时生成 `corr-...`。 |
| Header | `X-Actor-Role` | string | 否 | 是 | 处理器不读取。 |

该接口没有请求体，也不使用幂等 Header。自动创建依赖序列号主键天然去重，但并发首次请求仍以数据库实际结果为准。

### 成功响应

返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.serialNumber` | string | 否 | 请求序列号。 |
| `data.workOrderNumber` | string | 否 | 产品单元所属工单。 |
| `data.materialCode` | string | 否 | 工单物料。 |
| `data.currentOperationSequence` | integer | 否 | 当前工序；自动创建时固定为 `10`，不查询路线首工序。 |
| `data.currentStationCode` | string | 否 | 当前站点；数据库为 NULL 时返回空字符串。 |
| `data.unitStatus` | string | 否 | 产品单元状态；自动创建时为 `CREATED`。 |
| `data.sop` | object/null | 是 | 当前工序有 `sop_id` 且主数据存在时返回 SOP，否则为 `null`。 |
| `data.sop.sopId` | string | 否 | SOP ID。 |
| `data.sop.documentUri` | string | 否 | 文档 URI。 |
| `data.sop.version` | string | 否 | SOP 版本。 |
| `data.parameters` | array | 否 | 当前物料和工序的参数规格；当前工序不存在或查询失败时为空。 |
| `data.parameters[].code` | string | 否 | 参数代码。 |
| `data.parameters[].unit` | string/null | 是 | 单位。 |
| `data.parameters[].lowerLimit` | number/null | 是 | 下限。 |
| `data.parameters[].target` | number/null | 是 | 目标值。 |
| `data.parameters[].upperLimit` | number/null | 是 | 上限。 |
| `data.parameters[].required` | boolean | 否 | 数据库存值严格等于 `1` 时为 true。 |
| `data.materials` | array | 否 | 该父物料所有 `EFFECTIVE` BOM 的组件；C++ 不按工单固化的 BOM 版本过滤，也没有显式排序。 |
| `data.materials[].materialCode` | string | 否 | 组件物料代码。 |
| `data.materials[].quantityPer` | number | 否 | 单位产品用量。 |
| `data.materials[].unit` | string | 否 | 用量单位。 |
| `data.materials[].materialName` | string | 否 | 组件物料名称。 |
| `data.validations.operationMatch` | boolean | 否 | 固定 `true`，当前实现不做动态校验。 |
| `data.validations.equipmentReady` | boolean | 否 | 固定 `true`，当前实现不查询设备。 |
| `meta.correlationId` | string | 否 | 请求值或生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | `serialNumber` Query 参数完全缺失，消息固定为 `serialNumber required`。 |
| `404` | `NOT_FOUND` | 新序列号指定/默认的工单不存在，消息固定为 `work order not found for new serial`。 |
| `500` | `DATABASE_ERROR` | 自动创建后仍无法读取产品单元，或已有关联工单无法读取。该分支是对 C++ 空 optional 解引用风险的安全映射，不改变正常契约。 |

### 数据库访问与副作用

1. 查询 `production_product_units`。
2. 序列号不存在时查询 `production_work_orders`；存在则插入产品单元，当前工序 `10`、状态 `CREATED`，再读回。
3. 查询关联工单与 `production_work_order_operations` 当前工序。
4. 当前工序存在时查询 `master_parameter_specifications`；查询 `master_sop_documents` 解析 SOP。
5. 联结 `master_bom_components/master_boms/master_materials` 查询所有生效 BOM 组件。

除首次创建产品单元外均为只读；该自动创建不写审计、幂等、trace 或 outbox，与 C++ 一致。参数、BOM、SOP 辅助查询失败时分别降级为空数组或 `null`。

## `POST /api/v1/executions/start`

### 功能

开始一个离散产品单元的工序执行：记录 START 事件，把单元置为 `IN_PROCESS`，按条件启动工单，并同步工序、派工任务、人工记录、追溯、审计和幂等投影。接口不检查角色。权威实现位于 C++ `backend/src/server/HttpServer.cpp`。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 相同键返回首次保存的序列号与 `cached=true`。 |
| `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`；写入执行事件、审计和响应。 |
| `X-Actor-Id` | string | 否 | 执行人；支持 `X-User-Id`，默认 `system`。同时写入事件 operator、人工 worker 和审计 actor。若默认 `system` 不是员工，人工记录外键写入会失败，但 C++ 忽略该失败。 |
| `X-Actor-Role` | string | 否 | 处理器不读取。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `serialNumber` | string | 是 | 否 | 非空且必须已存在于 `production_product_units`。 |
| `workOrderNumber` | string | 是 | 否 | 非空；C++ 不验证它等于产品单元所属工单。 |
| `operationSequence` | number/string | 否 | 否 | 转换为整数，小数截断、整数字符串可转换，无效值默认 `0`；不验证等于产品单元当前工序。 |
| `operationCode` | string | 否 | 否 | 省略、空、`null` 或非字符串时生成 `OP-{operationSequence}`。 |
| `stationCode` | string | 否 | 否 | 原样写入事件、产品单元和人工记录，默认空字符串；不校验站点。 |
| `equipmentCode` | string | 否 | 否 | 原样写入执行事件和 trace resourceCode，默认空字符串；不校验设备。 |

其他 JSON 字段不参与状态更新，但完整请求仍保存到审计 `after_data`。

### 成功响应

首次调用返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.serialNumber` | string | 否 | 请求序列号。 |
| `data.status` | string | 否 | 固定 `IN_PROCESS`。 |
| `meta.correlationId` | string | 否 | 请求值或生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中返回 `200 OK`，`data` 仅含 string `serialNumber` 和 boolean `cached=true`；命中发生在 JSON 解析与业务校验前。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 缺少幂等键、JSON 非法，或 `serialNumber/workOrderNumber` 不是非空字符串。 |
| `404` | `NOT_FOUND` | 序列号不存在；数据库查询失败也按 C++ `queryOne` 语义表现为此响应。 |

### 数据库访问与状态变化

1. 查询 `idempotency_keys`，再查询产品单元是否存在。
2. 事务内插入 `production_execution_events`：工厂固定 `PLANT-A`、事件类型 `START`。
3. 产品单元更新为 `IN_PROCESS`，写入当前站点和进入时间。
4. 仅当请求工单当前为 `RELEASED` 时更新为 `IN_PROGRESS`，首次设置 `actual_start_at`。
5. 将请求工单/工序的 `production_work_order_operations` 与 `production_operation_tasks` 更新为 `STARTED`。
6. 插入 OPEN `production_labor_records`。
7. 插入 `trace_events`：`EXECUTION_START`/`EQUIPMENT`；插入 `audit_events`：`EXECUTION_START`/`PRODUCT_UNIT`。
8. 插入幂等记录：状态 `200`、资源为序列号、消息 `started`，然后提交。

C++ 除事务开始外不检查第 2 至第 8 步任何 SQL 或 commit 返回值。Python 保持该可观察行为：只要前置序列号存在，即使请求工单/工序不匹配或部分投影写入失败，仍返回成功。

## `POST /api/v1/executions/complete`

### 功能

完成离散产品单元的当前工序。接口记录过程参数和物料消耗，根据人工判定、参数规格与质量门决定产品单元的新状态，并同步工单/工序、人工、追溯、安灯、报表、outbox、审计和幂等投影。接口不检查角色。权威实现位于 C++ `backend/src/server/HttpServer.cpp`。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空。相同键重试时直接返回首次保存的序列号与 `cached=true`，不会解析或校验本次 Body。 |
| `X-Correlation-Id` | string | 否 | 省略或为空时生成 `corr-...`；写入执行事件、审计和响应。C++ 通用错误文案称关联 ID 与幂等键都必传，但 `correlation()` 会先生成关联 ID，因此实际只会因幂等键为空失败。 |
| `X-Actor-Id` | string | 否 | 执行人；也接受 `X-User-Id`，默认 `system`。写入参数记录、物料消耗、执行事件、审计，并用于关闭已有人工记录。 |
| `X-Actor-Role` | string | 否 | 处理器不读取，不做角色授权。 |
| `Content-Type` | string | 否 | C++ 不验证媒体类型，直接解析原始 Body；建议使用 `application/json`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `serialNumber` | string | 是 | 否 | 必须是非空字符串，并已存在于 `production_product_units`。 |
| `workOrderNumber` | string | 是 | 否 | 必须是非空字符串。C++ 不验证它是否等于产品单元自身的工单号；后续工序、规格、状态和事件均按请求值查询或写入。 |
| `operationSequence` | number/string | 条件必传 | 否 | 未显式做“必传”校验，但转换结果必须等于产品单元当前工序，否则返回 `409`。整数直接使用；小数截断；整数字符串转换；省略、`null`、无效字符串或其他类型为 `0`。 |
| `equipmentCode` | string | 否 | 否 | 省略、`null` 或非字符串时为空字符串；写入执行事件，不校验设备主数据。 |
| `stationCode` | string | 否 | 否 | 转换规则同上；写入执行事件，不校验站点主数据。 |
| `passed` | boolean/string/number | 否 | 否 | 默认 `true`。boolean 原值；string 仅 `true`/`TRUE` 为真；number 非零为真；其他字符串为假；其他类型回退默认值。 |
| `defectCode` | string | 否 | 否 | 省略、`null` 或非字符串时为空字符串；写入执行事件。 |
| `parameters` | array | 否 | 否 | 过程参数数组；省略、`null` 或非 array 时不记录参数。 |
| `parameters[].code` | string | 否 | 否 | 参数代码；省略、`null` 或非字符串时为空字符串。 |
| `parameters[].value` | number | 否 | 否 | 测量值；仅 JSON number 有效，省略、`null`、字符串或其他类型按 `0.0`。 |
| `materialConsumptions` | array | 否 | 否 | 物料消耗数组；省略、`null` 或非 array 时不记录消耗。 |
| `materialConsumptions[].materialCode` | string | 否 | 否 | 消耗物料代码；省略或类型不符时为空字符串，写入可能因外键失败。 |
| `materialConsumptions[].lotNumber` | string | 否 | 否 | 批次号；省略或类型不符时为空字符串，写入可能因外键失败。 |
| `materialConsumptions[].quantity` | number | 否 | 否 | 默认 `1.0`；仅 JSON number 有效。接口不读取请求中的单位，消耗记录的 `unit_code` 固定为 `EA`。 |

正常请求示例：

```json
{
  "serialNumber": "SN-000001",
  "workOrderNumber": "WO-20260801-001",
  "operationSequence": 10,
  "stationCode": "ST-ASSY-01",
  "equipmentCode": "EQ-ASSY-01",
  "passed": true,
  "parameters": [
    {"code": "TEMP", "value": 85},
    {"code": "PRESSURE", "value": 120}
  ],
  "materialConsumptions": [
    {"materialCode": "MAT-WAFER-01", "lotNumber": "LOT-001", "quantity": 1}
  ]
}
```

### 状态判定

判定按以下顺序执行，前面的条件优先：

| 条件 | `unitStatus` | `workOrderCompleted` | 执行事件类型 |
| --- | --- | --- | --- |
| `passed=false` | `FAILED` | `false` | `FAIL` |
| `passed=true`，任一有规格参数低于下限或高于上限 | `HOLD` | `false` | `PASS` |
| `passed=true`、参数合格、当前工序是质量门，但没有该序列号/工序的 `ACCEPTED` 检验结果 | `WAITING_INSPECTION` | `false` | `PASS` |
| 前述条件均不满足，并存在更大的工序序号 | `IN_PROCESS` | `false` | `PASS` |
| 前述条件均不满足，并且没有下一工序 | `PASSED` | `true` | `PASS` |

参数代码没有匹配规格时视为合格。规格下限或上限可空：只有非空的一侧参与比较。注意 `HOLD` 分支的执行事件仍是 `PASS`，因为 C++ 的事件类型仅由请求 `passed` 决定。

### 成功响应

首次处理返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.serialNumber` | string | 否 | 请求序列号。 |
| `data.operationSequence` | number | 否 | 转换后的当前工序序号。 |
| `data.unitStatus` | string | 否 | `FAILED`、`HOLD`、`WAITING_INSPECTION`、`IN_PROCESS` 或 `PASSED`。 |
| `data.workOrderCompleted` | boolean | 否 | 仅正常通过最后一道工序时为 `true`。 |
| `meta.correlationId` | string | 否 | 请求值或服务生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

```json
{
  "data": {
    "serialNumber": "SN-000001",
    "operationSequence": 10,
    "unitStatus": "IN_PROCESS",
    "workOrderCompleted": false
  },
  "meta": {
    "correlationId": "corr-...",
    "generatedAt": "2026-08-21T01:04:32Z"
  }
}
```

幂等命中也返回 `200 OK`，但 `data` 只包含 string `serialNumber` 和固定 boolean `cached=true`；不再返回工序、状态或工单完成标记。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | `X-Idempotency-Key` 为空、JSON 无法解析，或 `serialNumber/workOrderNumber` 不是非空字符串。 |
| `404` | `NOT_FOUND` | 序列号不存在；与 C++ `queryOne` 一致，产品单元查询失败也表现为未找到。 |
| `409` | `CONFLICT` | 请求 `operationSequence` 与产品单元 `current_operation_sequence` 不相等。 |
| `500` | `DATABASE_ERROR` | Python 在幂等查询或事务开启失败时返回数据库错误；正常业务写入仍保持 C++ 的忽略返回值语义。 |

错误响应包含 string `error.code`、string `error.message` 和 string `meta.correlationId`，不包含 `meta.generatedAt`。

### 数据库访问与状态变化

1. 查询 `idempotency_keys`；命中即返回缓存。随后读取 `production_product_units`，校验序列号存在和当前工序完全匹配。
2. 查询请求工单/工序的 `production_work_order_operations(operation_code, quality_gate)` 与 `production_work_orders(material_code, plant_code)`。工单不存在时工厂回退为 `PLANT-A`，工序不存在时代码回退为 `OP-{operationSequence}` 且不是质量门。
3. 在事务中逐项查询 `master_parameter_specifications`，写入 `production_parameter_records`。记录会保存测量值、规格上下限、操作人、时间和 `in_spec`；缺少某侧规格时，C++ 实际写入数值 `0` 而不是 SQL `NULL`，Python 保持一致。
4. 逐项写入 `production_material_consumptions`，单位固定 `EA`；按工厂/物料/批次扣减所有匹配的 `material_inventory_balances`，并写入 `MATERIAL_CONSUMED`/`MATERIAL_LOT` 追溯事件。预留量只有在严格大于本次数量时才扣减，等于时保持原值，这是 C++ 当前 SQL 的实际行为。
5. C++ 当前先只查询 `operation_code, quality_gate`，随后检查同一 JSON 行里未被查询的 `sop_id`；该字段会成为 `null`，所以 `production_sop_acknowledgements` 分支实际不会写入。Python 保持这一可观察行为，避免迁移后无依据地增加 SOP 确认记录。
6. 查询下一工序。质量门还会查询 `quality_inspection_results` 中相同序列号/工序且 `disposition=ACCEPTED` 的记录。
7. `FAILED` 时工单 `rejected_quantity+1`；`HOLD` 时写入 CRITICAL/QUALITY `trace_andon_events`，并更新当天已有的 `reporting_andon_summary` 行（不存在时不自动插入）。
8. 写入 `production_execution_events`，包含幂等键、关联 ID、工厂、工单、序列号、工序/工位/设备、操作人、PASS/FAIL 和缺陷代码。
9. `PASSED`：完成产品单元，工单 `completed_quantity+1`，当前工序完成；全部工序都完成/跳过时工单置 `COMPLETED`；始终写一条发往 ERP 的 `work_order.completion` outbox。`IN_PROCESS`：当前工序完成、下一工序启动并移动产品单元。`WAITING_INSPECTION`：产品单元等待检验且当前工序完成。`HOLD/FAILED`：更新产品单元状态并完成当前工序。
10. 关闭同序列号/工序的所有 OPEN `production_labor_records`，用 SQLite 秒时间差计算 `duration_seconds`。
11. 写入 `EXECUTION_COMPLETE`/`SERIAL_NUMBER` 追溯和 `EXECUTION_COMPLETE`/`PRODUCT_UNIT` 审计；更新当天已有的 `reporting_production_shift_summary` 行；保存状态 `200` 的幂等记录后提交。

与 C++ 一致，事务中的插入、更新、投影及最终 commit 返回值不决定 HTTP 成功与否：前置校验通过并成功开启事务后，即使某一项因为外键、检查约束或缺少报表行而没有写入，接口仍按计算结果返回 `200`。这一兼容行为不等同于推荐的新接口事务策略。

## `POST /api/v1/executions/rework`

### 功能

创建返工记录，并尝试把产品单元移动到指定目标工序、置为 `REWORK`。同时写入追溯、审计和幂等投影。接口不检查角色、产品单元是否存在、当前状态是否允许返工或目标工序是否属于工单；这些与详细设计文档不同的宽松行为来自当前 C++ 实际处理器。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空。相同键再次请求直接返回首次保存的序列号与 `cached=true`。 |
| `X-Correlation-Id` | string | 否 | 省略或为空时生成 `corr-...`；用于追溯之外的执行事件尝试、审计和响应。 |
| `X-Actor-Id` | string | 否 | 操作人；支持别名 `X-User-Id`，默认 `system`。写入审计，并绑定到执行事件写入尝试。 |
| `X-Actor-Role` | string | 否 | 处理器不读取，不做授权。 |
| `Content-Type` | string | 否 | 不验证媒体类型，建议为 `application/json`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `serialNumber` | string | 是 | 否 | 必须为非空字符串。接口不校验该序列号是否存在；不存在时仍创建返工记录并返回成功。 |
| `targetOperationSequence` | number/string | 否 | 否 | 目标工序。整数直接使用、小数截断、整数字符串转换；省略、`null`、无效字符串或其他类型为 `0`。不校验正数或是否存在对应工序。 |
| `reasonCode` | string | 否 | 否 | 返工原因；省略、`null` 或非字符串时写入空字符串。 |
| `approvedBy` | string | 否 | 否 | 批准人；省略、`null` 或非字符串时写入空字符串，不与 Header 操作人联动，也不验证人员主数据。 |

```json
{
  "serialNumber": "SN-000001",
  "targetOperationSequence": 10,
  "reasonCode": "RW-DEFECT",
  "approvedBy": "W-002"
}
```

### 成功响应

首次处理固定返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.serialNumber` | string | 否 | 请求序列号。 |
| `data.status` | string | 否 | 固定为 `REWORK`；即使产品单元不存在或内部某项写入失败也不改变。 |
| `data.targetOperationSequence` | number | 否 | 转换后的目标工序，默认 `0`。 |
| `meta.correlationId` | string | 否 | 请求值或服务生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中同样返回 `200 OK`，但 `data` 只包含 string `serialNumber` 和 boolean `cached=true`，不会返回状态或目标工序。命中发生在 Body 解析和必填校验之前。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 幂等键为空、JSON 无法解析，或 `serialNumber` 缺失/为空/不是字符串。 |
| `500` | `DATABASE_ERROR` | Python 在幂等查询或事务开启失败时返回数据库错误。事务开始后的 SQL 和 commit 错误按 C++ 行为忽略。 |

当前实现没有 `404`、`409` 或 `403` 分支：未知序列号、无效目标工序、任意当前状态和任意角色都能得到业务成功响应。

### 数据库访问与状态变化

1. 查询 `idempotency_keys`；命中后不解析请求体。
2. 开启事务，插入 `production_rework_orders`，包含生成的 `RW-...` 标识、序列号、目标工序、原因和批准人。该表不对序列号建立外键，因此未知序列号仍可留下返工记录。
3. 按序列号更新 `production_product_units`：`unit_status=REWORK`、`current_operation_sequence=targetOperationSequence`。当前站点、进入工序时间和完成时间保持不变；序列号不存在时更新零行。
4. 更新后读取产品单元的 `work_order_number`；查不到时使用空字符串。
5. C++ 当前 `production_execution_events` INSERT 明确列出 11 列，但 `VALUES` 只有 10 个占位符，同时传入 11 个参数。SQLite 因列/值数量不匹配拒绝该语句，而 C++ 忽略错误继续执行。Python 故意保留同一 SQL 形态，因此该 URL 当前不会写入 `REWORK` execution event；集成测试已锁定这一可观察兼容行为。
6. 插入 `trace_events`：工厂固定 `PLANT-A`、事件 `REWORK`、资源类型 `SERIAL_NUMBER`、资源代码为序列号。未知序列号的追溯记录使用空工单号。
7. 插入 `audit_events`：`action=REWORK`、`resource_type=PRODUCT_UNIT`、`before_data={}`、`after_data` 为完整请求 JSON；操作人来自 Header 或默认值。
8. 插入 `idempotency_keys`：HTTP 状态 `200`、资源为序列号、消息为 `rework`，随后提交事务。

与 C++ 一致，第 2 至第 8 步的写入及 commit 返回值不影响最终成功响应。生产产品若要修复第 5 步的事件缺失，应作为明确的兼容性变更处理，而不是在 1:1 迁移中静默修复。

## `POST /api/v1/executions/scrap`

### 功能

创建报废记录，并尝试把产品单元置为 `SCRAPPED`。请求包含非空工单号时，还会增加工单拒收数并写入发往 ERP 的报废 outbox；无论是否包含工单号，都会记录追溯、审计和幂等投影。接口不检查角色、产品单元是否存在、当前状态是否允许报废或请求工单是否属于该序列号。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空；相同键重试直接返回首次保存的序列号与 `cached=true`。 |
| `X-Correlation-Id` | string | 否 | 省略或为空时生成 `corr-...`；用于执行事件写入尝试、审计和响应。 |
| `X-Actor-Id` | string | 否 | 操作人；也接受 `X-User-Id`，省略时为 `system`。写入报废记录和审计。 |
| `X-Actor-Role` | string | 否 | 处理器不读取，不做授权。 |
| `Content-Type` | string | 否 | 不检查媒体类型，建议为 `application/json`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `serialNumber` | string | 是 | 否 | 必须为非空字符串。接口不验证序列号存在；未知序列号仍会创建报废记录并返回成功。 |
| `scrapCode` | string | 否 | 否 | 报废代码；省略、`null` 或非字符串时写入空字符串。 |
| `disposition` | string | 否 | 否 | 处置方式；省略、空字符串、`null` 或非字符串时回退为 `DISPOSE`，其他非空字符串原样写入，不做枚举校验。 |
| `workOrderNumber` | string | 否 | 否 | 省略、`null` 或非字符串时为空字符串。只有非空时才增加工单拒收数、尝试写 execution event，并创建 ERP outbox；不验证工单存在或与序列号匹配。 |

```json
{
  "serialNumber": "SN-000001",
  "scrapCode": "SC-CRACK",
  "disposition": "DISPOSE",
  "workOrderNumber": "WO-20260801-001"
}
```

### 成功响应

首次处理固定返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.serialNumber` | string | 否 | 请求序列号。 |
| `data.status` | string | 否 | 固定 `SCRAPPED`；即使序列号不存在或事务内某项写入失败也不改变。 |
| `meta.correlationId` | string | 否 | 请求值或服务生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中也返回 `200 OK`，但 `data` 只包含 string `serialNumber` 和 boolean `cached=true`。命中发生在 JSON 解析和必填校验之前。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 幂等键为空、JSON 无法解析，或 `serialNumber` 缺失/为空/不是字符串。 |
| `500` | `DATABASE_ERROR` | Python 在幂等查询或事务开启失败时返回数据库错误。事务开始后的 SQL 和 commit 错误按 C++ 行为忽略。 |

当前没有 `404`、`409` 或 `403`：未知序列号、未知/不匹配工单、任意当前产品状态和任意角色均可返回业务成功。

### 数据库访问与状态变化

1. 查询 `idempotency_keys`，命中后不解析请求体。
2. 开启事务并插入 `production_scrap_records`：保存 `SCR-...` 标识、序列号、报废代码、处置方式、请求工单号和 Header 操作人。该表不对序列号或工单建立外键，空字符串和未知标识均可保存。
3. 按序列号更新 `production_product_units`：`unit_status=SCRAPPED`、`completed_at=当前 UTC 时间`；工序序号和当前站点保持不变。未知序列号时更新零行。
4. `workOrderNumber` 非空时，按请求工单号执行 `rejected_quantity+1`。工单不存在时更新零行，但仍继续后续 outbox。
5. C++ 当前 SCRAP execution event INSERT 列出 12 列，却只有 11 个 `VALUES` 占位符并绑定 12 个参数，SQLite 会拒绝该语句；错误被忽略。因此当前 URL 实际不会写入 `production_execution_events`。Python 保留相同 SQL 和可观察行为，独立测试验证 event 数量为零。
6. `workOrderNumber` 非空时始终尝试写 `integration_outbox_messages`：aggregate 为 `WORK_ORDER`/请求工单号，事件为 `work_order.scrap`，目标为 `ERP`，状态为 `PENDING`，payload 只含序列号和报废代码。即使工单不存在也没有外键阻止 outbox。
7. 插入 `trace_events`：工厂固定 `PLANT-A`、事件 `SCRAP`、资源类型 `SERIAL_NUMBER`。未提供工单时追溯的工单号为空字符串。
8. 插入 `audit_events`：`action=SCRAP`、`resource_type=PRODUCT_UNIT`、`before_data={}`、`after_data` 为完整请求 JSON。
9. 插入 `idempotency_keys`：HTTP 状态 `200`、资源为序列号、消息 `scrapped`，然后提交事务。

与 C++ 一致，第 2 至第 9 步的写入及 commit 返回值不影响最终响应。第 5 步事件缺失若要修复，应单独评估兼容性、下游报表和重复事件影响。

## `POST /api/v1/quality/inspection-lots`

### 功能

按物料匹配最新生效的检验计划，读取该计划的抽样数量并创建 `PENDING` 检验批，同时写入审计和幂等投影。仅 `QUALITY_ENGINEER` 或 `MES_ADMIN` 可调用。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空。相同键重试返回首次生成的检验批 ID 与 `cached=true`。 |
| `X-Correlation-Id` | string | 否 | 省略或为空时生成 `corr-...`；写入审计和响应。 |
| `X-Actor-Role` | string | 条件必传 | 必须为 `QUALITY_ENGINEER` 或 `MES_ADMIN`；别名为 `X-Role`。省略时默认 `MES_OPERATOR` 并返回 `403`。角色校验发生在幂等查询之前，因此无权角色不能读取已缓存结果。 |
| `X-Actor-Id` | string | 否 | 审计操作人；支持 `X-User-Id`，默认 `system`。 |
| `Content-Type` | string | 否 | 不检查媒体类型，建议为 `application/json`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `materialCode` | string | 业务必传 | 否 | 处理器没有单独的 422 校验，但会按该字符串匹配 `EFFECTIVE` 检验计划；省略、空、`null` 或非字符串时通常因无计划返回 `404`。 |
| `plantCode` | string | 否 | 否 | 省略、空、`null` 或非字符串时使用 `PLANT-A`；非空值原样写入检验批。计划匹配不按请求工厂过滤，检验批表也不对工厂建立外键。 |
| `workOrderNumber` | string | 否 | 否 | 省略、`null` 或非字符串时写入空字符串；不验证工单存在或匹配。 |
| `serialNumber` | string | 否 | 否 | 省略、`null` 或非字符串时写入空字符串；不验证产品单元。 |
| `inspectionType` | string | 否 | 否 | 省略、空、`null` 或非字符串时使用 `PATROL`；其他非空字符串原样写入。检验批表没有类型 CHECK，因此 `CUSTOM` 等值也会成功。 |

```json
{
  "plantCode": "PLANT-A",
  "materialCode": "MAT-PROD-01",
  "workOrderNumber": "WO-20260801-001",
  "serialNumber": "SN-000001",
  "inspectionType": "PATROL"
}
```

### 成功响应

首次处理返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.lotId` | string | 否 | 服务生成的 `ILOT-...` 检验批 ID。 |
| `data.status` | string | 否 | 固定 `PENDING`。 |
| `data.sampleSize` | number | 否 | 匹配计划的抽样规则数量；没有抽样规则时为 `1`。 |
| `meta.correlationId` | string | 否 | 请求值或服务生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中返回 `200 OK`，`data` 只含 string `lotId` 和 boolean `cached=true`，不再返回状态或样本数量。调用者仍须提供允许的角色，因为授权发生在缓存查询之前。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 幂等键为空或 JSON 无法解析。 |
| `403` | `FORBIDDEN` | 有效角色不是 `QUALITY_ENGINEER` 或 `MES_ADMIN`；`error.message` 固定为 `QUALITY_ENGINEER required`。 |
| `404` | `NOT_FOUND` | 按 `materialCode` 找不到 `status=EFFECTIVE` 的检验计划；计划查询失败也按 C++ `queryOne` 语义表现为此响应。 |
| `500` | `DATABASE_ERROR` | Python 在幂等查询或事务开启失败时返回数据库错误。事务开始后的写入错误按 C++ 行为忽略。 |

### 数据库访问与状态变化

1. 依次执行写请求 Header 校验、角色校验和 `idempotency_keys` 查询。缓存命中后不解析 Body。
2. 按 `quality_inspection_plans.material_code` 查询 `EFFECTIVE` 计划，不按工厂、工序、检验类型或有效截止时间过滤；按 `effective_from DESC` 取一条。
3. 相关子查询从 `quality_sampling_rules` 取该计划代码/版本的第一条 `sample_size`，没有记录时 `COALESCE` 为 `1`。子查询没有 `ORDER BY`，若存在多条规则，具体第一条依赖 SQLite 查询顺序。
4. 生成 `ILOT-...` ID 并开启事务，插入 `quality_inspection_lots`：使用请求/默认工厂与类型、请求物料/工单/序列号、匹配计划及样本数，状态固定 `PENDING`。
5. 检验计划表对工厂和物料有外键，计划 `inspection_type` 有枚举 CHECK；但检验批表的 `plant_code`、`material_code`、`inspection_type` 没有对应外键或枚举 CHECK。因此只要物料能先匹配到计划，请求中的未知工厂或自定义检验类型仍可写入检验批，独立测试已覆盖。
6. 插入 `audit_events`：`action=INSPECTION_LOT_CREATE`、`resource_type=INSPECTION_LOT`、`before_data={}`、`after_data` 为完整请求 JSON。
7. 插入 `idempotency_keys`：状态 `200`、资源为检验批 ID、消息 `lot created`，随后提交事务。

与 C++ 一致，第 4 至第 7 步的 SQL 与 commit 返回值不影响最终响应：若检验批插入因约束失败，审计与幂等写入仍可能成功，接口仍返回生成的批 ID、`PENDING` 和样本数。

## `POST /api/v1/quality/inspections`

### 功能

提交检验结果并更新检验批。`ACCEPTED` 可放行处于 `WAITING_INSPECTION` 的产品单元并推进下一工序或完成产品/工单；`REJECTED` 自动创建不合格项、把产品置为 `HOLD` 并拉起质量安灯。接口还写入质量报表、追溯、审计和幂等投影。仅 `QUALITY_ENGINEER` 或 `MES_ADMIN` 可调用。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空；相同键返回首次生成的检验 ID 与 `cached=true`。 |
| `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`；写入审计和响应。 |
| `X-Actor-Role` | string | 条件必传 | 必须为 `QUALITY_ENGINEER` 或 `MES_ADMIN`；支持 `X-Role`。省略时默认 `MES_OPERATOR` 并返回 `403`。角色校验在幂等查询之前。 |
| `X-Actor-Id` | string | 否 | 检验员和审计操作人；支持 `X-User-Id`，默认 `system`。 |
| `Content-Type` | string | 否 | 不检查媒体类型，建议为 `application/json`。 |

### JSON Body

C++ 处理器没有任何 Body 字段必填校验；下表字段全部可省略，但缺失值可能使某些数据库写入失败，而接口仍返回成功。

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `serialNumber` | string | 否 | 否 | 省略、`null` 或非字符串时为空字符串；用于检验结果、产品状态查询/更新和追溯。 |
| `disposition` | string | 否 | 否 | 原样使用，默认空字符串。结果表允许 `PENDING`、`ACCEPTED`、`REJECTED`、`HOLD`；其他值会使结果/检验批状态写入失败，但响应仍返回该字符串。 |
| `workOrderNumber` | string | 否 | 否 | 默认空字符串；写入结果与追溯。产品放行时实际更新的工单取自产品单元，而不是该请求字段。 |
| `inspectionLotId` | string | 否 | 否 | 默认空字符串。非空时尝试更新对应检验批状态；结果表对该字段有外键，空字符串或未知 ID 会使检验结果 INSERT 失败。 |
| `inspectionPlanCode` | string | 否 | 否 | 省略、空、`null` 或非字符串时使用 `IP-01`；非空值原样写入，不查询计划。 |
| `operationSequence` | number/string | 否 | 否 | 整数直接使用、小数截断、整数字符串转换；省略、`null`、无效值或其他类型为 `0`。仅写入检验结果；产品推进使用产品单元自己的当前工序。 |
| `defectCode` | string | 否 | 否 | 默认空字符串。`REJECTED` 自动不合格项在为空时使用 `DEF-UNKNOWN`。 |

完整请求 JSON 还会保存到 `quality_inspection_results.payload` 和审计 `after_data`，其他字段不参与业务逻辑。

### 处置行为

| `disposition` | 条件 | `unitDisposition` | 产品/工单变化 |
| --- | --- | --- | --- |
| `ACCEPTED` | 产品存在且状态为 `WAITING_INSPECTION`，并有下一工序 | `IN_PROCESS` | 产品移动到下一工序并更新时间；仍会创建 `work_order.completion` outbox。 |
| `ACCEPTED` | 产品存在且为 `WAITING_INSPECTION`，无下一工序 | `PASSED` | 产品完成，工单完成数量 `+1`；若所有工序均 `COMPLETED/SKIPPED`，工单置 `COMPLETED`；创建 completion outbox。 |
| `ACCEPTED` | 产品存在但不是 `WAITING_INSPECTION` | 产品当前状态 | 不修改产品，也不创建 outbox。 |
| `ACCEPTED` | 产品不存在 | 空字符串 | 不修改产品，也不创建 outbox。 |
| `REJECTED` | 任意 | `HOLD` | 尝试创建 MAJOR/OPEN 不合格项，把匹配序列号产品置为 `HOLD`，创建 CRITICAL/QUALITY 安灯。 |
| 其他值（包括 `HOLD`、`PENDING`、空字符串） | 任意 | 请求处置字符串 | 不修改产品单元。`HOLD` 响应并不自动把产品置为 HOLD。 |

### 成功响应

首次处理固定返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.inspectionId` | string | 否 | 生成的 `INS-...` 标识；即使结果 INSERT 失败仍返回。 |
| `data.lotStatus` | string | 否 | 请求 `disposition` 或空字符串。 |
| `data.unitDisposition` | string | 否 | 按上表计算，可能为空字符串。 |
| `meta.correlationId` | string | 否 | 请求值或服务生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中返回 `200 OK`，`data` 只包含 string `inspectionId` 和 boolean `cached=true`。调用者仍必须通过角色校验。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 幂等键为空或 JSON 无法解析。不存在 Body 字段级 422。 |
| `403` | `FORBIDDEN` | 有效角色不是 `QUALITY_ENGINEER` 或 `MES_ADMIN`。 |
| `500` | `DATABASE_ERROR` | Python 在幂等查询或事务开启失败时返回；事务开始后的 SQL/commit 错误被忽略。 |

接口没有 `404` 或 `409`：未知检验批、未知产品、未知工单、非法处置等均可能仍返回 `200`。

### 数据库访问与状态变化

1. Header 和角色校验后查询 `idempotency_keys`；缓存命中不解析 Body。
2. 生成 `INS-...` 并开启事务，插入 `quality_inspection_results`：工厂固定 `PLANT-A`，检验员来自 Header，payload 为完整请求。非空 `inspectionLotId` 时再按 ID更新 `quality_inspection_lots.status`。
3. `ACCEPTED` 时查询 `production_product_units`。仅 `WAITING_INSPECTION` 产品会查询下一道 `production_work_order_operations` 并推进；末工序会更新产品、工单完成数量，按剩余工序数量决定工单是否完成。只要进入了该放行分支，无论是否还有下一工序，都写发往 ERP 的 `work_order.completion` outbox。
4. `REJECTED` 时插入 `quality_nonconformances`：工厂 `PLANT-A`、严重度 `MAJOR`、处置 `OPEN`、影响数量 `1`；更新产品 `HOLD`，并插入 CRITICAL/QUALITY `trace_andon_events`。
5. 更新当天已有的所有 `reporting_quality_shift_summary` 工厂行：`inspection_count+1`，并按请求处置增加 accepted/rejected/hold。不存在当天行时不自动插入；即使结果 INSERT 失败也仍增加检验次数。
6. 插入 `INSPECTION`/`SERIAL_NUMBER` 追溯，工单号使用请求字段；插入 `INSPECTION_SUBMIT`/`INSPECTION` 审计。
7. 插入幂等记录：状态 `200`、资源为检验 ID、消息为请求处置，然后提交。

事务内所有写入、查询失败的默认值及 commit 结果都保持 C++ 的宽松可观察语义。独立测试包含空 Body：检验结果因约束未落库，但质量报表、追溯、审计、幂等和成功响应仍继续产生。

## `POST /api/v1/quality/nonconformances`

### 功能

创建质量不合格项（NC）及序列号隔离记录；请求带非空资源代码时把对应产品单元置为 `HOLD`，并始终尝试创建质量安灯、审计和幂等投影。仅 `QUALITY_ENGINEER` 或 `MES_ADMIN` 可调用。处理器不做 Body 必填校验。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空；相同键返回首次生成的 NC ID 与 `cached=true`。 |
| `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`；用于审计和响应。 |
| `X-Actor-Role` | string | 条件必传 | 必须为 `QUALITY_ENGINEER` 或 `MES_ADMIN`；支持别名 `X-Role`。角色校验发生在幂等查询前。 |
| `X-Actor-Id` | string | 否 | 审计操作人；支持 `X-User-Id`，默认 `system`。 |
| `Content-Type` | string | 否 | 不检查媒体类型，建议为 `application/json`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `plantCode` | string | 否 | 否 | NC 工厂；省略、空、`null` 或非字符串时为 `PLANT-A`。只写 NC；安灯工厂始终固定 `PLANT-A`。 |
| `sourceInspectionId` | string | 否 | 否 | 源检验 ID，默认空字符串。NC 表有外键；空或未知 ID 会使 NC INSERT 失败，但接口继续执行。 |
| `severity` | string | 否 | 否 | 非空时同时用于 NC 和安灯。省略时 NC 默认为 `MAJOR`，安灯默认为 `WARNING`。NC 仅允许 `MINOR/MAJOR/CRITICAL`，安灯仅允许 `INFO/WARNING/CRITICAL`，因此显式 `MINOR`/`MAJOR` 会使安灯失败，显式 `INFO`/`WARNING` 会使 NC 失败。 |
| `defectCode` | string | 否 | 否 | 缺陷代码，默认空字符串；空字符串满足 NOT NULL，不做业务校验。 |
| `disposition` | string | 否 | 否 | NC 处置；默认 `OPEN`，允许 `OPEN/REWORK/SCRAP/USE_AS_IS/RETURN/CLOSED`。成功响应的状态不读取该字段，始终为 `OPEN`。 |
| `affectedQuantity` | number | 否 | 否 | 影响数量；仅 JSON number 有效，省略、`null`、字符串或其他类型为 `1.0`。表没有正数 CHECK。 |
| `resourceCode` | string | 否 | 否 | 被隔离的序列号，默认空字符串。隔离记录资源类型固定 `SERIAL_NUMBER`；非空时尝试把同序列号产品置为 `HOLD`。 |

```json
{
  "plantCode": "PLANT-A",
  "sourceInspectionId": "INS-...",
  "defectCode": "DEF-CRACK",
  "affectedQuantity": 2.5,
  "resourceCode": "SN-000001"
}
```

### 成功响应

首次处理固定返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.nonconformanceId` | string | 否 | 生成的 `NC-...` ID；即使 NC INSERT 失败仍返回。 |
| `data.status` | string | 否 | 固定 `OPEN`，不反映请求 `disposition` 或数据库实际落库结果。 |
| `meta.correlationId` | string | 否 | 请求值或服务生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中返回 `200 OK`，`data` 只包含 string `nonconformanceId` 和 boolean `cached=true`；仍须通过角色校验。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 幂等键为空或 JSON 无法解析；不存在 Body 字段级校验。 |
| `403` | `FORBIDDEN` | 有效角色不是 `QUALITY_ENGINEER` 或 `MES_ADMIN`。 |
| `500` | `DATABASE_ERROR` | Python 在幂等查询或事务开启失败时返回；事务内 SQL 与 commit 错误被忽略。 |

无 `404`/`409`：源检验、资源产品不存在或约束值非法时仍可能返回 `200`。

### 数据库访问与状态变化

1. Header/角色校验后查询 `idempotency_keys`；缓存命中不解析 Body。
2. 生成 `NC-...` 并插入 `quality_nonconformances`。`source_inspection_id` 外键指向 `quality_inspection_results`；空字符串不是 SQL NULL，因此无源检验时该写入失败。
3. 插入 `quality_quarantine_records`：字段名沿用 C++ schema 的 `nonconference_id` 拼写，引用本次 NC，资源类型固定 `SERIAL_NUMBER`。若 NC 未落库，外键使隔离记录也失败。
4. `resourceCode` 非空时独立更新 `production_product_units.unit_status=HOLD`。这一步不依赖 NC/隔离记录成功；测试已验证源检验缺失时 NC 和隔离均失败，产品仍会进入 HOLD。
5. 插入 `trace_andon_events`：工厂固定 `PLANT-A`、类别 `QUALITY`、资源类型 `NONCONFORMANCE`、资源代码为生成的 NC ID、消息固定 `nonconformance created`。安灯表不对 NC 建外键，因此 NC 失败时安灯仍可存在。
6. 插入 `NC_CREATE`/`NONCONFORMANCE` 审计，`before_data={}`、`after_data` 为完整请求；插入状态 `200`、消息 `nc created` 的幂等记录并提交。

所有事务内写入与 commit 返回值均按 C++ 实际行为忽略。因此 `data.status=OPEN` 只表示处理器的固定响应，不保证 NC、隔离、产品 HOLD 和安灯全部成功。

## `POST /api/v1/quality/capas`

### 功能

为不合格项创建纠正与预防措施（CAPA）案件，初始状态固定 `OPEN`，并写入审计和幂等投影。仅 `QUALITY_ENGINEER` 或 `MES_ADMIN` 可调用。处理器不校验任何 Body 字段。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空；相同键返回首次生成的 CAPA ID 与 `cached=true`。 |
| `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`；写入审计和响应。 |
| `X-Actor-Role` | string | 条件必传 | 必须为 `QUALITY_ENGINEER` 或 `MES_ADMIN`；支持 `X-Role`。角色校验在幂等查询之前。 |
| `X-Actor-Id` | string | 否 | 审计操作人；支持 `X-User-Id`，默认 `system`。与 CAPA `ownerId` 相互独立。 |
| `Content-Type` | string | 否 | 不检查媒体类型，建议为 `application/json`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `plantCode` | string | 否 | 否 | 省略、空、`null` 或非字符串时为 `PLANT-A`。CAPA 表不对工厂建立外键，未知工厂可写入。 |
| `nonconformanceId` | string | 否 | 否 | 默认空字符串。CAPA 表有 NC 外键；空或未知 ID 会使 CAPA INSERT 失败，但接口继续返回成功。 |
| `ownerId` | string | 否 | 否 | CAPA 负责人，默认空字符串；表只要求 NOT NULL，不校验人员主数据，空字符串可保存。 |
| `rootCause` | string | 否 | 否 | 根因，默认空字符串；原样写入。 |
| `correctiveAction` | string | 否 | 否 | 纠正措施，默认空字符串；原样写入。 |
| `dueAt` | string | 否 | 否 | 到期时间，默认空字符串；不解析或校验时间格式。 |

```json
{
  "plantCode": "PLANT-A",
  "nonconformanceId": "NC-...",
  "ownerId": "W-002",
  "rootCause": "材料裂纹",
  "correctiveAction": "更换供应批次",
  "dueAt": "2026-09-01T00:00:00Z"
}
```

### 成功响应

首次处理固定返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.capaId` | string | 否 | 生成的 `CAPA-...` ID；即使 CAPA INSERT 失败仍返回。 |
| `data.status` | string | 否 | 固定 `OPEN`。 |
| `meta.correlationId` | string | 否 | 请求值或服务生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

幂等命中返回 `200 OK`，`data` 只含 string `capaId` 和 boolean `cached=true`；仍须通过角色校验。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 幂等键为空或 JSON 无法解析；没有 Body 字段级校验。 |
| `403` | `FORBIDDEN` | 有效角色不是 `QUALITY_ENGINEER` 或 `MES_ADMIN`。 |
| `500` | `DATABASE_ERROR` | Python 在幂等查询或事务开启失败时返回；事务内 SQL/commit 错误被忽略。 |

没有 `404`/`409`：未知 NC、未知工厂、空负责人或无效时间格式仍可能返回 `200`。

### 数据库访问与状态变化

1. Header/角色校验后查询 `idempotency_keys`；命中后不解析 Body。
2. 生成 `CAPA-...` 并开启事务，插入 `quality_capa_cases`：状态固定 `OPEN`，`closed_at` 保持 NULL；请求字符串原样写入。
3. `nonconformance_id` 外键引用 `quality_nonconformances`。处理器把缺失值绑定为空字符串而非 SQL NULL，因此空 Body 时 CAPA INSERT 失败。工厂和负责人没有外键，未知工厂、未知/空负责人不会单独阻止写入。
4. 插入 `CAPA_CREATE`/`CAPA` 审计：`before_data={}`、`after_data` 为完整请求 JSON。
5. 插入幂等记录：状态 `200`、资源为 CAPA ID、消息 `capa created`，随后提交。

与 C++ 一致，第 2 至第 5 步返回值不影响成功响应。独立测试验证空 Body 的 CAPA 未落库，但审计和幂等记录仍存在，响应仍为 `OPEN`。

## `GET /api/v1/quality/spc/charts`

### 功能

读取最新 100 条 SPC 测量值，可按质量特性代码精确过滤。接口不分页、不做角色或认证校验，也不计算控制限；控制限和违规规则直接来自已保存的测量记录。

### 请求

| 位置 | 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- | --- |
| Header | `X-Correlation-Id` | string | 否 | 原样写入内外两层响应元数据；省略时生成 `corr-...`。其他 Header 不读取。 |
| Query | `characteristicCode` | string | 否 | 出现时按 `characteristic_code` 精确匹配，区分大小写；显式传空字符串会筛选空代码，而不是取消过滤。 |
| Body | 无 | - | 否 | 请求体会被忽略。 |

```bash
curl -i 'http://127.0.0.1:8080/api/v1/quality/spc/charts?characteristicCode=DIM-L'
```

### 成功响应

固定返回 `200 OK`。C++ 实现先调用 `listEnvelope`，再把结果传给 `respondOk`，因此实际 JSON 是双层 envelope；Python 保留该行为：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.data` | array | 否 | 按 `measuredAt` 降序排列，最多 100 条；无记录时为 `[]`。 |
| `data.data[].measurementId` | string | 否 | SPC 测量 ID。 |
| `data.data[].characteristicCode` | string | 否 | 质量特性代码。 |
| `data.data[].value` | number | 否 | 实测值。 |
| `data.data[].cl` | number/null | 是 | 中心线。 |
| `data.data[].ucl` | number/null | 是 | 控制上限。 |
| `data.data[].lcl` | number/null | 是 | 控制下限。 |
| `data.data[].usl` | number/null | 是 | 规格上限。 |
| `data.data[].lsl` | number/null | 是 | 规格下限。 |
| `data.data[].violationRule` | string | 否 | `violation_rule` 为 NULL 时转换为空字符串，否则为规则代码。 |
| `data.data[].inControl` | boolean | 否 | 仅以 `violation_rule` 是否为 NULL 判断；NULL 为 `true`，非 NULL（包括空字符串）为 `false`。 |
| `data.data[].measuredAt` | string | 否 | 数据库中的测量时间字符串。 |
| `data.meta.page` | number | 否 | 固定为 `1`。 |
| `data.meta.pageSize` | number | 否 | 固定为 `100`。 |
| `data.meta.total` | number | 否 | 本次实际返回条数，不是过滤条件下的全表总数。 |
| `data.meta.correlationId` | string | 否 | 请求值或服务生成值。 |
| `data.meta.generatedAt` | string | 否 | 内层列表生成时间。 |
| `meta.correlationId` | string | 否 | 与内层关联 ID 相同。 |
| `meta.generatedAt` | string | 否 | 外层响应生成时间。 |

```json
{
  "data": {
    "data": [
      {
        "measurementId": "SPC-001",
        "characteristicCode": "DIM-L",
        "value": 10.01,
        "cl": 10.0,
        "ucl": 10.04,
        "lcl": 9.96,
        "usl": 10.05,
        "lsl": 9.95,
        "violationRule": "",
        "inControl": true,
        "measuredAt": "2026-08-21T00:00:00Z"
      }
    ],
    "meta": {
      "page": 1,
      "pageSize": 100,
      "total": 1,
      "correlationId": "spc-list",
      "generatedAt": "2026-08-21T00:00:01Z"
    }
  },
  "meta": {
    "correlationId": "spc-list",
    "generatedAt": "2026-08-21T00:00:01Z"
  }
}
```

### 数据库访问与错误响应

从 `quality_spc_measurements` 读取所列字段；存在查询参数时追加 `AND characteristic_code=?`，随后固定追加 `ORDER BY measured_at DESC LIMIT 100`。接口不写数据库、不启动显式事务。

C++ 的正常业务路径没有错误响应。Python 在数据库查询失败时返回 `500 DATABASE_ERROR`；正常空结果仍返回 `200` 和空数组。

## `POST /api/v1/equipment/{equipmentCode}/events`

### 功能

接收设备状态事件，更新设备当前状态和心跳；可选记录产量计数并累加当前小时 OEE。`FAULT` 事件还会自动创建纠正维修单和关键设备安灯。接口不校验角色。

### 请求 Header 与 Path

| 位置 | 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- | --- |
| Header | `X-Idempotency-Key` | string | 是 | 非空；幂等命中发生在 JSON、路径设备和状态校验之前。 |
| Header | `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`。C++ 的错误文案称其必传，但实际生成逻辑使其不会为空。 |
| Header | `X-Actor-Id` | string | 否 | 审计操作人；支持 `X-User-Id`，默认 `system`。 |
| Path | `equipmentCode` | string | 是 | 必须对应 `asset_equipment` 中已有设备；幂等命中时忽略。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `status` | string | 是 | 否 | 非空，且数据库只接受 `OFFLINE`、`IDLE`、`RUNNING`、`FAULT`、`MAINTENANCE`。未知非空值在 INSERT 时返回 `409`。 |
| `reasonCode` | string | 否 | 否 | 默认空字符串，写入事件。 |
| `occurredAt` | string | 否 | 否 | 空、缺失或非字符串时使用服务当前 UTC 时间；非空字符串不解析格式。 |
| `sourceEventId` | string | 否 | 否 | 空、缺失或非字符串时使用幂等键；数据库全局唯一。 |
| `counterCode` | string | 否 | 否 | 非空时触发计数器写入；若仅传 `counterValue`，代码默认 `OUT`。 |
| `counterValue` | number | 否 | 否 | 字段存在即触发计数器写入；仅 JSON number 被读取，其他类型按 `0`。 |

```json
{
  "status": "RUNNING",
  "reasonCode": "START",
  "occurredAt": "2026-08-21T01:02:03Z",
  "sourceEventId": "PLC-EVENT-001",
  "counterValue": 5.5
}
```

### 成功与幂等响应

首次接收返回 `202 Accepted`：

```json
{
  "data": {"accepted": true},
  "meta": {
    "correlationId": "equipment-event-running",
    "generatedAt": "2026-08-21T01:02:04Z"
  }
}
```

幂等命中改为返回 `200 OK`，`data` 为 `{"eventId":"EVT-...","cached":true}`。注意 C++ 向 `asset_equipment_events` 写入的是另一个 `EEQ-...` ID，却在幂等表保存新生成的 `EVT-...` ID；因此缓存响应的 `eventId` 不能用于查询实际事件。Python 保留这一实际行为。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 幂等键为空、JSON 无法解析，或 `status` 缺失/空/非字符串。 |
| `404` | `NOT_FOUND` | 路径设备不存在。 |
| `409` | `CONFLICT` | `sourceEventId` 重复，或事件 INSERT 的其他约束失败（包括未知状态）；消息固定为 `duplicate sourceEventId`。 |
| `500` | `DATABASE_ERROR` | Python 在幂等查询或事务开启失败时返回。 |

### 数据库访问与状态变化

1. 查询 `idempotency_keys`；命中即返回缓存结果。
2. 查询 `asset_equipment` 的工厂和当前状态；开启事务并插入 `asset_equipment_events`。只有该 INSERT 的结果会被检查，失败时回滚。
3. 更新设备 `current_status` 和 `last_heartbeat_at`。
4. 出现非空 `counterCode` 或任意类型的 `counterValue` 字段时，插入 `asset_equipment_counters`；按服务当前小时执行 `reporting_equipment_oee_hourly` UPDATE，将 `actual_output`、`good_output` 加上计数值，`running_seconds` 加 `1`。若小时桶不存在，不会自动插入。
5. `status=FAULT` 时插入 `CORRECTIVE/OPEN` 的 `asset_maintenance_work_orders`，插入 `CRITICAL/EQUIPMENT` 安灯，并尝试累加当天已有的 `reporting_andon_summary`；汇总行不存在时不创建。
6. 写入 `EQUIPMENT_EVENT` 审计和状态 `202`、消息 `accepted`、资源 `EVT-...` 的幂等记录后提交。

与 C++ 一致，第 3 至第 6 步的 SQL 和 commit 结果不会改变 `202` 响应；只有设备事件主记录 INSERT 失败会显式回滚并返回错误。

## `GET /api/v1/equipment/{equipmentCode}/state`

### 功能

返回设备主数据中的当前状态、最近心跳距当前时间的秒数，以及尚未完成或取消的维修工单数量。接口不做角色或认证校验。

### 请求

| 位置 | 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- | --- |
| Header | `X-Correlation-Id` | string | 否 | 原样写入响应；省略时生成 `corr-...`。其他 Header 不读取。 |
| Path | `equipmentCode` | string | 是 | 按设备代码精确查询。 |
| Query | 无 | - | 否 | 查询参数会被忽略。 |
| Body | 无 | - | 否 | 请求体会被忽略。 |

### 成功响应

设备存在时返回 `200 OK`：

| JSON 路径 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `data.equipmentCode` | string | 否 | 设备代码。 |
| `data.plantCode` | string | 否 | 所属工厂。 |
| `data.status` | string | 否 | `OFFLINE`、`IDLE`、`RUNNING`、`FAULT` 或 `MAINTENANCE`。 |
| `data.lastHeartbeatAt` | string | 否 | 数据库值；NULL 转为空字符串。 |
| `data.heartbeatAgeSeconds` | number | 否 | SQLite 当前 Unix 秒减去心跳 Unix 秒并截为整数；无心跳或时间无法解析时为 `0`。未来心跳可能产生负数。 |
| `data.openMaintenanceCount` | number | 否 | 该设备状态不为 `COMPLETED`、`CANCELLED` 的维修单数量。 |
| `meta.correlationId` | string | 否 | 请求值或服务生成值。 |
| `meta.generatedAt` | string | 否 | UTC RFC 3339 时间。 |

```json
{
  "data": {
    "equipmentCode": "EQ-ASSY-01",
    "plantCode": "PLANT-A",
    "status": "MAINTENANCE",
    "lastHeartbeatAt": "2026-08-21T01:00:00Z",
    "heartbeatAgeSeconds": 120,
    "openMaintenanceCount": 2
  },
  "meta": {
    "correlationId": "equipment-state",
    "generatedAt": "2026-08-21T01:02:00Z"
  }
}
```

### 数据库访问与错误响应

查询视图 `v_equipment_current_state`。视图从 `asset_equipment` 读取状态，通过 SQLite `strftime('%s', ...)` 计算心跳年龄，并以相关子查询统计 `asset_maintenance_work_orders`。

设备不存在时返回 `404 NOT_FOUND`，消息为 `equipment not found`。与 C++ `queryOne` 的实际行为一致，Python 也把该查询的数据库错误映射为同一个 `404`。接口不写数据库。

## `POST /api/v1/maintenance-work-orders`

### 功能

创建初始状态为 `OPEN` 的设备维修工单，并写入审计和幂等投影。仅 `EQUIPMENT_ENGINEER` 或 `MES_ADMIN` 可调用。C++ 处理器不做任何 Body 字段级校验。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空；角色校验通过后，命中时返回首次生成的维修单 ID。 |
| `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`。 |
| `X-Actor-Role` | string | 条件必传 | 必须为 `EQUIPMENT_ENGINEER` 或 `MES_ADMIN`；支持 `X-Role`。角色校验发生在幂等查询之前。 |
| `X-Actor-Id` | string | 否 | 审计操作人；支持 `X-User-Id`，默认 `system`。 |

### JSON Body

| 参数 | 类型 | 必传 | 可空 | 说明 |
| --- | --- | --- | --- | --- |
| `equipmentCode` | string | 否 | 否 | 默认空字符串；设备外键不存在或为空会使主记录 INSERT 失败，但接口继续成功。 |
| `maintenanceType` | string | 否 | 否 | 空、缺失、`null` 或非字符串时为 `CORRECTIVE`；数据库还接受 `PREVENTIVE`、`PREDICTIVE`。其他值会使 INSERT 失败。 |
| `description` | string | 否 | 否 | 默认空字符串，表允许。 |
| `assigneeId` | string | 否 | 否 | 默认空字符串；不校验人员主数据。 |
| `dueAt` | string | 否 | 否 | 默认空字符串；不解析时间格式。 |

```json
{
  "equipmentCode": "EQ-ASSY-01",
  "maintenanceType": "PREVENTIVE",
  "description": "更换润滑油",
  "assigneeId": "W-004",
  "dueAt": "2026-08-31T00:00:00Z"
}
```

### 成功与幂等响应

首次处理固定返回 `200 OK`，`data.maintenanceWorkOrderId` 是生成的 `MWO-...`，`data.status` 固定为 `OPEN`。即使维修单 INSERT 因外键或枚举约束失败，响应仍然如此。

幂等命中也返回 `200`，`data` 只包含 string `maintenanceWorkOrderId` 和 boolean `cached=true`；仍须先通过角色校验。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 幂等键为空或 JSON 无法解析。没有 Body 字段级 `422`。 |
| `403` | `FORBIDDEN` | 有效角色不是 `EQUIPMENT_ENGINEER` 或 `MES_ADMIN`。 |
| `500` | `DATABASE_ERROR` | Python 在幂等查询或事务开启失败时返回。 |

没有 `404` 或 `409`：未知设备、未知维修类型和空 Body 可能未创建主记录，但仍返回 `200`。

### 数据库访问与状态变化

1. 校验 Header/角色并查询 `idempotency_keys`。
2. 开启事务，尝试插入 `asset_maintenance_work_orders`：状态固定 `OPEN`，完成时间为 NULL。
3. 插入 `MAINTENANCE_CREATE`/`MAINTENANCE_WO` 审计和状态 `200`、消息 `created` 的幂等记录，然后提交。

与 C++ 一致，事务内三项 SQL及 commit 的返回值均被忽略。独立测试验证空 Body 时维修单主记录因空设备外键未落库，但审计、幂等和成功响应仍存在。

## `/api/v1/andons`

该 URL 提供安灯事件的创建和列表读取。创建不限制角色；列表默认只返回活动安灯。

### `POST /api/v1/andons`

#### 请求

| 位置/参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| Header `X-Idempotency-Key` | string | 是 | 非空；命中时返回首次生成的安灯 ID，不再解析 Body。 |
| Header `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`。 |
| Header `X-Actor-Id` | string | 否 | 审计操作人；支持 `X-User-Id`，默认 `system`。 |
| Body `plantCode` | string | 否 | 空、缺失、`null` 或非字符串时为 `PLANT-A`；不校验工厂主数据。 |
| Body `severity` | string | 否 | 默认 `WARNING`；数据库只接受 `INFO`、`WARNING`、`CRITICAL`。 |
| Body `category` | string | 否 | 默认 `QUALITY`；任意非空字符串可保存。 |
| Body `resourceType` | string | 否 | 默认空字符串。 |
| Body `resourceCode` | string | 否 | 默认空字符串。 |
| Body `relatedWorkOrderNumber` | string | 否 | 默认空字符串；不校验工单。 |
| Body `relatedSerialNumber` | string | 否 | 默认空字符串；不校验产品单元。 |
| Body `message` | string | 否 | 默认空字符串，数据库允许。 |

首次处理返回 `200 OK`，`data` 包含生成的 `andonId`、固定 `status=ACTIVE` 和响应时刻 `raisedAt`。幂等命中返回 `{"andonId":"AND-...","cached":true}`。

创建处理在同一事务中尝试：

1. 插入 `trace_andon_events`，`raised_at` 使用数据库默认当前时间。
2. 对当天已有的 `reporting_andon_summary` 执行 `raised_count+1`；汇总行不存在时不创建。
3. 插入 `ANDON_RAISE`/`ANDON` 审计和状态 `200`、消息 `raised` 的幂等记录后提交。

与 C++ 一致，事务内 SQL 和 commit 错误都不影响成功响应。比如未知 `severity` 会违反数据库约束，安灯主记录不落库，但审计和幂等记录仍可能保存。

错误仅包括：幂等键为空或 JSON 非法时的 `422 VALIDATION_ERROR`，以及 Python 在幂等查询/事务开启失败时的 `500 DATABASE_ERROR`。接口不做角色校验，不返回业务 `403/404/409`。

### `GET /api/v1/andons`

#### 请求与过滤

| 位置 | 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- | --- |
| Header | `X-Correlation-Id` | string | 否 | 写入内外两层元数据。 |
| Query | `status` | string | 否 | 省略时为 `ACTIVE`。值严格等于 `ACTIVE` 时只查未关闭安灯；任何其他值（包括空字符串）都查最近 100 条全部安灯，并不按该值过滤。 |

#### 成功响应

固定返回 `200 OK`。和其他 C++ 列表处理器一样，`listEnvelope` 又被 `respondOk` 包裹，因此实际为双层 envelope：记录数组位于 `data.data`，分页元数据位于 `data.meta`，外层还有 `meta`。

每条记录包含：

| JSON 字段 | 类型 | 说明 |
| --- | --- | --- |
| `andonId` | string | 安灯 ID。 |
| `plantCode` | string | 工厂代码。 |
| `severity` | string | 严重级别。 |
| `category` | string | 分类。 |
| `resourceType` | string | NULL 转空字符串。 |
| `resourceCode` | string | NULL 转空字符串。 |
| `message` | string | 消息。 |
| `raisedAt` | string | 提升时间。 |
| `acknowledgedAt` | string | NULL 转空字符串。 |
| `acknowledgedBy` | string | NULL 转空字符串。 |
| `durationSeconds` | number | 当前/关闭时间减提升时间；结果为 NULL 时转 `0`。 |
| `relatedWorkOrderNumber` | string | NULL 转空字符串。 |

C++ 虽然从数据库读取 `related_serial_number` 和非活动查询的 `closed_at`，但实际响应没有 `relatedSerialNumber` 或 `closedAt` 字段；Python保持该行为。

活动查询使用 `v_active_andons`，按 `CRITICAL`、`WARNING`、其他严重级别排序，同级按 `raised_at` 升序且没有 LIMIT。非活动查询直接读取 `trace_andon_events`，按 `raised_at` 降序限制 100 条，其中仍会包含尚未关闭的记录。两种模式的 `page=1`、`pageSize=100`，`total` 都是实际返回条数。

## `POST /api/v1/andons/{andonId}/close`

### 功能

关闭指定安灯，记录关闭人和时间，累加当天已有的安灯关闭汇总，并写入审计及幂等投影。允许角色为 `MES_SUPERVISOR`、`QUALITY_ENGINEER`、`EQUIPMENT_ENGINEER` 或 `MES_ADMIN`。

### 请求

| 位置 | 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- | --- |
| Header | `X-Idempotency-Key` | string | 是 | 非空；角色校验后命中即返回首次关闭的安灯 ID，路径和 Body 被忽略。 |
| Header | `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`。 |
| Header | `X-Actor-Role` | string | 条件必传 | 必须是四个允许角色之一；支持 `X-Role`，校验发生在幂等查询前。 |
| Header | `X-Actor-Id` | string | 否 | 写入 `closed_by` 和审计；支持 `X-User-Id`，默认 `system`。 |
| Path | `andonId` | string | 是 | 必须存在；不要求当前仍为活动状态。 |
| Body | 任意 JSON | 否 | 不读取业务字段，完整 JSON 仅写入审计 `after_data`；空 Body 视为 `{}`。 |

### 成功与幂等响应

首次处理返回 `200 OK`：`data.andonId` 为路径 ID，`data.status` 固定 `CLOSED`，`data.closedAt` 为响应时刻。数据库中的 `closed_at` 在 UPDATE 前单独生成，通常相同但不保证与响应字段完全一致。

幂等命中返回 `200`，`data` 仅含 `andonId` 和 `cached=true`。角色校验仍先执行，因此无权限调用者不能读取缓存结果。

### 错误响应

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 幂等键为空或 JSON 无法解析。 |
| `403` | `FORBIDDEN` | 角色不在允许集合，消息固定 `supervisor role required`。 |
| `404` | `NOT_FOUND` | 安灯不存在，消息 `andon not found`。Python 与 C++ 一样也把该查询错误映射为 `404`。 |
| `500` | `DATABASE_ERROR` | Python 在幂等查询或事务开启失败时返回。 |

### 数据库访问与重复关闭

1. 校验 Header/角色、查询幂等记录、解析 JSON，再读取安灯的 `plant_code/category/severity`。
2. 事务内更新 `trace_andon_events.closed_at/closed_by`。
3. 对当天匹配的 `reporting_andon_summary` 执行 `closed_count+1`；汇总行不存在不创建。
4. 插入 `ANDON_CLOSE` 审计和状态 `200`、消息 `closed` 的幂等记录后提交。

C++ 不检查 `closed_at IS NULL`。因此对同一已关闭安灯使用新的幂等键会再次成功，覆盖关闭时间/关闭人并再次累加 `closed_count`；Python测试显式保留该行为。事务内 SQL 和 commit 结果也按 C++ 实际实现忽略。

## `POST /api/v1/material/reservations`

### 功能

按批次失效时间升序，把请求数量分配到一个或多个有可用库存的物料批次，写入预留、库存余额、物料事务、审计和幂等投影。仅 `PLANNER`、`MES_SUPERVISOR` 或 `MES_ADMIN` 可调用。

### 请求 Header

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `X-Idempotency-Key` | string | 是 | 非空；角色校验通过后命中时返回首次分配的第一个预留 ID。 |
| `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`。 |
| `X-Actor-Role` | string | 条件必传 | 三个允许角色之一；支持 `X-Role`，校验先于缓存。 |
| `X-Actor-Id` | string | 否 | 物料事务和审计操作人；支持 `X-User-Id`，默认 `system`。 |

### JSON Body

| 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `materialCode` | string | 否 | 默认空字符串，用于精确查询可用库存。没有字段级校验。 |
| `reservedQuantity` | number | 否 | 仅 JSON number 被读取，默认 `0`；小于等于 `0` 时直接成功且不分配批次。 |
| `workOrderNumber` | string | 否 | 默认空字符串；预留表要求已有工单，但插入失败会被忽略。 |
| `operationSequence` | number/string | 否 | number 截断为整数，整数字符串可转换，默认 `0`。 |
| `expiresAt` | string | 否 | 默认空字符串；不解析时间格式。 |

### 分配算法

接口从 `v_available_inventory` 查询目标物料，按 `expiry_at` 升序遍历。视图条件仅为 `on_hand_quantity > reserved_quantity` 且批次状态为 `AVAILABLE`；它不排除已经过期的批次，因此过期但状态仍为 `AVAILABLE` 的批次仍会优先分配。

每个批次分配 `min(剩余需求, available_quantity)`，并尝试：

1. 插入 `material_inventory_reservations`，生成独立 `RSV-...` ID，状态 `ACTIVE`。
2. 对对应 `material_inventory_balances.reserved_quantity` 加上本次数量。
3. 插入 `RESERVE` 类型的 `material_material_transactions`。C++ INSERT 未提供表的 `transaction_id`；SQLite 因此保存 NULL，Python保持相同列清单。

如果遍历结束后短缺量大于 `0.0001`，整个事务回滚并返回 `409`；消息为 `insufficient inventory, short N.NNNNNN`。否则写 `MATERIAL_RESERVE` 审计，以第一个预留 ID（无分配时为空字符串）保存幂等记录并提交。

### 成功与幂等响应

首次成功返回 `200 OK`：

```json
{
  "data": {
    "status": "ACTIVE",
    "allocatedLots": [
      {"lotNumber": "LOT-RES-A", "quantity": 2.0, "reservationId": "RSV-..."},
      {"lotNumber": "LOT-RES-B", "quantity": 3.0, "reservationId": "RSV-..."}
    ]
  },
  "meta": {"correlationId": "reservation-create", "generatedAt": "2026-08-21T00:00:00Z"}
}
```

数量小于等于 `0` 时 `allocatedLots=[]`，仍写审计和资源 ID 为空字符串的幂等记录。幂等命中响应只含 `reservationId` 和 `cached=true`，不重放完整分配清单。

### 错误响应与忽略写错

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 幂等键为空或 JSON 无法解析。 |
| `403` | `FORBIDDEN` | 角色不在允许集合。 |
| `409` | `CONFLICT` | 可用数量不足；短缺分支显式回滚全部本次分配。 |
| `500` | `DATABASE_ERROR` | Python 在幂等/库存查询或事务开启失败时返回。 |

C++ 不检查每个批次内的三条 SQL，也不检查审计、幂等和 commit。因此例如未知工单可能导致预留主记录失败，但余额与物料事务仍写入，响应仍把生成的预留 ID列入 `allocatedLots`。Python保留该非原子错误处理语义。

## `POST /api/v1/material/consumptions`

### 功能

按序列号记录物料批次消耗，扣减库存、尝试冲减预留，并写物料事务、追溯、审计及幂等投影。接口不校验角色。

### 请求 Header 与 Body

| 位置/参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| Header `X-Idempotency-Key` | string | 是 | 非空；命中发生在 JSON 和 Body 字段校验之前。 |
| Header `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`。 |
| Header `X-Actor-Id` | string | 否 | 消耗记录、物料事务和审计操作人；支持 `X-User-Id`，默认 `system`。 |
| Body `serialNumber` | string | 是 | 非空字符串；不要求产品单元已存在。 |
| Body `materialCode` | string | 是 | 非空字符串。 |
| Body `lotNumber` | string | 是 | 非空字符串。 |
| Body `workOrderNumber` | string | 否 | 默认空字符串；消耗表要求已有工单，但写入错误会被忽略。 |
| Body `quantity` | number | 否 | 仅 JSON number 被读取；缺失、`null`、字符串或其他类型时为 `1.0`。没有正数校验。 |

### 库存校验与写入

处理器先仅按 `material_code + lot_number` 查询一条 `material_inventory_balances`，不限定工厂或库位。无记录或 `on_hand_quantity < quantity` 时返回 `409 insufficient on-hand`。

校验通过后，在事务内尝试：

1. 插入 `production_material_consumptions`，ID 为 `MC-...`、单位固定 `EA`。
2. 对所有匹配该物料/批次的余额执行 `on_hand_quantity -= quantity`。只有 `reserved_quantity > quantity` 时才减去 quantity；两者相等时预留量保持不变。
3. 插入 `CONSUMPTION` 物料事务，工厂/库位固定 `PLANT-A/RAW-STORE`，引用类型为 `SERIAL_NUMBER`；与 C++ 一样不提供 `transaction_id`，SQLite 保存 NULL。
4. 插入 `MATERIAL_CONSUMED` 追溯事件、`MATERIAL_CONSUME` 审计和状态 `200`、消息 `consumed` 的幂等记录后提交。

事务内所有 SQL 和 commit 返回值均被忽略。因此可能出现部分写入：例如负数 quantity 通过库存比较，消耗主表因 `quantity > 0` 约束不落库，但余额反而增加，负数物料事务/追溯/审计/幂等仍写入，接口仍返回成功。独立测试保留这一 C++ 实际行为。

### 成功与错误响应

首次成功固定返回 `200 OK`，`data` 仅含生成的 `consumptionId`。幂等命中返回同一 ID 和 `cached=true`。

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| `422` | `VALIDATION_ERROR` | 幂等键为空、JSON 非法，或三个必传字符串任一为空/非字符串。 |
| `409` | `CONFLICT` | 找不到余额，余额查询失败，或账面在手量小于请求 quantity。 |
| `500` | `DATABASE_ERROR` | Python 在幂等查询或事务开启失败时返回。 |

接口没有角色校验，也不校验 quantity 正数、工单、序列号或余额所在工厂/库位。

## `GET /api/v1/material/allocations/suggest`

### 功能

按批次失效时间升序返回指定物料的全部当前可用库存，并从 `1` 开始标注 FIFO 排名。接口只给出建议，不写预留，不校验角色。

### 请求

| 位置 | 参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- | --- |
| Header | `X-Correlation-Id` | string | 否 | 原样写入响应；省略时生成 `corr-...`。 |
| Query | `materialCode` | string | 是 | 必须出现；显式传空字符串视为已提供，正常返回空数组。按代码精确匹配。 |
| Body | 无 | - | 否 | 请求体被忽略。 |

### 成功响应

固定返回 `200 OK`，`data` 是直接数组（没有内层列表 envelope）：

| JSON 字段 | 类型 | 说明 |
| --- | --- | --- |
| `lotNumber` | string | 批次号；数据库 NULL 转空字符串。 |
| `availableQuantity` | number | `on_hand_quantity - reserved_quantity`。 |
| `expiryAt` | string | 批次失效时间；NULL 转空字符串。 |
| `locationCode` | string | 库位。 |
| `fifoRank` | number | 查询结果中的 1-based 顺序。 |

```json
{
  "data": [
    {
      "lotNumber": "LOT-001",
      "availableQuantity": 1000.0,
      "expiryAt": "2027-01-01T00:00:00Z",
      "locationCode": "RAW-STORE",
      "fifoRank": 1
    }
  ],
  "meta": {"correlationId": "allocation-suggest", "generatedAt": "2026-08-21T00:00:00Z"}
}
```

### 数据库访问与错误响应

查询 `v_available_inventory`，该视图只保留在手量大于预留量且批次状态为 `AVAILABLE` 的余额；不排除已过期批次。SQL 使用 `ORDER BY expiry_at`，SQLite 把 NULL 排在升序结果前，因此无失效日期的批次获得更高 FIFO 排名。

缺少 query 参数时返回 `422 VALIDATION_ERROR`，消息为 `materialCode required`。Python 在查询失败时返回 `500 DATABASE_ERROR`。空结果是正常 `200` 和 `data=[]`。

## `GET /api/v1/trace/units/{serialNumber}`

### 功能

聚合一个产品单元的工单/物料状态、执行事件及参数、物料消耗和检验结果，形成正向追溯视图。接口不做角色或认证校验，也不写数据库。

### 请求与产品响应

Path `serialNumber` 必须对应 `production_product_units`，Header 只读取可选 `X-Correlation-Id`。成功返回 `200 OK`，`data.product` 包含：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `serialNumber` | string | 序列号。 |
| `workOrderNumber` | string | 所属工单。 |
| `materialCode` | string | 工单成品物料。 |
| `routingVersion` | string | 工单路线版本。C++ 虽查询 `routing_code`，但响应不返回它。 |
| `status` | string | 产品单元状态。 |

产品不存在或主查询失败时返回 `404 NOT_FOUND`，消息 `serial not found`。

### `data.operations`

处理器按 `production_execution_events.occurred_at` 升序逐事件生成一项，而不是按工序聚合。因此同一工序的 START/COMPLETE 等多个事件分别出现。每项包含：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sequence` | number | 工序序号。 |
| `operationCode` | string | 工序代码。 |
| `stationCode` | string | NULL 转空字符串。 |
| `equipmentCode` | string | NULL 转空字符串。 |
| `operatorId` | string | 操作人。 |
| `eventType` | string | 事件类型。 |
| `occurredAt` | string | 事件时间。 |
| `parameters` | array | 按该序列号和工序查询的全部参数，按 `recorded_at` 升序。同工序的每个事件会重复包含同一参数数组。 |

参数项包含 `code`、number `value`、number/null `lowerLimit`、number/null `upperLimit`、boolean `inSpec`（数据库整数严格等于 `1`）、string `recordedAt`。

### 物料消耗与检验

`data.materialConsumptions` 按 `consumed_at` 升序，每项包含 `materialCode`、`lotNumber`、number `quantity`、`consumedAt`。

`data.inspections` 按 `inspected_at` 升序，每项包含 `planCode`、number/null `operationSequence`、`disposition`、`defectCode`、`inspectorId`、`inspectedAt`；NULL `defect_code` 转空字符串。

```json
{
  "data": {
    "product": {
      "serialNumber": "SN-TRACE-001",
      "workOrderNumber": "WO-20260801-001",
      "materialCode": "MAT-PROD-01",
      "routingVersion": "1",
      "status": "WAITING_INSPECTION"
    },
    "operations": [],
    "materialConsumptions": [],
    "inspections": []
  },
  "meta": {"correlationId": "unit-trace", "generatedAt": "2026-08-21T00:00:00Z"}
}
```

与 C++ `queryAll` 行为对齐，Python把执行事件、参数、消耗或检验子查询失败映射为空数组；只有产品主查询失败映射为 `404`。

## `POST /api/v1/trace/impact-query`

### 功能

按物料批次查询所有消耗记录对应的产品单元，统计在制、完成、报废数量和风险级别，并写审计及幂等投影。接口不校验角色。

### 请求

| 位置/参数 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| Header `X-Idempotency-Key` | string | 是 | 非空；命中时返回 `queryId`（实际为批次号）和 `cached=true`。 |
| Header `X-Correlation-Id` | string | 否 | 省略时生成 `corr-...`。 |
| Header `X-Actor-Id` | string | 否 | 审计操作人；支持 `X-User-Id`，默认 `system`。 |
| Body `lotNumber` | string | 是 | 非空字符串；非字符串按缺失处理。 |

### 统计规则

查询 `production_material_consumptions` 并按序列号 LEFT JOIN `production_product_units`。查询没有 `DISTINCT`，因此统计单位是消耗记录，不是唯一序列号：同一产品多次消耗同批次会在 `affectedUnits` 和计数中重复出现。

状态分类：

- `PASSED` 计入 `completedCount`。
- `SCRAPPED` 计入 `scrappedCount`。
- 其他任何状态（包括 `CREATED`、`IN_PROCESS`、`FAILED` 等）以及无产品主记录时生成的 `UNKNOWN`，都计入 `inProcessCount`。

`affectedUnitCount` 是消耗查询行数。风险级别：大于 100 为 `CRITICAL`，大于 10 为 `WARNING`，否则为 `INFO`。

### 成功与幂等响应

首次处理返回 `200 OK`：

```json
{
  "data": {
    "query": {"lotNumber": "LOT-001"},
    "impactSummary": {
      "affectedUnitCount": 12,
      "inProcessCount": 4,
      "completedCount": 5,
      "scrappedCount": 3,
      "riskLevel": "WARNING"
    },
    "affectedUnits": [
      {
        "serialNumber": "SN-001",
        "workOrderNumber": "WO-001",
        "unitStatus": "PASSED"
      }
    ]
  },
  "meta": {"correlationId": "impact-query", "generatedAt": "2026-08-21T00:00:00Z"}
}
```

处理器随后分别插入 `TRACE_IMPACT_QUERY`/`MATERIAL_LOT` 审计和状态 `200`、资源为批次号、消息 `impact` 的幂等记录，不使用显式事务且忽略两项写入错误。幂等命中只返回 `{"queryId":"LOT-001","cached":true}`，不重放统计结果。

### 错误与降级

幂等键为空、JSON 非法或 `lotNumber` 为空/非字符串时返回 `422 VALIDATION_ERROR`。Python 在幂等查询失败时返回 `500 DATABASE_ERROR`。影响查询失败按 C++ `queryAll` 行为降级为空结果：计数全为 `0`、风险 `INFO`，仍返回 `200` 并尝试写审计/幂等。

## `POST /api/v1/integration/inbox`

### 功能与请求

接收外部系统事件，以 `eventId` 作为消息主键并同步完成本地投影消费。接口不校验角色或认证，也不读取幂等 Header。Body 必须是 JSON；下列三个 string 字段均为必传且不能为空：

| 字段 | 说明 |
| --- | --- |
| `eventId` | 外部事件 ID，同时作为 `integration_inbox_messages.message_id`。 |
| `sourceSystem` | 来源系统。 |
| `eventType` | 事件类型，决定投影分支。 |
| `payload` | 可选事件载荷；缺失时按空对象处理。 |

非法 JSON 返回 `422 VALIDATION_ERROR / invalid json`；必填字段缺失、不是 string 或为空时返回 `422 VALIDATION_ERROR / eventId/sourceSystem/eventType required`。

### 消费规则

新消息先以 `RECEIVED` 状态、完整请求 JSON 载荷写入收件箱，然后在同一事务中执行以下分支：

- `eventType` 等于 `erp.plan.pushed` 或包含 `plan.pushed`：按 `payload.materialCode` 查找最新生效路线。找到后创建 `WO-YY-MM-DD-xxxxxx` 形式的 `DRAFT` 工单，并把路线工序复制为 `PENDING` 工单工序；`plantCode` 默认 `PLANT-A`，`priority` 和 `quantity` 缺失或无法转换时为 `0`。没有生效路线则把消息标记为 `FAILED`，错误为 `no effective routing`。
- `eventType` 包含 `equipment.status`：当 `equipmentCode` 和 `status` 都非空时，写设备事件并更新设备当前状态与心跳；`plantCode` 默认 `PLANT-A`，`occurredAt` 缺失时使用当前 UTC 时间。
- `eventType` 包含 `wms.inventory`：将匹配工厂、物料和批次的库存 `on_hand_quantity` 增加 `quantity`；工厂默认 `PLANT-A`，缺失或非 number 数量按 `0`。
- 其他事件不建立业务投影，直接标记为 `PROCESSED`。

除“无生效路线”外，业务投影 SQL 没有命中或执行失败均按 C++ 的忽略写结果行为继续处理；收件箱最终更新 `processed_at`、状态和错误消息。

### 响应与重复消息

首次消费始终返回 `202 Accepted`：

```json
{
  "data": {
    "messageId": "EVENT-001",
    "status": "PROCESSED"
  },
  "meta": {"correlationId": "inbox-001", "generatedAt": "2026-08-22T00:00:00Z"}
}
```

若 `eventId` 已存在，不再消费投影，返回数据库中已有状态和 `cached=true`，HTTP 仍为 `202`。收件箱查库或开启事务失败时，Python 返回 `500 DATABASE_ERROR`。

## `GET /api/v1/integration/messages`

### 权限与查询

仅 `X-Actor-Role: MES_ADMIN` 可访问；也接受备用 Header `X-Role`。省略角色时按 `MES_OPERATOR`，返回 `403 FORBIDDEN / MES_ADMIN required`。

可选 Query `status` 对 `integration_inbox_messages.status` 做精确匹配。只要参数存在就启用过滤，因此 `?status=` 会匹配空状态并通常返回空列表。结果按 `received_at` 倒序，最多 100 条；`total` 是本次返回行数，不是独立 COUNT 查询。

### 响应

每条消息包含 `messageId`、`sourceSystem`、`messageType`、`status`、number `retryCount`、`receivedAt`、`processedAt` 和 `errorMessage`；数据库中 NULL 的处理时间和错误消息转换为空字符串。

C++ 先构造 `listEnvelope`，再把它传入通用 `respondOk`，所以响应保留双层 `data/meta`：

```json
{
  "data": {
    "data": [
      {
        "messageId": "EVENT-001",
        "sourceSystem": "ERP",
        "messageType": "erp.plan.pushed",
        "status": "PROCESSED",
        "retryCount": 0,
        "receivedAt": "2026-08-22T00:00:00Z",
        "processedAt": "2026-08-22T00:00:01Z",
        "errorMessage": ""
      }
    ],
    "meta": {
      "page": 1,
      "pageSize": 100,
      "total": 1,
      "correlationId": "messages-001",
      "generatedAt": "2026-08-22T00:00:00Z"
    }
  },
  "meta": {"correlationId": "messages-001", "generatedAt": "2026-08-22T00:00:00Z"}
}
```

查询失败时 Python 返回 `500 DATABASE_ERROR`。

## `GET /api/v1/integration/outbox`

### 权限与查询

仅 `MES_ADMIN` 可访问，角色 Header 与默认值规则同收件箱消息列表。可选 Query `status` 对 `integration_outbox_messages.status` 做精确匹配；参数存在但值为空时仍启用过滤。结果按 `created_at` 倒序并限制 100 条，`total` 是返回行数。

处理器从消息表出发 LEFT JOIN `integration_outbox_delivery_attempts` 和 `integration_outbox_dead_letters`，按 `outbox_id` 聚合。投递重试数使用尝试表的 `COUNT(attempt_id)`，而不是死信表中保存的 `retry_count`；最后尝试时间使用 `MAX(attempted_at)`。

### 响应

每条消息包含：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `outboxId` | string | 出站消息 ID。 |
| `aggregateType` / `aggregateId` | string | 聚合类型和 ID。 |
| `eventType` / `targetSystem` | string | 事件类型和目标系统。 |
| `status` | string | `PENDING`、`PUBLISHED` 或 `FAILED`。 |
| `retryCount` | number | 关联投递尝试行数。 |
| `createdAt` | string | 创建时间。 |
| `publishedAt` | string | 发布时间，NULL 转空字符串。 |
| `lastAttemptAt` | string | 最新尝试时间，无尝试时为空字符串。 |
| `lastError` / `failedAt` / `replayedAt` | string | 死信信息，相应字段 NULL 或没有死信记录时为空字符串。 |

与 C++ 一致，列表使用和 `/api/v1/integration/messages` 相同的双层 `data/meta` 信封，内层分页固定为 `page=1`、`pageSize=100`。查询失败时 Python 返回 `500 DATABASE_ERROR`。

## `POST /api/v1/integration/outbox/{id}/replay`

### 请求与校验顺序

Header `X-Idempotency-Key` 必须非空，否则返回 `422 VALIDATION_ERROR`；`X-Correlation-Id` 可省略并自动生成。随后要求 `MES_ADMIN`，权限检查发生在幂等缓存查询之前，因此未授权请求即使使用已成功的幂等键仍返回 `403`。

幂等未命中时按 path `id` 查询出站消息：不存在或主查询失败按 C++ `queryOne` 行为返回 `404 NOT_FOUND / outbox message not found`；状态不是 `FAILED` 返回 `409 INVALID_STATE / only FAILED messages may be replayed`。

### 成功处理

在事务中执行：

1. 把出站消息改为 `PENDING` 并清空 `published_at`。
2. 把对应死信的 `replayed_at` 更新为当前 UTC 时间；不存在死信行不视为错误。
3. 写 `OUTBOX_REPLAY` / `OUTBOX` 审计，before 为 `{"status":"FAILED"}`、after 为 `{"status":"PENDING"}`。
4. 保存状态码 `200`、资源 ID 为出站消息 ID、消息 `outbox replayed` 的幂等记录。

两个必需 UPDATE 任一执行失败会回滚并返回 `500 PERSISTENCE_ERROR`；开启事务失败返回 `500 DATABASE_ERROR`。审计、幂等写和 commit 结果按 C++ 调用方式不改变响应。

首次成功返回 `202 Accepted`：

```json
{"data":{"outboxId":"OUT-001","status":"PENDING"},"meta":{"correlationId":"replay-001","generatedAt":"2026-08-22T00:00:00Z"}}
```

幂等命中不读取 path 对应消息，返回幂等记录中的资源 ID、固定状态 `PENDING` 和 `cached=true`，HTTP 为 `200 OK`。

## `GET /api/v1/dashboard`

### 范围与数据源

接口无角色校验。可选 Query `plantCode` 缺失时默认 `PLANT-A`；参数存在但为空时使用空字符串，不回退默认工厂。数据来自：

- `v_dashboard_current`：按工厂汇总班次生产报表。计划达成率为 `completed / expected * 100`，一次合格率为 `good / (completed + rejected) * 100`，两者在视图中保留两位小数。
- `v_wip_by_operation`：遍历各工序行，分别累加 `IN_PROCESS`、`REWORK`、`HOLD` 数量；`WAITING_INSPECTION` 和 `CREATED` 虽参与视图分组，但三个 FILTER 都不计数。`total` 是上述三项之和。
- `reporting_equipment_oee_hourly`：先跨设备/小时求和，再计算可用率 `running / planned`、性能率 `actual / ideal`、质量率 `good / actual` 和三者乘积 OEE；各分母不大于零时相应指标为 `0`。
- `v_active_andons`：只统计未关闭的 `CRITICAL` 和 `WARNING`；字段 `unacknowledged` 实际等于两者之和，不检查 `acknowledged_at`，已确认但未关闭的安灯仍计入。

### 响应

```json
{
  "data": {
    "scope": {"plantCode": "PLANT-A"},
    "production": {
      "plannedQuantity": 150,
      "completedQuantity": 90,
      "goodQuantity": 80,
      "rejectedQuantity": 10,
      "planAttainmentPercent": 75.0,
      "firstPassYieldPercent": 80.0
    },
    "wip": {"total": 4, "processing": 2, "hold": 1, "rework": 1},
    "oee": {
      "availabilityPercent": 75.0,
      "performancePercent": 80.0,
      "qualityPercent": 90.0,
      "oeePercent": 54.0
    },
    "andon": {"critical": 1, "warning": 1, "unacknowledged": 2},
    "freshness": {
      "lastEventAt": "2026-08-22T00:00:00Z",
      "delaySeconds": 0,
      "stale": false
    }
  },
  "meta": {"correlationId": "dashboard-001", "generatedAt": "2026-08-22T00:00:00Z"}
}
```

没有生产汇总行时各生产数字为零；没有 OEE/WIP/活动安灯时相应统计为零。生产汇总的 `last_event_at` 为空或整行不存在时，`lastEventAt` 使用当前 UTC 时间。`delaySeconds` 固定 `0`、`stale` 固定 `false`。各数据源查询失败按 C++ `queryOne/queryAll` 行为降级为相同的空数据，而不返回 HTTP 错误。

## `POST /api/v1/master/recipes`

### 权限、幂等与主字段

必须提供非空 `X-Idempotency-Key`，允许角色为 `MES_ADMIN` 或 `PROCESS_ENGINEER`；权限先于幂等缓存检查。Body 必须为 JSON，主字段规则如下：

| 字段 | 类型 | 必传 | 说明 |
| --- | --- | --- | --- |
| `recipeCode` / `version` | string | 是 | 共同组成配方版本主键。 |
| `recipeName` | string | 否 | 缺失按空字符串写入。 |
| `productMaterialCode` | string | 是 | 成品物料；不存在时由外键插入失败映射为 `409`。 |
| `targetBatchSize` | number | 是 | 必须大于 0；数字字符串不接受。 |
| `unitCode` | string | 是 | 配方默认单位。 |
| `status` | string | 否 | 默认 `DRAFT`，只接受 `DRAFT` 或 `EFFECTIVE`。 |
| `effectiveFrom` / `effectiveTo` | string | 否 | 起始时间默认当前 UTC；空结束时间写 NULL。 |
| `approvedBy` | string | 否 | EFFECTIVE 时使用；空值默认请求操作人。 |
| `components` | array | 是 | 必须是非空数组。 |
| `parameters` | array | 否 | 存在时必须为数组。 |

### 组分与参数校验

每个组分要求：正整数 `sequence`、非空 `materialCode`/`phaseCode`、正数 `targetQuantity`，且 `0 <= lowerLimit <= targetQuantity <= upperLimit`。上下限缺失时各自默认目标值；`unitCode` 为空时继承配方单位；`required` 默认 true、`hazardous` 默认 false。布尔字段也按 C++ helper 接受非零数字及字符串 `true`/`TRUE`。

每个参数要求：正整数 `stepSequence`、非空 `parameterCode`/`unitCode`，且 `lowerLimit <= targetValue <= upperLimit`；缺失的三个数值均按 0。`parameterName` 可为空，`required` 默认 true。

主字段/组件缺失返回统一 `422 VALIDATION_ERROR`；无效状态、组件、参数类型或参数范围分别返回对应 validation 消息。

### 持久化与响应

事务依次写配方、所有组分、所有参数。任一业务插入失败回滚并返回 `409 CONFLICT`。EFFECTIVE 配方写 `approved_by` 和当前 `approved_at`，DRAFT 两字段为 NULL。随后尝试写 `RECIPE_CREATE` / `RECIPE` 审计（资源 ID 为 `code:version`）及状态 `200` 的幂等记录；与 C++ 一致，这两项和 commit 的返回值不改变成功响应。

```json
{
  "data": {
    "recipeCode": "REC-001",
    "version": "1",
    "status": "DRAFT",
    "componentCount": 2,
    "parameterCount": 1
  },
  "meta": {"correlationId": "recipe-001", "generatedAt": "2026-08-22T00:00:00Z"}
}
```

幂等命中不解析 Body，返回 `{"recipeId":"REC-001:1","cached":true}`，HTTP 为 `200`。开启事务失败返回 `500 INTERNAL_ERROR`；幂等查库失败返回 `500 DATABASE_ERROR`。

## `GET /api/v1/master/recipes/{recipe}/versions/{version}`

### 功能与响应

按配方代码和版本读取配方主记录、组分及工艺参数；接口不校验角色。主记录不存在或主查询失败返回 `404 NOT_FOUND / recipe version not found`。

与 C++ 把 `SqliteStore` 查询行直接放入 JSON 的行为一致，三个响应区域都保留数据库 snake_case 列名，不转换为 camelCase：

```json
{
  "data": {
    "recipe": {
      "recipe_code": "REC-001",
      "version": "1",
      "recipe_name": "Mixing",
      "product_material_code": "MAT-PROD-01",
      "target_batch_size": 100.0,
      "unit_code": "KG",
      "status": "DRAFT",
      "effective_from": "2026-08-01T00:00:00Z",
      "effective_to": null,
      "approved_by": null,
      "approved_at": null
    },
    "components": [
      {
        "component_sequence": 10,
        "material_code": "MAT-RAW-01",
        "phase_code": "MIX",
        "target_quantity": 25.0,
        "lower_limit": 24.0,
        "upper_limit": 26.0,
        "unit_code": "KG",
        "required": 1,
        "hazardous": 0
      }
    ],
    "parameters": [
      {
        "step_sequence": 10,
        "parameter_code": "TEMP",
        "parameter_name": "Temperature",
        "target_value": 80.0,
        "lower_limit": 75.0,
        "upper_limit": 85.0,
        "unit_code": "C",
        "required": 1
      }
    ]
  },
  "meta": {"correlationId": "recipe-read", "generatedAt": "2026-08-22T00:00:00Z"}
}
```

组分按 `component_sequence` 升序；参数按 `step_sequence`、`parameter_code` 升序。`required` 和 `hazardous` 保持数据库整数，不转换成 boolean，nullable 的有效期和审批字段保持 JSON null。组分或参数子查询失败按 C++ `queryAll` 行为降级为空数组。

## `POST /api/v1/batches`

### 权限、请求和主数据校验

必须提供非空 `X-Idempotency-Key`，允许角色仅为 `PLANNER`、`MES_SUPERVISOR`、`PROCESS_ENGINEER`；`MES_ADMIN` 不在允许列表。权限先于幂等检查。

Body 必须包含非空 string `batchNumber`、`plantCode`、`recipeCode`、`recipeVersion`、`equipmentCode`，以及大于 0 的 number `plannedQuantity`。缺失或类型不符返回 `422 VALIDATION_ERROR`。

随后依次校验：

1. 配方版本不存在或查询失败：`404 NOT_FOUND / recipe version not found`。
2. 配方状态不是 `EFFECTIVE`：`409 CONFLICT / recipe version is not effective`。
3. 当前 UTC 字符串早于 `effective_from`，或大于等于非空 `effective_to`：`409 CONFLICT / recipe version is outside its effective period`。
4. 设备不存在或查询失败：`404 NOT_FOUND / equipment not found`。
5. 设备 `plant_code` 与请求工厂不同：`422 VALIDATION_ERROR / equipment does not belong to plant`。

成功时在事务中创建 `DRAFT` 批次，成品物料和单位从配方复制，实际数量默认 0；然后尝试写 `BATCH_CREATE` 审计及状态 200、资源为批次号的幂等记录。批次插入失败回滚并返回 `409 CONFLICT`；辅助写和 commit 返回值不改变 C++ 成功语义。

```json
{
  "data": {
    "batchNumber": "BATCH-001",
    "status": "DRAFT",
    "recipeCode": "REC-001",
    "recipeVersion": "1",
    "plannedQuantity": 250.5
  },
  "meta": {"correlationId": "batch-create", "generatedAt": "2026-08-22T00:00:00Z"}
}
```

幂等命中不解析 Body，返回 `batchNumber` 和 `cached=true`。

## `GET /api/v1/batches`

接口无角色校验。可选 Query `plantCode`、`status` 均为精确过滤；参数存在但为空时仍启用过滤。数据从 `v_batch_progress` 查询，按 `last_activity_at` 倒序、`batch_number` 升序，最多 200 条。

响应保留视图的 snake_case 字段：`batch_number`、`plant_code`、`product_material_code`、`recipe_code`、`recipe_version`、`equipment_code`、`planned_quantity`、`actual_quantity`、`unit_code`、`status`、`quality_disposition`、`hold_reason`、`started_at`、`completed_at`、`charge_count`、`parameter_record_count`、`last_activity_at`。nullable 字段保持 JSON null。

`charge_count` 和 `parameter_record_count` 是 DISTINCT ID 数。权威视图的活动时间表达式为 `MAX(COALESCE(charged_at, recorded_at, updated_at))`；由于称量列在参数列之前，同一批次同时 JOIN 到称量与参数时会优先采用称量时间，并非简单取两类事件的绝对最新时间。

与 C++ 一致，列表使用双层 `data/meta` 信封，内层 `page=1`、`pageSize=200`、`total=本次行数`。查询失败按 `queryAll` 行为返回空列表。

## `GET /api/v1/batches/{id}`

接口无角色校验，按批次号从 `v_batch_progress` 读取单行。不存在或查询失败返回 `404 NOT_FOUND / batch not found`。

成功响应的 `data` 直接采用与批次列表相同的 17 个 snake_case 视图字段，nullable 字段保持 JSON null，`charge_count` / `parameter_record_count` 及 `last_activity_at` 也沿用同一聚合和 `COALESCE` 规则；详情响应只有通用外层信封，不再添加列表分页信封。

## `POST /api/v1/batches/{id}/state`

### 权限、幂等和状态图

要求非空 `X-Idempotency-Key`，允许 `MES_OPERATOR`、`MES_SUPERVISOR`、`PROCESS_ENGINEER`；省略角色默认 `MES_OPERATOR`，`MES_ADMIN` 不在列表中。权限先于幂等检查。Body `targetStatus` 由下列状态图约束：

- `DRAFT -> RELEASED`（必须主管或工艺工程师批准）
- `DRAFT -> CANCELLED`
- `RELEASED -> IN_PROCESS`
- `RELEASED -> CANCELLED`
- `IN_PROCESS -> HOLD`
- `IN_PROCESS -> WAITING_QA`
- `HOLD -> IN_PROCESS`（必须主管或工艺工程师批准）

其他转换，包括空/非 string 目标，返回 `409 INVALID_STATE / transition {from} -> {target} is not allowed`。批次不存在或批次/设备 JOIN 查询失败返回 `404 NOT_FOUND`。

### 附加门槛

进入 `IN_PROCESS` 时设备状态不能是 `OFFLINE`、`FAULT`、`MAINTENANCE`，否则返回 `409 EQUIPMENT_UNAVAILABLE`；`IDLE`、`RUNNING` 以及约束外状态通过。

进入 `WAITING_QA` 时：

- `v_batch_recipe_compliance` 中所有 required 组分的缩放累计称量必须在上下限内。
- 所有 required 配方参数必须存在最新记录且 `in_spec=1`。

任一不满足返回 `409 BATCH_NOT_COMPLETE`。两项 COUNT 查询失败按 C++ `value_or(0)` 视为没有缺项。

### 持久化和响应

成功更新状态、`updated_at`、`version+1`。进入 HOLD 时 `hold_reason` 使用可选 string `reason`（缺失为空字符串），其他目标清空该字段；仅 `RELEASED -> IN_PROCESS` 设置 `started_at`，HOLD 恢复不会覆盖首次启动时间。

事务还尝试写：

- `BATCH_STATE_CHANGE` 审计，before 为旧状态，after 为新状态和 reason。
- 目标 ERP 的 `batch.state.changed` PENDING outbox，载荷包含批次号、from、to。
- 状态 200、资源为批次号、消息 `batch state changed` 的幂等记录。

状态 UPDATE 失败回滚并返回 `500 INTERNAL_ERROR`；辅助写及 commit 结果不改变成功语义。成功响应包含 `batchNumber`、`previousStatus`、`status` 和更新后的 number `version`。

幂等命中发生在 Body 和批次查询之前，返回 `{"batchNumber":"<本次 path id>","cached":true}`；它使用本次 path，而不是幂等记录中的资源 ID。

## `POST /api/v1/batches/{id}/charges`

### 请求、权限与配方上限

要求非空 `X-Idempotency-Key`，角色规则同批次状态接口。Body 必须包含正整数 `componentSequence`、非空 string `materialCode` / `lotNumber`、正 number `quantity`；`componentSequence` 也按 C++ helper 接受可转换的整数字符串。

处理器联查批次、配方和指定组分：不存在或查询失败返回 `404 NOT_FOUND / batch or recipe component not found`；批次必须是 `IN_PROCESS`，否则 `409 INVALID_STATE`；请求物料必须等于配方组分物料，否则 `422 MATERIAL_MISMATCH`。

缩放上限为 `component.upper_limit * batch.planned_quantity / recipe.target_batch_size`。该批次/组分的历史称量 SUM 加本次数量若大于 `scaledUpper + 1e-9`，返回 `409 CHARGE_OVER_TOLERANCE`。

### 批次与库存校验

按工厂、物料、批次号读取物料批次，并聚合同工厂所有库位的 `on_hand_quantity - reserved_quantity`：

- 物料批次不存在、物料不匹配或查询失败：`404 NOT_FOUND / material lot not found`。
- 状态不是 `AVAILABLE`，或非空 `expiry_at <= 当前 UTC`：`409 LOT_UNAVAILABLE`。
- 批次主数据 `available_quantity` 或聚合库存可用量任一小于本次数量：`409 INSUFFICIENT_INVENTORY`。

### 事务与响应

事务写入不可变称量记录，单位取配方组分单位，操作人取请求 actor；`source_event_id` 优先使用非空 `sourceEventId`，否则使用幂等键。随后：

1. 扣减物料批次 `available_quantity`，结果不大于 0 时状态改为 `CONSUMED`。
2. 扣减该工厂/批次所有匹配库存行的 `on_hand_quantity`。
3. 尝试写 `BATCH_CHARGE` / `BATCH` 审计和状态 200、资源为 `chargeId` 的幂等记录。

前三个核心 SQL 任一失败回滚并返回 `409 CONFLICT`；审计、幂等和 commit 结果不改变成功响应。本接口按 C++ 实现不写 outbox。

成功 `data` 包含 `chargeId`、`batchNumber`、`componentSequence`、`lotNumber`、`quantity`、`cumulativeQuantity`、`scaledUpperLimit`。幂等命中发生在 Body/path 校验前，仅返回原 `chargeId` 和 `cached=true`。

## `POST /api/v1/batches/{id}/parameters`

### 请求与规格判定

要求非空 `X-Idempotency-Key`，角色规则同批次状态接口。Body 必须包含正整数 `stepSequence`、非空 string `parameterCode` 和 JSON number `value`；数值字符串不接受，步骤号仍按 C++ integer helper 接受整数字符串。

处理器按批次固化的配方版本、步骤号和参数代码联查规格。批次或参数不存在、或查询失败时返回 `404 NOT_FOUND / batch or recipe parameter not found`；批次不是 `IN_PROCESS` 返回 `409 INVALID_STATE`。

`lower_limit <= value <= upper_limit` 时 `inSpec=true`，边界值视为合格。记录固化规格上下限和单位，`source_event_id` 使用非空 `sourceEventId`，否则使用幂等键。

### 越界处理与响应

参数记录插入失败回滚并返回 `409 CONFLICT`。越界时在同一事务中尝试：

- 把批次改为 `HOLD`，原因 `parameter {code} out of specification`，更新时间并递增版本。
- 创建 `CRITICAL` / `PROCESS_PARAMETER` 安灯，资源类型 `BATCH`、资源代码为批次号、消息同 HOLD 原因。

上述两个越界辅助 SQL 的结果按 C++ 实现被忽略。随后尝试写 `BATCH_PARAMETER_RECORD` 审计（after 只包含参数代码、值、inSpec）以及资源为 `recordId` 的幂等记录。本接口不写 outbox。

成功响应包含 `recordId`、`batchNumber`、`parameterCode`、`value`、boolean `inSpec`、`batchStatus`（合格固定 `IN_PROCESS`，越界固定 `HOLD`）和 `andonId`（合格时空字符串）。幂等命中不解析 Body/path，仅返回原 `recordId` 和 `cached=true`。

## `POST /api/v1/batches/{id}/quality-disposition`

### 权限、签名与状态

要求非空 `X-Idempotency-Key`，仅 `QUALITY_ENGINEER` 可调用；`MES_ADMIN` 不自动放行，权限先于幂等检查。Body `disposition` 必须为 `ACCEPTED` 或 `REJECTED`，`electronicSignature` 必须是非空 string，否则返回 `422 VALIDATION_ERROR`。

批次不存在或主查询失败返回 `404 NOT_FOUND`。允许状态为：

- `WAITING_QA`：无需偏差引用。
- `HOLD`：必须提供非空 string `deviationReference`。C++ 条件并不区分处置值，因此 ACCEPTED 和 REJECTED 都适用。

其他情况返回 `409 INVALID_STATE / batch must be WAITING_QA; HOLD acceptance also requires deviationReference`。

可选 `actualQuantity` 只有在值为 JSON number 时使用，否则回退到批次 `planned_quantity`；小于 0 返回 `422 VALIDATION_ERROR`，0 允许。

### 事务与响应

事务写入唯一的 `quality_batch_dispositions` 行，记录可选 `reasonCode`、nullable 偏差引用、请求 actor、电子签名和审核时间。随后更新批次：

- ACCEPTED -> `COMPLETED`
- REJECTED -> `REJECTED`
- `quality_disposition` 写原处置值，`actual_quantity` 写实际量，`completed_at`/`updated_at` 写当前时间，清空 HOLD 原因，版本加一。

任一核心 SQL 失败回滚并返回 `409 CONFLICT`。随后尝试写 `BATCH_QUALITY_DISPOSITION` 审计、目标 ERP 的 `batch.quality.dispositioned` PENDING outbox，以及资源为 `dispositionId` 的幂等记录；辅助写和 commit 结果不改变成功语义。

成功 `data` 包含 `dispositionId`、`batchNumber`、`disposition`、终态 `status`、`actualQuantity`、`reviewedBy`、`reviewedAt`。幂等命中不解析 Body/path，只返回原 `dispositionId` 和 `cached=true`。

## `GET /api/v1/batches/{id}/ebr`

### 电子批记录组成

接口无角色校验。主查询把 `production_batches b.*` 与该批次固化版本的 `master_recipes` 名称、目标批量、审批人、审批时间合并；批次不存在、配方 JOIN 不成立或主查询失败返回 `404 NOT_FOUND / batch not found`。

成功 `data` 包含：

- `batch`：批次表全部 snake_case 字段，外加 `recipe_name`、`target_batch_size`、`approved_by`、`approved_at`；nullable 字段保持 JSON null，版本保持 number。
- `recipeComponents`：固化配方组分，按 `component_sequence` 升序；字段和类型同配方读取接口。
- `charges`：`charge_id`、序号、物料、批次、数量、单位、操作人、时间，按 `charged_at` 升序。
- `parameters`：`record_id`、步骤、代码、测量值、上下限、单位、整数 `in_spec`、记录人、时间，按 `recorded_at` 升序。
- `qualityDisposition`：处置 ID、处置值、nullable 原因/偏差、审核人、电子签名、审核时间；不存在或子查询失败时为 JSON null。
- `auditTrail`：批次资源审计的 ID、操作人、动作、nullable `before_data`/`after_data` 文本、时间，按 `occurred_at` 升序。JSON 文本不解析，仍作为 string 返回。
- `generatedAt`：EBR 数据内部的当前 UTC 生成时间；通用外层 `meta` 仍另有 `generatedAt`。

除主记录外，组分、称量、参数、审计子查询失败均按 C++ `queryAll` 行为降级为空数组。

## `GET /metrics`

接口无角色校验，返回 `Content-Type: text/plain; version=0.0.4` 的 Prometheus 文本，不使用 JSON 信封：

```text
# TYPE mes_http_requests_total counter
mes_http_requests_total 120
# TYPE mes_http_client_errors_total counter
mes_http_client_errors_total 8
# TYPE mes_http_server_errors_total counter
mes_http_server_errors_total 2
```

每个应用 Router 持有独立的原子计数器：

- `mes_http_requests_total`：已完成响应的请求总数。
- `mes_http_client_errors_total`：其中状态为 4xx 的请求数。
- `mes_http_server_errors_total`：其中状态为 5xx 的请求数；5xx 不重复计入 4xx。

中间件在处理器返回后更新计数，所以某次 `/metrics` 响应展示的是它开始响应前已经完成的计数，不包含本次 metrics 请求自身；响应完成后该请求才计入下一次读取，与 C++ post-routing handler 的时序一致。

## `POST /api/v1/work-orders/{id}/split`

### 权限、请求和未启动约束

要求非空 `X-Idempotency-Key`，允许 `PLANNER`、`MES_SUPERVISOR`、`MES_ADMIN`，权限先于幂等。Body 要求非空 string `newWorkOrderNumber` 和正整数 `plannedQuantity`，新编号不能等于 path 源工单；数量也接受可转换整数字符串。

源工单不存在或主查询失败返回 `404 NOT_FOUND / source work order not found`。只有同时满足以下条件才允许拆分：

- 状态为 `DRAFT`、`RELEASED` 或 `SUSPENDED`。
- `completed_quantity=0` 且 `rejected_quantity=0`。
- 没有关联 `production_product_units`；COUNT 查询失败按 C++ `value_or(0)` 视为 0。

否则返回 `409 INVALID_STATE`。拆出数量必须严格小于源计划量，确保源剩余量为正，否则返回 `409 QUANTITY_CONSERVATION`。

### 事务和响应

事务先创建子工单，复制源的工厂、物料、路线/版本、nullable BOM/版本、优先级、状态及 nullable 计划起止时间；子工单计划量为拆出量。子工单插入冲突返回 `409 CONFLICT`。

随后复制全部工单工序（包括 SOP、质量门、可跳过、周期和当前状态）、扣减源计划量并将源版本加一、写 `SPLIT` lineage。任一失败回滚并返回 `500 PERSISTENCE_ERROR`。最后尝试写 `WORK_ORDER_SPLIT` 审计、目标 ERP 的 `work_order.split` outbox，以及资源为子工单号的幂等记录。

成功响应包含 `sourceWorkOrderNumber`、`newWorkOrderNumber`、`splitQuantity`、拆分后的 `sourcePlannedQuantity` 和原状态。幂等命中不解析 Body/path，只返回原子工单号和 `cached=true`。

## `POST /api/v1/work-orders/{id}/merge`

### 权限、请求和合并约束

要求非空 `X-Idempotency-Key`，允许 `PLANNER`、`MES_SUPERVISOR`、`MES_ADMIN`，权限先于幂等查询。Body 必须提供非空 string `sourceWorkOrderNumber`，且源工单号不能等于 path 中的目标工单号，否则返回 `422 VALIDATION_ERROR / different sourceWorkOrderNumber required`。

源或目标工单不存在、或任一主查询失败时返回 `404 NOT_FOUND / source or target work order not found`。两个工单都必须同时满足：

- 状态为 `DRAFT`、`RELEASED` 或 `SUSPENDED`。
- `completed_quantity=0` 且 `rejected_quantity=0`。
- 没有关联 `production_product_units`；COUNT 查询失败按 C++ `value_or(0)` 视为 0。

否则返回 `409 INVALID_STATE`。通过未启动检查后，两个工单的工厂、物料、路线代码、路线版本及状态必须完全相同，否则返回 `409 INCOMPATIBLE_WORK_ORDERS`。BOM、优先级和计划日期不参与兼容性判断。

### 事务、追溯和响应

事务把源计划量加到目标工单并递增目标版本，把源工单状态改为 `CANCELLED` 并递增源版本，再写入从源到目标、事件为 `MERGE`、数量为源计划量的不可变 lineage。源工单原计划量及其工序记录保持不变。三个核心 SQL 任一失败都会回滚并返回 `500 PERSISTENCE_ERROR`。

随后尝试写 `WORK_ORDER_MERGE` 审计、目标 ERP 的 `work_order.merge` PENDING outbox，以及状态 200、资源为目标工单号、消息 `work orders merged` 的幂等记录；这些辅助写和 commit 结果按 C++ 行为不改变成功响应。

成功 `data` 包含 `targetWorkOrderNumber`、`sourceWorkOrderNumber`、`mergedQuantity`、合并后的 `targetPlannedQuantity` 和目标原状态。幂等命中发生在 Body/path 业务校验之前，返回首次请求的目标工单号及 `cached=true`。
