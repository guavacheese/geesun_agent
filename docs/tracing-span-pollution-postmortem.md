# 观测后端污染复盘：healthcheck 把 98% 的 HTTP span 灌进了 Phoenix/Langfuse

> 沉淀日期：2026-09-10
> 触发案例：`ea303fe`（2026-09-09）全局禁用 trace → 本次修正（`tracing.py` 重写 + `/healthz` + compose healthcheck）
> 适用范围：任何「FastAPI / ASGI 埋点 + 容器 healthcheck」的服务
> 相关文档：`docs/sse-streaming-debug.md`、`docs/llm-reasoning-field-passthrough.md`

---

## 0. 一句话结论

**「一版应用 log 被打进了 Langfuse/Phoenix」是个误判 —— 灌进去的不是日志，是 HTTP server span；
真凶是容器 healthcheck 每 15s 探活 `/docs`。**

`ea303fe` 的处置方向对（确实有非 LLM 数据在灌）、但归因错、手段过粗：一条注释把
**Phoenix + Langfuse + metrics 三路全关**，连真正有价值的 1.1% LLM trace 一起关掉了，
且 `FastAPIInstrumentor` 仍在埋点（span 照造、白耗 CPU）。

本次改为「**断源 + 过滤 + 恢复**」三层，实测 18/18 项验证通过。

---

## 1. 现象与实测影响

直接查生产 Phoenix 自带的 PostgreSQL（`spans` 表）：

| span_kind | 数量 | 其中 HTTP 形态 | 说明 |
|---|---:|---:|---|
| **UNKNOWN** | **106,434** | **106,368** | HTTP server span + `http send` 子 span |
| CHAIN | 1,119 | 0 | LangChain/LangGraph/deepagents |
| LLM | 261 | 0 | |
| TOOL | 257 | 0 | |
| AGENT | 138 | 0 | |
| **合计** | **108,209** | **106,368 = 98.3%** | 真实 LLM trace 仅 **≈1,200 = 1.1%** |

span 名称 TOP：

```
GET /docs http send          66,220
GET /docs                    33,106     ← 二者合计 99,326 = 91.8%（健康检查）
POST /api/v1/chat http send   6,020
model / ChatOpenAI / tools     ~780     ← 真实 LLM span
```

Langfuse 侧（`/api/public/traces?limit=30`）**30 条全是** `name="GET /docs"`，
`scope=opentelemetry.instrumentation.fastapi`、`http.user_agent=Python-urllib/3.13`、
相邻两条间隔 `Δ=15.2~15.3s`。

---

## 2. 证据链

### 2.1 「不是日志」：六条 OTLP 日志通道逐一证伪

| 候选通道 | 实测结论 |
|---|---|
| 代码里有 `LoggingHandler` / `LoggerProvider` | **0 命中**；`git log -S` 全历史也 0 |
| `opentelemetry-instrumentation-logging` 包 | 未安装 |
| **OTel SDK 自动装配**（`opentelemetry/sdk/_configuration/__init__.py:320-333` 会把 `LoggingHandler` 挂到 **root logger**，导出**全部**应用日志 —— 这条暗路真实存在） | **无法触发**：无 `opentelemetry-distro` → `opentelemetry_configurator` entry point 不存在 → `_OTelSDKConfigurator` 永不注册；项目 `OTEL_*` 环境变量 0 个；容器 CMD 是裸 `uv run uvicorn` 而非 `opentelemetry-instrument` |
| `phoenix/otel/otel.py`（`arize-phoenix-otel`）偷装 logging | 全文 **0 处** logging instrumentation 引用 |
| alloy 转发 OTLP logs | `deploy/alloy.config.alloy` 的 `otelcol.receiver.otlp` 只输出 **metrics→Prometheus / traces→Phoenix**，**logs 无出口，静默丢弃** |
| 应用 stdout | 只去 Loki |

⇒ **应用日志在本项目的 OTLP 链路上不可能外流。**

### 2.2 「是 HTTP span」：源头锁定

`deploy/docker-compose.yml` 的 healthcheck（改动前 L78-83）：

