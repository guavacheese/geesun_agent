"""OpenInference 追踪初始化模块（双 exporter：Phoenix + Langfuse + metrics）。

必须在任何 LangChain / LangGraph / deepagents 被 import 之前调用 setup_tracing()。
这是因为 OpenInference 的 auto-instrumentation 需要在模块加载时 hook 进去。

使用方法（在 server.py 顶部）：

    from src.core.logging import *         # 日志最先就绪
    from src.core.tracing import setup_tracing
    setup_tracing()                         # ← 此时还没有 import LangChain
    from .api.router import api_router      # ← 安全了

═══════════════════════════════════════════════════════════════════════════════
2026-09-10 重大修正：从「全关」改为「断源 + 过滤后恢复」
═══════════════════════════════════════════════════════════════════════════════
ea303fe（2026-09-09）因观察到"有非 LLM 数据在灌观测后端"而把三路 exporter 全部
注释。但真实根因不是应用日志误入 OTLP，而是 **容器 healthcheck 每 15s 探活
`/docs`** 触发的 FastAPI HTTP server span。实测生产 Phoenix spans 表 108,209 条：

    HTTP 形态         106,368 = 98.3%
    其中 GET /docs     99,326 = 91.8%（健康检查：5760 次/天 × 3 span）
    真实 LLM trace     ≈1,200 =  1.1%

全关三路 exporter 的代价是连那 1.1% 有价值数据一起断掉（含 metrics —— Prometheus
里 gen_ai_* / http_server_* 全部归零，相关看板失效）。本次改为：

  1) 断源：healthcheck 换 `/healthz`；FastAPIInstrumentor 用 excluded_urls 排除
     健康检查与文档端点 —— 这些请求连 span 都不再创建（在 ASGI 入口直接 return）；
  2) 过滤：三路 exporter 全部包在 _OpenInferenceOnlySpanProcessor 内，只放行带
     `openinference.span.kind` 的 OpenInference span，HTTP/ASGI span 一律丢弃；
  3) 恢复：Phoenix + Langfuse + metrics 三路全部重新启用。

完整复盘见 docs/tracing-span-pollution-postmortem.md。
═══════════════════════════════════════════════════════════════════════════════
"""

import logging
import os

logger = logging.getLogger(__name__)

_initialized = False

# ── span 过滤判据 ──
# OpenInference 埋点（LangChain/LangGraph/deepagents）**一定**设置该属性：
#   openinference/instrumentation/langchain/_tracer.py:294
#       span.set_attribute(OPENINFERENCE_SPAN_KIND, span_kind.value)
#   且同文件 210-217 行显示 _update_span() 在 span.end() **之前**调用
#   → SpanProcessor.on_end() 时必定可读到。
# OTel 的 HTTP/ASGI 埋点（opentelemetry.instrumentation.fastapi/asgi）**从不**设置它。
# 实测对照（Phoenix spans 表的 span_kind 列，Phoenix 正是从该属性推导）：
#   UNKNOWN 106,434（其中 HTTP 占 106,368）| CHAIN 1,119 | LLM 261 | TOOL 257 | AGENT 138
# 取值与 openinference.semconv.trace.SpanAttributes.OPENINFERENCE_SPAN_KIND 同值；
# 此处硬编码，避免为一个字符串常量额外引入 import 依赖。
_OPENINFERENCE_SPAN_KIND = "openinference.span.kind"

# ── FastAPIInstrumentor 不埋点的 URL（逗号分隔的正则片段）──
# 全部是探活/文档类端点，永远不是业务请求。排除后连 span 都不创建 —— 区别于
# 事后过滤：excluded_urls 在 ASGI middleware 入口直接 return，零开销。
# 匹配语义见 opentelemetry/util/http/__init__.py:82 `url_disabled()`（用 re.search，
# 不是 re.match，故无需写 `^...$` 锚点）。
_HTTP_EXCLUDED_URLS = "healthz,/api/health,/docs,/openapi.json,/redoc"


try:
    from opentelemetry.sdk.trace import SpanProcessor as _SpanProcessorBase
except ImportError:  # pragma: no cover — opentelemetry-sdk 是硬依赖，此处仅形式防御
    _SpanProcessorBase = object  # type: ignore[assignment,misc]


