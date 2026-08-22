# MES 日志系统与 Trace 追踪体系设计

> 语言无关（Language-agnostic）的设计文档。
> 不绑定任何具体语言/框架实现，但会说明如何与本项目 Python（Poetry + FastAPI）技术栈衔接：`logging`/`structlog` 日志与 `opentelemetry-python` 追踪 SDK。
> 适用场景：MES 制造执行系统（HTTP API），单实例起步、未来向多实例/多服务演进。

---

## 目录

- [1. 背景与现状](#1-背景与现状)
- [2. 设计目标与原则](#2-设计目标与原则)
- [3. 核心概念：可观测性三支柱](#3-核心概念可观测性三支柱)
- [4. 全局标识体系（Trace ID / Span ID / Correlation ID）](#4-全局标识体系)
- [5. 上下文传播协议](#5-上下文传播协议)
- [6. 日志体系设计](#6-日志体系设计)
- [7. Trace 追踪体系设计](#7-trace-追踪体系设计)
- [8. 端到端数据流架构](#8-端到端数据流架构)
- [9. MES 典型场景用例](#9-mes-典型场景用例)
- [10. 指标设计（简述）](#10-指标设计简述)
- [11. 实施路线图](#11-实施路线图)
- [12. 红线与验收清单](#12-红线与验收清单)

---

## 1. 背景与现状

### 1.1 为什么需要这套体系

MES 系统一旦出问题（工单丢失、重复创建、数据库锁、接口超时），运维和开发必须在**几分钟内**定位到：

- 哪个请求失败了？失败在哪个环节（网关 / API / DB / 缓存）？
- 这个工单号被谁、在什么时间、以什么幂等键处理过？
- 一次跨服务操作完整经过哪些节点、各耗时多少？
- 是否是缓存降级 / 配置错误 / 数据异常导致？

仅靠散落的 `println` 风格日志无法回答这些问题。需要**结构化日志 + 分布式追踪 + 指标**三件套。

### 1.2 项目现状盘点

| 能力 | 现状 | 缺口 |
|---|---|---|
| 关联 ID | HTTP 层已有 `x-correlation-id`，缺失时自动生成 `corr-xxxxxxxxxxxxxxxx`，响应 `meta.correlationId` 返回 | 仅覆盖 HTTP 请求，未贯通到 DB span、日志字段、外部调用 |
| 幂等键 | 已有 `x-idempotency-key` + `idempotency_keys` 表 | 幂等判定事件未打日志 |
| 日志插件 | Python `logging` + `structlog`：JSON/console 格式、文件滚动落盘（daily、非阻塞）、级别过滤、堆栈回溯 | 尚未启用文件落盘；日志未统一携带 trace/span 上下文；无集中式采集 |
| 追踪插件 | `opentelemetry-python`：W3C trace-context 默认传播，可选 jaeger/zipkin exporter，默认关闭 | 未启用；未定义业务 span 与属性 |
| 缓存/Redis | 尚未加入请求路径（`DEVELOPMENT_PROGRESS.txt` 明确待办） | 需在基础设施层记录连接、命中/未命中、TTL、失效、降级事件 |

### 1.3 设计边界

- **本设计不改变 HTTP / 数据库权威接口语义**（保证与 C++ 老系统 1:1 行为一致）。
- 日志与追踪只落在**基础设施层**：请求进入、处理、DB、缓存、外部调用、异常、结果返回。
- 允许逐步启用，单实例可用，多实例/多服务可平滑扩展。

---

## 2. 设计目标与原则

### 2.1 目标（可验证）

1. **全链路可追**：任意 HTTP 请求，能在 1 分钟内找到其完整 trace（跨 DB、缓存、外部调用）与相关日志。
2. **问题可定位**：错误日志必须能关联到（a）trace_id（b）correlation_id（c）幂等键（d）操作者（e）业务主键（工单号/物料号/序列号）。
3. **零噪音可读**：生产默认级别下，正常流日志体积可控、语义稳定、可被机器解析。
4. **零敏感泄漏**：日志与 trace 中绝不出现密码、token、完整敏感载荷。
5. **成本可控**：采样策略、保留策略、字段裁剪有明确规则，存储成本可预估。

### 2.2 原则

- **结构化优先**：一切日志输出为 JSON 键值对，禁止自由文本拼接（异常堆栈除外，作为字段值）。
- **约定大于配置**：字段名、级别、span 命名有全局字典，全团队统一。
- **语义与实现解耦**：本设计定义的字段/事件是**契约**；具体实现（Python `logging` / `structlog` / log4j）只是载体。
- **上下文自动传播**：Trace ID、Correlation ID 由基础设施自动注入日志和 span，业务代码不感知。
- **红线不可妥协**：脱敏规则由基础设施强制，业务代码无法绕过（见第 12 章）。

---

## 3. 核心概念：可观测性三支柱

```
            ┌────────────────────────────────────────────┐
            │              可观测性 (Observability)        │
            ├───────────────┬───────────────┬────────────┤
            │   Logs 日志    │  Metrics 指标  │ Traces 追踪│
            ├───────────────┼───────────────┼────────────┤
            │ 发生了什么    │ 状态/趋势/告警  │ 为什么 / 路径│
            │ "这件事发生"  │ "多少 / 多快"  │ "在哪一步"   │
            └───────────────┴───────────────┴────────────┘
```

三者不是并列可选项，而是互相引用：

- 日志 `error` 记录携带 `trace_id` → 点开可看该请求的完整 span 树；
- Trace 中某个 span 标记 `db: lock timeout` → 对应日志里有详细错误与堆栈；
- 指标 `http_request_duration_seconds` 的 99 分位飙升 → 下钻 trace 找到最慢的路径。

**核心术语**：

| 术语 | 含义 |
|---|---|
| **Trace（链路）** | 一次业务操作（如"创建工单"）跨越的所有处理的集合，由唯一 `trace_id` 标识 |
| **Span（跨度）** | Trace 中的一个最小工作单元（如"HTTP 入口"、"执行 SQL"），有 `span_id`、父 span、起止时间、属性、事件、状态 |
| **SpanContext** | trace_id + span_id + flags + trace_state 的传输单元 |
| **Event / Log** | Span 内部时间戳事件（如"cache miss"、"retry"），可携带结构化字段 |
| **Attribute** | Span 上的键值对（如 `mes.work_order_number=WO-1001`） |
| **Propagator** | 在进程/服务间传递 SpanContext 的编解码器（W3C / B3 / Jaeger） |

---

## 4. 全局标识体系

### 4.1 三种 ID 的职责划分

| ID | 生成方 | 格式 | 生命周期 | 用途 |
|---|---|---|---|---|
| **Trace ID** | 入口（网关/服务） | W3C：32 位十六进制（128 bit），如 `4bf92f3577b34da6a3ce929d0e0e4736` | 一次跨服务操作 | 追踪全链路 |
| **Span ID** | 每个工作单元 | 16 位十六进制（64 bit），如 `00f067aa0ba902b7` | 单个 span | 组成 span 树 |
| **Correlation ID** | 入口服务（无则生成） | 业务可读：`corr-` + 16 位十六进制，如 `corr-3d4f2a1b9c8d7e6f` | 一次客户端请求 | 业务方/日志/响应体展示、人工可读 |

关系：

- 一次 HTTP 请求 = **1 个 Trace** = **1 个 Correlation ID**（在服务内部一一对应）；
- Correlation ID 可以**包含在 trace 的根 span 属性**中，二者通过根 span 关联；
- 对外响应只暴露 `correlationId`（已有契约，不破坏）；Trace ID 通过日志/追踪系统查询。

### 4.2 标识注入点（HTTP 头规范）

| Header | 必须 | 说明 |
|---|---|---|
| `traceparent` | 否（缺失则生成） | W3C 标准，格式 `version-traceid-spanid-flags`，跨服务透传 |
| `tracestate` | 否 | 厂商扩展字段，透传 |
| `x-correlation-id` | 否（缺失则生成） | 业务关联 ID，响应 `meta.correlationId` 回传 |
| `x-idempotency-key` | 写接口建议 | 幂等键，与 trace 解耦但建议日志中同存 |
| `x-actor-id` / `x-actor-role` | 否 | 操作者身份，审计日志必备 |

### 4.3 生成与透传规则

1. **入口服务**：收到请求若无 `traceparent`，生成新 Trace ID；同时取/生成 Correlation ID。
2. **透传**：Trace ID 沿调用链透传（HTTP 头、DB span 继承上下文、缓存操作继承上下文）；Correlation ID 同一服务内全局透传，跨服务按部署策略（可映射为根 span 属性）。
3. **日志关联**：每条日志自动携带 `trace_id`、`span_id`、`correlation_id`（由日志 SDK 从当前上下文注入，业务代码不手动传）。
4. **幂等**：幂等键命中/创建事件日志同时记录 `idempotency_key` 与 `trace_id`，方便重放审计。

---

## 5. 上下文传播协议

### 5.1 协议选型

| 协议 | 选用 | 理由 |
|---|---|---|
| **W3C Trace-Context**（`traceparent`/`tracestate`） | ✅ 默认 | 行业标准、云厂商通用、`opentelemetry-python` 默认支持 |
| B3（Zipkin） | 兼容 | 遗留系统网关对接时启用 zipkin feature |
| Jaeger 格式 | 兼容 | 对接老 Jaeger 客户端时启用 |
| W3C Baggage（`baggage`） | 可选 | 跨服务携带业务上下文（如 tenant），**严禁放敏感数据** |

### 5.2 跨服务边界

```
  客户端
   │  traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
   ▼
  ┌─────────────┐  透传 traceparent   ┌──────────────┐  透传  ┌──────────────┐
  │ 网关 / LB    │ ──────────────────► │ MES API 服务 │ ─────► │ 业务服务/外部│
  │ (入口, 可选) │                     │ (FastAPI)    │        │ (DB/Redis)  │
  └─────────────┘                     └──────────────┘        └──────────────┘
```

- 服务边界处由 SDK Propagator 自动提取/注入，无需业务代码参与。
- 所有**出站调用**（外部 HTTP、DB、MQ）都应创建子 span 并继承当前 SpanContext。

---

## 6. 日志体系设计

### 6.1 日志分类

| 类别 | 输出方 | 内容 | 去向 |
|---|---|---|---|
| **访问日志（Access）** | 框架/中间件 | 每个 HTTP 请求：方法、路径、状态码、耗时、客户端、trace_id | 应用日志文件 / 集中式采集 |
| **应用日志（App）** | 业务代码 | 业务事件、告警、错误、调试 | 应用日志文件 |
| **审计日志（Audit）** | 写操作必经点 | 谁、何时、对什么资源、做了什么、幂等键、结果 | 应用日志（独立类别 `audit`）/ 可选独立存储 |
| **基础设施日志** | DB/缓存/调度 | 连接池、迁移、缓存命中/未命中、TTL、降级、重试 | 应用日志文件 |

> **审计与业务**：审计日志即写操作路径上的结构化日志，通过 `category=audit` 字段区分，不额外引入存储（起步阶段）。

### 6.2 结构化日志字段规范（契约）

所有日志必须是 JSON 对象，字段分三类：**必选 / 上下文 / 业务可选**。

**必选字段（每条日志都有）**：

```json
{
  "timestamp": "2026-08-22T08:15:30.123456Z",
  "level": "WARN",
  "logger": "api.work_orders",
  "message": "work order update skipped: already completed",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "00f067aa0ba902b7",
  "correlation_id": "corr-3d4f2a1b9c8d7e6f"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `timestamp` | RFC3339 字符串，UTC | 纳秒精度，统一 UTC，避免时区混乱 |
| `level` | DEBUG/INFO/WARN/ERROR/FATAL | 级别见 6.4 |
| `logger` | 字符串 | 模块路径命名空间（如 `api.work_orders`） |
| `message` | 字符串 | 人类可读的一句话；参数一律放字段，禁止拼接进 message |
| `trace_id` / `span_id` | 字符串 | 由 SDK 注入；无则空 |
| `correlation_id` | 字符串 | 由 SDK 注入 |

**上下文字段（按场景出现）**：

| 字段 | 场景 | 示例 |
|---|---|---|
| `category` | 日志类别 | `access` / `audit` / `db` / `cache` / `scheduler` |
| `method` / `path` / `status_code` | access | `POST` / `/api/v1/work-orders` / `201` |
| `latency_ms` | access、db、cache | `42` |
| `idempotency_key` | 写接口 | `abc-123` |
| `actor_id` / `actor_role` | 审计 | `u-1001` / `PLANNER` |
| `db.error` / `db.statement` | db 错误 | 错误摘要 + SQL（无绑定参数值） |
| `cache.action` | 缓存 | `hit` / `miss` / `set` / `expired` / `fallback` |
| `exception` | 错误 | 类型 + message + 堆栈（独立字段，含敏感时截断） |

**业务字段（MES 主键，可选但强烈建议）**：

```json
{
  "mes.work_order_number": "WO-2026-0812",
  "mes.material_code": "MAT-8812",
  "mes.plant_code": "P01",
  "mes.serial_number": "SN-0001",
  "mes.routing_code": "RT-A",
  "mes.routing_version": 1
}
```

> 业务主键必须作为**结构化字段**而非拼进 message，否则无法被检索和聚合。

### 6.3 日志格式与编码

- **生产格式**：`JSON`（每行一个对象，JSON Lines），便于 Filebeat/Loki/Promtail 采集。
- **开发格式**：`pretty`/`compact`（人类可读）。
- 编码统一 UTF-8；换行/控制字符转义；非 ASCII 不转义。

### 6.4 日志级别使用准则（契约）

| 级别 | 何时用 | 禁止 |
|---|---|---|
| `DEBUG` | 仅开发/排障；DB SQL、缓存键、中间变量 | 生产默认关闭 |
| `INFO` | 正常业务事件：请求完成、创建/更新成功、迁移完成、缓存命中摘要 | 每条日志都 INFO（噪音） |
| `WARN` | 可恢复异常：缓存 miss 降级、重试第 1 次、配置走默认值、幂等重复请求（非错误） | 掩盖性 warn（见注释） |
| `ERROR` | 影响本次请求/任务的失败：DB 错误、校验失败、外部调用失败 | 已知预期分支反复打 ERROR |
| `FATAL` | 服务无法继续：配置加载失败、启动失败、数据完整性被破坏 | 一般错误滥用 FATAL |

**WARN 约定**：WARN 必须带 `reason` 字段说明为何降级，并在每次升级事件（如"第 3 次重试仍失败"）提升为 ERROR 或告警。

### 6.5 脱敏与安全红线（强制）

日志 SDK / 序列化层统一执行，业务代码不可绕过：

| 类别 | 处理 |
|---|---|
| 密码 / 密钥 / token / cookie | **一律不记录**，即使请求体里有，也整体替换为 `[REDACTED]` |
| 完整请求体 / 完整响应体 | 默认不记录；需要时记录**脱敏摘要**（如只留字段名集合、长度、校验结果） |
| 身份证 / 手机号 / 工号 | 脱敏：`138****1234`，`3101**********12` |
| SQL 绑定参数值 | 不记录绑定值，只记录 SQL 模板 + 错误码 |
| 堆栈 | 只记录异常类名 + message + 栈帧文件行号；栈帧内若含参数值则截断 |
| 外部凭证（DB URI 含密码） | 连接串日志化时把密码部分替换为 `***` |

**实现要点**：
- 提供唯一的脱敏网关（sanitizer），所有 key 进入网关；
- 敏感字段名采用**字典匹配**（如含 `password`/`token`/`secret`/`authorization`）一刀切；
- 定期扫描日志库验证脱敏规则有效（自动化检查）。

### 6.6 日志存储与保留

| 阶段 | 存储 | 保留 | 说明 |
|---|---|---|---|
| 起步 | 本地文件滚动（Python `logging`：daily + max_log_files） | 本地 7~30 天 | 低成本，先跑起来 |
| 演进 | 集中式（Loki / Elasticsearch） | 30 天热 + 180 天冷 | 检索、看板、告警 |
| 审计需求强 | 审计日志独立索引/桶 | ≥ 1 年 | 合规要求另定 |

**文件滚动策略**（对齐现有日志配置能力）：
- 轮转：`daily`（生产），文件名带日期；
- 保留：`max_log_files = 90`；
- 写入：`non_blocking = true`（异步写，避免阻塞请求路径）；
- 目录：`./logs`，按类别前缀区分（`app.log`、`access.log`）。

---

## 7. Trace 追踪体系设计

### 7.1 Span 命名规范

格式：`<操作> <目标>` 或 `<层>.<操作>`，全部小写，语义稳定：

| 层 | 命名示例 |
|---|---|
| HTTP 入口 | `HTTP POST /api/v1/work-orders` |
| 数据库 | `DB QUERY production_work_orders`、`DB TXN create_work_order` |
| 缓存 | `CACHE GET mes:plant:{code}`、`CACHE SET key=... ttl=300` |
| 外部调用 | `EXT POST /internal/mes/trace` |
| 调度任务 | `JOB overnight_recompute` |

### 7.2 内置 Span 清单（基础设施层）

每个 span 必须：有名称、起止时间、状态（OK/ERROR）、至少 3 个定位属性。

```
Root Span: HTTP POST /api/v1/work-orders
├── Span: MIDDLEWARE auth (actor_id, actor_role)
├── Span: CACHE GET idempotency:{key}          # 幂等预检
├── Span: DB QUERY idempotency_keys            # 幂等复查
├── Span: DB TXN create_work_order
│   ├── Span: DB INSERT production_work_orders
│   └── Span: DB INSERT idempotency_keys
├── Span: DB QUERY production_work_orders       # 读回
└── Span: CACHE SET work_order:{number} ttl=3600
```

**统一 span 属性（semantic conventions 扩展）**：

| 属性 | 含义 |
|---|---|
| `http.method` / `http.route` / `http.status_code` | HTTP 语义 |
| `http.target` | 完整路径 |
| `db.system` / `db.statement` | DB 语义 |
| `mes.work_order_number` / `mes.material_code` 等 | MES 业务键（见 6.2 业务字段） |
| `error.type` / `exception.message` | 错误语义 |

**建议强制属性（基架层统一加）**：`service.name`、`deployment.environment`、`host.name`、`process.pid`（对齐 OTEL resource 规范）。

### 7.3 Span 事件（Event）规范

Span 上记录**带时间戳的关键事件**，比属性更细粒度：

| 事件 | 字段 |
|---|---|
| `cache.hit` / `cache.miss` | `key`、`ttl_ms` |
| `cache.fallback` | `key`、`reason`（`down`/`timeout`/`config_off`） |
| `retry` | `attempt`、`delay_ms` |
| `idempotency.replay` | `idempotency_key`、`existing_resource` |
| `db.pool.wait` | `wait_ms` |
| `db.error` | `error_type`、`message` |

### 7.4 采样策略

单实例阶段数据量可控，但多实例/高并发必须采样：

| 策略 | 适用 | 说明 |
|---|---|---|
| **头部采样（Head-based）** | 默认 | 入口按 `trace_id` 哈希比例采样（如 10%），整条链一致 |
| 强制采样 | 关键路径 | 写接口、报错请求（`status >= 500`）、慢请求（> 1s）**必须全采**，不受比例限制 |
| 尾部采样（Tail-based） | 演进阶段 | Collector 侧聚合，把含错误/慢 span 的 trace 补采 |
| 关键业务采样 | 可选 | 按 `mes.work_order_number` 前缀或操作者角色定向全采 |

> **原则**：错误永远全量可查，正常流量按成本采样。采样率是配置项（如 `OTEL_TRACES_SAMPLER_ARG=0.1`），不做硬编码。

### 7.5 Trace 后端选型

| 后端 | 适用 | 备注 |
|---|---|---|
| **Jaeger** | 首选（单机可用） | 与 `opentelemetry-python` jaeger exporter 配套；UI 成熟 |
| **Tempo (Grafana)** | 与 Grafana 大盘一体化 | 与 Loki 日志、Prometheus 指标统一入口 |
| Zipkin | 兼容 | 不建议新选 |

对接方式：应用通过 OTLP exporter 把 span 推给 **OTel Collector**，由 Collector 再转发到后端——解耦、可批量加采样/脱敏/批处理。

### 7.6 错误与状态语义

- 业务校验失败（4xx）：根 span 状态 **UNSET/OK**，只记事件（不算链路错误）；
- 服务错误（5xx / DB 失败 / 外部失败）：span 状态 **ERROR**，必带 `error.type` 和对应 ERROR 日志（日志携带同一 `span_id`）；
- 重试成功：子 span 记 `retry` 事件，父 span 仍 OK；
- 最终失败：根 span 状态 ERROR，日志与 trace 通过 `trace_id` 可交叉跳转。

---

## 8. 端到端数据流架构

### 8.1 总览

```
 客户端/上游
    │ HTTP (traceparent, x-correlation-id)
    ▼
┌─────────────────────────── 应用节点 (MES API) ───────────────────────────┐
│  HTTP 中间件：生成/提取 Trace+Correlation，注入日志上下文                   │
│  业务逻辑（handler）                                                      │
│   │── DB 访问 ──► DB Span（连接池等待、语句执行、事务）                     │
│   │── 缓存访问 ─► Cache Span（命中/未命中/TTL/降级事件）   (演进阶段)       │
│   │── 外部调用 ─► Ext Span（透传 traceparent）                             │
│   │── 定时任务 ─► JOB Span                                               │
│                                                                           │
│  Logging SDK (logging/structlog) ──► 结构化 JSON 日志 ──► 本地滚动文件      │
│                                                                           │
│  Tracing SDK (otel) ──► OTLP ──► OTel Collector                          │
└──────────────────────────────────────────────────────────────────────────┘
                                            │ OTLP / Jaeger / 批量日志
                    ┌───────────────┬────────┴───────────────┬──────────────┐
                    ▼               ▼                        ▼              ▼
              Trace 后端        日志存储                   指标存储        告警
             (Jaeger/Tempo)   (Loki/Elasticsearch)     (Prometheus)    (Alertmanager)
                    └───────────────┴────────────────────────┴──────────────┘
                                        ▲
                                        │ 统一检索 / 看板 / 关联跳转
                                    Grafana
```

### 8.2 各层职责

| 层 | 职责 | 要点 |
|---|---|---|
| **应用内 SDK** | 日志格式化、span 创建、传播、注入 | Python `logging`/`structlog` + `opentelemetry-python`，按配置启用 |
| **OTel Collector** | 接收 OTLP、批处理、采样、脱敏、转发 | 单实例可先内存直出，规模上来再独立部署 |
| **Trace 后端** | 存储与查询 trace | Jaeger 单机可 docker 起 |
| **日志管道** | 采集（Promtail/Filebeat）→ 存储 → 检索 | 从本地文件读，不改应用 |
| **Grafana** | 统一入口 | trace + 日志 + 指标 + 告警一个入口 |

### 8.3 与现有配置衔接（参考）

```toml
# pyproject.toml（Poetry）：自定义命名空间 [tool.mes.*]
# 日志：启用文件落盘 + JSON
[tool.mes.logging]
level = "INFO"
format = "json"            # 或 "console"
file_dir = "./logs"
file_prefix = "mes"
rotation = "daily"         # 文件名带日期
max_log_files = 90
non_blocking = true        # 异步写，避免阻塞请求路径

# 追踪：启用 OpenTelemetry（opentelemetry-python），W3C 传播 + Jaeger 后端
[tool.mes.tracing]
enable = true
# 其余走 OTEL 环境变量：OTEL_EXPORTER_OTLP_ENDPOINT、OTEL_TRACES_SAMPLER 等
```

---

## 9. MES 典型场景用例

### 9.1 场景一：创建工单失败排查

1. 前端收到 409 `CONFLICT`，响应 `meta.correlationId = corr-3d4f2a...`。
2. 运维用 correlationId 在日志系统查：命中 1 条 ERROR 日志，带 `trace_id=4bf92f...`、`idempotency_key=abc-123`。
3. 点 trace_id 跳转到 Jaeger：看到 `DB INSERT production_work_orders` 子 span 状态 ERROR、`db.error=UNIQUE constraint failed`。
4. 结论：幂等键预检与写入之间有竞态（并发重复提交），日志事件 `idempotency.replay` 佐证。
5. 修复方向明确：把幂等键唯一索引 + `INSERT OR IGNORE` 作为兜底。

### 9.2 场景二：缓存降级审计

演进阶段引入 Redis 后：

- 请求路径：`CACHE GET plant:{code}` → miss → `DB QUERY plants` → `CACHE SET ttl=300`。
- 若 Redis 不可用：span 事件 `cache.fallback {key, reason: connection_refused}`，WARN 日志带 `category=cache`。
- 全部记录，但不改变返回结果——满足 `DEVELOPMENT_PROGRESS.txt` 对"连接、命中/未命中、TTL、失效、降级事件"的日志要求。

### 9.3 场景三：跨日批次任务

- 定时任务根 span `JOB overnight_recompute`，每批记录 `batch_index`、`processed`、`failed` 事件；
- 中途失败：span 标记 ERROR + 事件记录失败批次号；重试时复用同一 `job_run_id`（业务关联 ID），与 trace_id 双向可查。

---

## 10. 指标设计（简述）

日志与追踪回答"发生了什么 / 为什么"，指标回答"是否健康"。起步阶段指标可后置，但**命名空间与采集通道应提前预留**。

| 指标 | 类型 | 告警建议 |
|---|---|---|
| `http_request_duration_seconds` | Histogram | p99 > 2s |
| `http_requests_total`（按 route/status） | Counter | 5xx 比例 > 1% |
| `db_query_duration_seconds` | Histogram | p99 > 500ms |
| `db_pool_connections`（idle/in_use） | Gauge | in_use 打满 |
| `cache_hit_ratio` | Gauge | < 0.8（持续） |
| `trace_sampled_ratio` | Gauge | 监控采样成本 |

方法：**RED**（Rate/Errors/Duration）针对请求，**USE**（Utilization/Saturation/Errors）针对资源。

---

## 11. 实施路线图

按阶段落地，每阶段独立可交付、可回滚、不改变接口语义。

### Phase 1：结构化日志 + 关联 ID 贯通（本周级）
- [ ] 启用 Python 日志文件落盘 + JSON 格式（生产配置）；
- [ ] 统一 `level/logger/category` 字典，清理非结构化日志；
- [ ] 中间件统一注入 `correlation_id` 到日志字段；
- [ ] 幂等键 / 审计事件补日志（含 `idempotency_key`、`actor_id`、结果）；
- [ ] 脱敏网关上线 + 日志脱敏扫描脚本；
- [ ] 验收：任意请求可在日志中通过 correlation_id 收敛。

### Phase 2：Trace 打通（月级）
- [ ] 启用 `opentelemetry-python`，W3C 传播，HTTP 入口根 span；
- [ ] DB / 缓存 / 外部调用子 span + 7.2 强制属性；
- [ ] 错误分支 `span.status = ERROR` + 日志携带 `trace_id/span_id`；
- [ ] 部署 OTel Collector + Jaeger；
- [ ] 采样策略上线（错误全采 + 头部采样）；
- [ ] 验收：任一 ERROR 日志可 1 跳进入 trace 树。

### Phase 3：集中化与看板（季度级）
- [ ] Promtail + Loki 采集本地日志；Grafana 统一入口；
- [ ] trace↔日志↔指标关联（`trace_id` / `correlation_id` 字段联动）；
- [ ] 访问日志独立流、审计日志保留策略；
- [ ] 基础告警（5xx 比例、DB 延迟、错误日志风暴）；
- [ ] 验收：一条工单创建链路可在 Grafana 一个页面全览。

### Phase 4：演进（按需）
- [ ] Redis 缓存事件（hit/miss/TTL/fallback）接入 span 与日志；
- [ ] 业务指标（RED/USE）与业务大盘；
- [ ] 尾部采样、多服务 trace 贯通；
- [ ] 日志成本治理（级别动态调整、字段裁剪、冷热分层）。

---

## 12. 红线与验收清单

### 12.1 红线（不可妥协）

1. **严禁**在日志/span 中记录密码、密钥、token、cookie、完整敏感请求体/响应体。
2. **严禁**把业务主键拼进 message 字符串（必须为结构化字段）。
3. **严禁**生产环境开启 DEBUG 级别（除非临时故障排查并记录恢复时间）。
4. **严禁**吞掉错误却不留日志（`catch 后无日志` = 事故隐患）。
5. **严禁**在日志里输出数据库连接串明文（URI 密码须打码）。
6. 日志与追踪不得改变业务结果，降级路径必须可观测（事件+WARN）。

### 12.2 验收清单（自检）

- [ ] 每条日志含 `timestamp/level/logger/message/trace_id/correlation_id`；
- [ ] 日志 JSON 可被机器解析，无拼接文本；
- [ ] 错误日志可跳转 trace，trace 可跳转日志；
- [ ] 写接口日志含 `idempotency_key` 与 `actor_id`；
- [ ] 脱敏扫描 0 命中敏感数据；
- [ ] 采样配置明确，错误全量可查；
- [ ] 保留策略与成本符合预期；
- [ ] 新代码的日志/span 通过 Code Review 按本规范检查。

---

## 附：关键决策记录（ADR 摘要）

| 决策 | 选择 | 原因 |
|---|---|---|
| 传播协议 | W3C Trace-Context | 标准、通用，框架默认支持 |
| 日志格式 | JSON Lines | 机器可读、采集器兼容 |
| Trace 后端 | Jaeger（先）/ Tempo（若用 Grafana 全家桶） | 单机可用、生态成熟 |
| Correlation 与 Trace 关系 | 一一对应，correlation 对外、trace 对内 | 保持现有响应契约不变 |
| 采样原则 | 错误全采 + 头部采样 + 关键路径强制全采 | 成本与可观测性平衡 |
| 缓存 | 演进阶段再引入，日志事件先行 | 遵循 DEVELOPMENT_PROGRESS 节奏，不破坏接口语义 |