```yaml
test: ["CMD","python3","-c","import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8009/docs').status==200 else 1)"]
interval: 15s
```

三条独立证据同时指向它：

1. Langfuse 里的 `http.user_agent` = **`Python-urllib/3.13`** —— 不是 curl、不是浏览器；
2. URL 恰好是 **`/docs`**，与 healthcheck 探活路径逐字一致；
3. 相邻 trace 间隔 **15.25s**，与 `interval: 15s` 吻合。

**数值自校验**：`86400 / 15 = 5,760` 次/天 × 3 span（server + receive + send） = **17,280 span/天**，
与 Phoenix 实测的按天计数 `09-05: 16,986`、`09-06: 16,983`、`09-07: 17,252` 吻合。

---

## 3. 真实根因链路

```
docker healthcheck（每 15s）
  └─ python3 urllib.request.urlopen("http://127.0.0.1:8009/docs")
       └─ FastAPIInstrumentor 的 ASGI middleware
            └─ 为每个请求创建 3 个 span（SERVER + receive + send）
                 └─ TracerProvider 的两个 exporter（Phoenix gRPC / Langfuse HTTP）
                      └─ alloy:4317 → Phoenix        +  10.10.10.67:3000 → Langfuse
```

`/docs` 只是"顺手拿来做探活"的端点 —— 它恰好是 FastAPI 路由，于是每一个探活请求
都变成一条完整的 trace。

---

## 4. `ea303fe` 判定：方向对、归因错、手段过粗

| 维度 | 判定 | 依据 |
|---|---|---|
| 感知到「有非 LLM 数据在灌」 | ✅ 方向对 | HTTP span 确实占 98.3% |
| 归因 | ❌ **错** | commit 写的是「本地 `.env` 误拷生产 endpoint/key → exporter 报错刷屏 server.log」—— 那是**另一个问题**（日志刷屏），对 HTTP span 污染只字未提 |
| 手段 | ❌ **过粗** | 一条注释把 Phoenix + Langfuse + **metrics** 三路全关 → 真正有价值的 1.1% LLM trace 一并断掉 |
| 有效性 | ❌ **治标不治本** | `FastAPIInstrumentor` 仍注册（容器日志仍在打 `[TRACING] FastAPIInstrumentor 已注册`）→ **span 照造**，只是没人收，白耗 CPU |
| commit message 一致性 | ⚠️ 自相矛盾 | Impact 段同时写「生产零影响」与「生产需取消注释才能恢复」，**实测生产同样被关停** |

**生产被关停的实测证据**：

- 1.0.6 容器内 `tracing.py`：活跃 `add_span_processor` = **0**，日志 `exporter=DISABLED`
- Langfuse 最新 trace `09-10 15:07:59 CST`（1.0.5 容器 15:08:27 被替换），此后 **2h20m 零新增**
- Phoenix 最后 span `2026-09-10 07:08:14Z`，同刻断流
- Prometheus `count({__name__=~"gen_ai_.*"})` **返空**（对照 `up=4`、`otelcol_*`=6 正常）
  → **metrics 也一并断了**，`gen_ai_*` / `http_server_*` 那套看板全灭

---

## 5. 修复：断源 + 过滤 + 恢复

### 第 1 层（断源）：healthcheck 换 `/healthz`

| 文件 | 改动 |
|---|---|
| `src/server.py:134` | 新增 `@app.get("/healthz", include_in_schema=False)`，返回 `{"status":"ok"}`，**不查数据库**（与原 `/docs` 探活语义等价） |
| `deploy/docker-compose.yml:78-89` | `test` 路径 `/docs` → `/healthz`；`interval` 15s → 30s；`retries` 10 → 5（判死窗口仍为 `30s × 5 = 150s`，与原 `15s × 10` 等价，但无效请求减半） |

### 第 2 层（断源加固）：`excluded_urls` 在 ASGI 入口短路

`src/core/tracing.py:62`：

```python
_HTTP_EXCLUDED_URLS = "healthz,/api/health,/docs,/openapi.json,/redoc"
```

`src/core/tracing.py:295` 传给 `FastAPIInstrumentor().instrument(...)`。