class _OpenInferenceOnlySpanProcessor(_SpanProcessorBase):  # type: ignore[misc]
    """只放行 OpenInference span，HTTP/ASGI 等基础设施 span 静默丢弃。

    包装任意下游 SpanProcessor（SimpleSpanProcessor / BatchSpanProcessor），
    在 on_end 处按 `openinference.span.kind` 属性存在与否分流。

    on_start 不做过滤：该属性由 instrumentation 在 span 结束前才写入（见上文），
    start 时读不到，转发给下游即可（SDK 内置两个 processor 的 on_start 都是 pass，
    转发仅为语义完整）。

    计数为类级 —— Phoenix/Langfuse 各挂一个实例，共享同一统计，shutdown 时打
    一条 info 日志便于确认过滤是否生效。
    """

    _dropped = 0
    _passed = 0

    def __init__(self, downstream) -> None:
        self._downstream = downstream

    def on_start(self, span, parent_context=None) -> None:
        try:
            self._downstream.on_start(span, parent_context)
        except Exception:  # 观测链路异常不得影响业务
            logger.debug("[TRACING] on_start 转发下游异常", exc_info=True)

    def on_end(self, span) -> None:
        try:
            attributes = getattr(span, "attributes", None)
            if attributes and _OPENINFERENCE_SPAN_KIND in attributes:
                _OpenInferenceOnlySpanProcessor._passed += 1
                self._downstream.on_end(span)
            else:
                _OpenInferenceOnlySpanProcessor._dropped += 1
        except Exception:
            logger.debug("[TRACING] span 过滤异常", exc_info=True)

    def shutdown(self) -> None:
        logger.info(
            "[TRACING] span 过滤统计 — 放行=%d 丢弃=%d "
            "（丢弃项 = HTTP/ASGI 等非 OpenInference span）",
            _OpenInferenceOnlySpanProcessor._passed,
            _OpenInferenceOnlySpanProcessor._dropped,
        )
        try:
            self._downstream.shutdown()
        except Exception:
            logger.debug("[TRACING] 下游 shutdown 异常", exc_info=True)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._downstream.force_flush(timeout_millis)