命中后 asgi middleware **直接 `return await self.app(...)`**
（`opentelemetry/instrumentation/asgi/__init__.py:747-748`）——
既不建 span，也不计入 `http_server_*` 指标。零开销，区别于"事后过滤"。

> 匹配语义：`opentelemetry/util/http/__init__.py:82-83` 的 `url_disabled()` 用 **`re.search`**
> （不是 `re.match`），故无需 `^...$` 锚点；多项以 `|` 拼成一个正则整体。
> `,/docs` 带前导斜杠是刻意的 —— `/api/v1/documents` 不会被误伤（第 5 字符是 `u` 不是 `s`）。

### 第 3 层（过滤）：只放行 OpenInference span

`src/core/tracing.py:71` 新增 `_OpenInferenceOnlySpanProcessor`，包住两个 exporter
（`:194` Phoenix / `:224` Langfuse）。

判据：`span.attributes` 中是否存在 **`openinference.span.kind`**（`src/core/tracing.py:55`）。

为什么这个判据可靠：

- OpenInference 埋点**一定**设置它 ——
  `openinference/instrumentation/langchain/_tracer.py:294`
  `span.set_attribute(OPENINFERENCE_SPAN_KIND, span_kind.value)`；
  且同文件 `:210-217` 显示 `_update_span()` 在 `span.end()` **之前**调用
  → `SpanProcessor.on_end()` 时必定可读到；
- OTel 的 HTTP/ASGI 埋点（`opentelemetry.instrumentation.fastapi` / `asgi`）**从不**设置它；
- Phoenix `spans` 表的 `span_kind` 列正是从该属性推导 —— 实测 UNKNOWN 全是 HTTP、
  CHAIN/LLM/TOOL/AGENT 全是非 HTTP（见第 1 节表格），交叉验证成立。

**有意接受的副作用**：HTTP server span 被丢弃后，chain/LLM span 的 `parent_span_id`
指向不存在的 span，Phoenix/Langfuse 会把它当作所属 trace 的 root 展示。trace 仍按同一
`trace_id` 聚合（spike 场景 ⑥ 已验证），可读性不受影响；换来的是观测后端零噪音。

### 第 4 层（恢复）：三路 exporter 全部重新启用

`src/core/tracing.py:186-296`：Phoenix gRPC（`SimpleSpanProcessor`）+ Langfuse HTTP
（`BatchSpanProcessor`）+ Metrics（`MeterProvider` / `PeriodicExportingMetricReader`，15s）。

> span 过滤**只作用于 trace**，metrics 不受影响 —— `http_server_*` / `gen_ai_*` 照常上报。

---

## 6. 验证

`tests/spikes/tracing_span_filter.py`，**18/18 PASS**（用生产同款镜像
`geesun-agent:1.0.6` 的 venv 解释器运行，真实 OTel SDK + 真实 LangChain Runnable +
真实 FastAPI 请求，无 mock、无外部 LLM/网络）。

核心对照实验（同一进程内挂两个 exporter）：

| exporter | 总计 | OpenInference span | HTTP span |
|---|---:|---:|---:|
| **raw**（不过滤） | 6 | 3 | **3** |
| **kept**（经 `_OpenInferenceOnlySpanProcessor`） | 3 | **3** | **0** |

- `/healthz` 请求在 raw exporter 中**零 span** → `excluded_urls` 生效 ✅
- `/api/v1/biz` 在 raw 中产生 3 条 HTTP span、kept 中 0 条 → 过滤生效 ✅
- 3 条真实 LangChain CHAIN span（`span.kind` 取值 `['CHAIN']`）在 kept 中一条不漏 → 不误杀 ✅
- 边界：attributes 为 `None` / 空 dict / 无该属性 → 丢弃；值为空串 `""` → 放行（仅判存在性）✅
- 生命周期：`on_start` / `force_flush` / `shutdown` 均正确转发 ✅
- `excluded_urls`：命中探活/文档端点 6/6，不误伤业务 URL 8/8 ✅

运行方式（`.venv` 是 Linux 布局，Windows 本机 Python 无法直接使用，故走容器）：

```bash
docker run --rm \
  -v 'D:/workspace/geesun_agent/src:/app/src:ro' \
  -v 'D:/workspace/geesun_agent/tests:/app/tests:ro' \
  --entrypoint sh \
  172.16.220.74:8333/geesun_ai/geesun-agent:1.0.6 \
  -c 'cd /app && /app/.venv/bin/python tests/spikes/tracing_span_filter.py'
```

---

## 7. 铁律（每条都有实测依据）

### 铁律 1：注释 exporter ≠ 停止埋点

OTLP 有清晰的四层，很多人把它们混为一谈：

| 层 | 包 | 职责 |
|---|---|---|
| API | `opentelemetry-api` | 全局 provider 注册点 |
| SDK | `opentelemetry-sdk` | `TracerProvider` / `SpanProcessor` / `MeterProvider` |
| 传输 | `opentelemetry-exporter-otlp-proto-{grpc,http}` | **一个包同时给 traces/metrics/logs 三种 exporter**（entry_points 三个同名 `otlp_proto_http`）—— 用哪个**类**才决定发哪种信号 |
| 埋点 | `*-instrumentation-*` / `openinference-*` | **只造 span，不发送** |

**注释掉 exporter 只是"没人收"，埋点仍在跑。** 要真正止损必须动埋点层
（`excluded_urls` / 卸载 instrumentor / 过滤 processor）。

补充两条易错认知：
- **`arize-phoenix-otel` 只是便捷封装**（`register()` = 装 provider + exporter），
  它的 `add_span_processor` 会**替换**已有 processor —— 这正是本项目弃用它、手写
  `TracerProvider` 的原因；
- **Langfuse 不是 SDK**，只是一个 OTLP **traces** 接收端
  （`{base}/api/public/otel/v1/traces` + Basic auth）。它收到什么，100% 取决于你给它挂了哪些 span。

### 铁律 2：`FastAPIInstrumentor` 的 import 顺序（静默失效陷阱）

`FastAPIInstrumentor.instrument()` 的实现是 **patch 类属性**
（`opentelemetry/instrumentation/fastapi/__init__.py:442-445`
`fastapi.FastAPI = _InstrumentedFastAPI`），因此：

> 调用方 **必须** 在 `instrument()` **之后**才执行 `from fastapi import FastAPI`。

顺序反了会让该名字绑定到未被 patch 的旧类 —— 创建的 app **完全不被埋点，且不报任何错**。
本 spike 首轮就栽在这里（18 项里 2 项 FAIL，raw exporter 一条 HTTP span 都没有）。

`src/server.py` 当前顺序正确（`setup_tracing()` 在 L13，`from fastapi import FastAPI` 在 L17），
**改动 server.py 顶部 import 顺序时必须保持**。

### 铁律 3：只走 `./start_stack.sh`，别手工裸跑 `docker stack deploy`

`docker stack deploy` **不像 `docker compose` 那样自动读 cwd 的 `.env`**，
变量展开只认 shell 环境（`start_stack.sh:66-71` 的 `set -a; source .env; set +a` 是必需的）。
而 `POSTGRES_HOST: "${AGENT_PG_HOST:-agent-postgres}"` 的默认值恰好就是生产的正确值 ——
"忘记 source `.env`" 在 PG 这一项上**不报错、静默连本地容器库**。

---

## 附：本次改动文件索引

| 文件 | 改动 |
|---|---|
| `src/core/tracing.py` | 重写：新增 `_OpenInferenceOnlySpanProcessor`（L71）、`_OPENINFERENCE_SPAN_KIND`（L55）、`_HTTP_EXCLUDED_URLS`（L62）；恢复三路 exporter 并各自包过滤（L194 / L224）；`FastAPIInstrumentor().instrument(excluded_urls=...)`（L295）；模块 docstring 重写为本次修正的记录 |
| `src/server.py` | L134 新增 `/healthz`（`include_in_schema=False`，不查库） |
| `deploy/docker-compose.yml` | L78-89 healthcheck：`/docs` → `/healthz`，`interval` 15s → 30s，`retries` 10 → 5 |
| `tests/spikes/tracing_span_filter.py` | 新增：18 项端到端验证 |