def setup_tracing() -> bool:
    """初始化 OpenInference 追踪，向 Phoenix + Langfuse 上报 trace，并注册 metrics。

    ★ 重要：不再使用 phoenix.otel.register() 来配置 Phoenix exporter，
    因为它的 TracerProvider.add_span_processor() 会替换已有 processor。
    改为手动创建 TracerProvider 并显式添加两个处理器 —— 且各自包一层
    _OpenInferenceOnlySpanProcessor，从源头挡住 HTTP/ASGI span。

    :return: 是否至少启用了一路 trace exporter
    """
    global _initialized
    if _initialized:
        return True

    from src.core.config import settings

    try:
        from opentelemetry import trace as trace_api
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GrpcExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HttpExporter,
        )
        from opentelemetry.sdk import trace as trace_sdk
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import (
            SimpleSpanProcessor,
            BatchSpanProcessor,
        )
        # ── Metrics SDK（2026-09-03 补链路：此前只注册 trace → gen_ai_*/http_server_* 全 0）──
        from opentelemetry import metrics as metrics_api
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            PeriodicExportingMetricReader,
        )
        from openinference.instrumentation.langchain import LangChainInstrumentor

        # FastAPI instrumentor 是新增依赖（pyproject ≥0.50b0），缺包时仅 http_server
        # 指标缺席、其余 trace/metrics 不受影响 → 单独降级，不拖垮整个 setup_tracing()
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        except ImportError:
            FastAPIInstrumentor = None
            logger.warning(
                "[TRACING] opentelemetry-instrumentation-fastapi 未安装 — "
                "http_server_* 指标缺席（缺依赖可后续 uv sync 补齐）"
            )

        resource = Resource.create({
            # service.name 缺省时 OTel SDK 给 unknown_service → alloy prometheus exporter
            # 映射成 job="unknown_service"（2026-09-07 实测 gen_ai_*/http_server_* 全中招）。
            # 必须显式给，SDK 不会自动从 OTEL_SERVICE_NAME env 合并到 create() 的 resource。
            "service.name": settings.otel_service_name,
            # Phoenix 19.x 按标准 OTel `project.name` 资源属性分组项目；
            # 旧版 OpenInference 用 `openinference.project.name`，现代 Phoenix 已忽略，
            # 缺失 `project.name` 时所有 trace 落入内置 "default" 项目（2026-09-02 实测）。
            "project.name": settings.otel_project_name,
            "openinference.project.name": settings.otel_project_name,
        })
        tracer_provider = trace_sdk.TracerProvider(resource=resource)

        # ── 1. Phoenix gRPC exporter（包 OpenInference span 过滤）──
        phoenix_endpoint = settings.phoenix_collector_endpoint
        if phoenix_endpoint:
            os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = phoenix_endpoint
            tracer_provider.add_span_processor(
                _OpenInferenceOnlySpanProcessor(
                    SimpleSpanProcessor(GrpcExporter(endpoint=phoenix_endpoint))
                )
            )
            logger.info(
                "[TRACING] Phoenix gRPC exporter 已添加 — endpoint=%s"
                "（已挂 OpenInference span 过滤）",
                phoenix_endpoint,
            )
        else:
            logger.warning(
                "[TRACING] phoenix_collector_endpoint 为空 — Phoenix exporter 跳过"
            )

        # ── 2. Langfuse HTTP exporter（包 OpenInference span 过滤）──
        if settings.langfuse_secret_key and settings.langfuse_base_url:
            import base64

            auth_bytes = base64.b64encode(
                f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
            )
            headers = {
                "Authorization": f"Basic {auth_bytes.decode()}",
                "x-langfuse-ingestion-version": "4",
            }
            langfuse_endpoint = (
                f"{settings.langfuse_base_url.rstrip('/')}"
                "/api/public/otel/v1/traces"
            )
            tracer_provider.add_span_processor(
                _OpenInferenceOnlySpanProcessor(
                    BatchSpanProcessor(
                        HttpExporter(endpoint=langfuse_endpoint, headers=headers)
                    )
                )
            )
            logger.info(
                "[TRACING] Langfuse HTTP exporter 已添加 — endpoint=%s"
                "（已挂 OpenInference span 过滤）",
                langfuse_endpoint,
            )
        else:
            logger.warning(
                "[TRACING] LANGFUSE 配置不完整 — Langfuse exporter 跳过"
            )

        # ── 3. Metrics：MeterProvider + OTLP gRPC exporter ──
        # metrics 与 trace 共用同一 OTLP gRPC 端点（生产 = alloy:4317 统一入口，
        # grpc 同端口按 OTLP service path 分流 trace/metrics，alloy 侧零改动）；
        # 15s 周期导出（PeriodicExportingMetricReader），兼顾观察时效与开销。
        # 注意：span 过滤只作用于 trace，metrics 不受影响（http_server_* / gen_ai_* 照常）。
        metrics_registered = False
        if settings.otel_metrics_enabled:
            metrics_endpoint = settings.otel_metrics_endpoint or phoenix_endpoint
            if metrics_endpoint:
                try:
                    metric_reader = PeriodicExportingMetricReader(
                        OTLPMetricExporter(
                            endpoint=metrics_endpoint,
                            timeout=5,
                        ),
                        export_interval_millis=15000,
                    )
                    meter_provider = MeterProvider(
                        metric_readers=[metric_reader],
                        resource=resource,
                    )
                    metrics_api.set_meter_provider(meter_provider)
                    metrics_registered = True
                    logger.info(
                        "[METRICS] MeterProvider 已注册 — OTLP gRPC endpoint=%s, 导出周期=15s",
                        metrics_endpoint,
                    )
                except Exception as e:
                    logger.warning("[METRICS] MeterProvider 初始化异常: %s", e)
            else:
                logger.warning(
                    "[METRICS] otel_metrics_enabled=True 但无 endpoint "
                    "（phoenix_collector_endpoint 为空）— metrics 跳过"
                )

        # ── 4. 激活 ──
        trace_api.set_tracer_provider(tracer_provider)
        LangChainInstrumentor().instrument()

        # HTTP server instrument（http_server_* metrics + HTTP server span）。
        # 须在 FastAPI app 实例化前 instrument —— setup_tracing() 位于 server.py 顶部、
        # import api.router 之前（模块 docstring 已声明），顺序安全。
        #
        # ⚠️ 顺序铁律（2026-09-10 实测）：FastAPIInstrumentor.instrument() 的实现是
        #    `fastapi.FastAPI = _InstrumentedFastAPI`（patch 类属性，见
        #    opentelemetry/instrumentation/fastapi/__init__.py:442-445），因此调用方
        #    **必须在本函数之后**才执行 `from fastapi import FastAPI` —— 否则该名字
        #    绑定到未被 patch 的旧类，创建的 app 完全不被埋点，且不报任何错。
        #    server.py 当前顺序（L13 setup_tracing → L17 from fastapi import FastAPI）
        #    正确；改动 server.py 顶部 import 顺序时务必保持这一点。
        #    被排除的 URL 见 _HTTP_EXCLUDED_URLS（在 ASGI middleware 入口直接 return，
        #    既不建 span，也不计入 http_server_* 指标）。
        if FastAPIInstrumentor is not None:
            try:
                FastAPIInstrumentor().instrument(
                    excluded_urls=_HTTP_EXCLUDED_URLS,
                )
                logger.info(
                    "[TRACING] FastAPIInstrumentor 已注册"
                    "（http_server_* metrics + HTTP span；已排除 URL: %s）",
                    _HTTP_EXCLUDED_URLS,
                )
            except Exception as e:
                logger.warning("[TRACING] FastAPIInstrumentor 注册失败: %s", e)

        was_setup = bool(
            phoenix_endpoint
            or (settings.langfuse_secret_key and settings.langfuse_base_url)
        )
        _initialized = True
        logger.info(
            "[TRACING] OpenInference 初始化完成 — "
            "auto_instrument=langchain, "
            "Phoenix=%s, Langfuse=%s, Metrics=%s, span_filter=OpenInferenceOnly",
            bool(phoenix_endpoint),
            bool(settings.langfuse_secret_key and settings.langfuse_base_url),
            metrics_registered,
        )
        return was_setup

    except ImportError as e:
        logger.warning(
            "[TRACING] 依赖缺失 (%s) — 请检查是否安装了 "
            "openinference-instrumentation-langchain 和 "
            "opentelemetry-exporter-otlp-proto-http",
            e,
        )
        return False
    except Exception as e:
        logger.warning("[TRACING] 初始化异常: %s", e)
        return False
