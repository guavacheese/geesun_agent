"""OpenInference 追踪初始化模块（双 exporter：Phoenix + Langfuse）。

必须在任何 LangChain / LangGraph / deepagents 被 import 之前调用 setup_tracing()。
这是因为 OpenInference 的 auto-instrumentation 需要在模块加载时 hook 进去。

使用方法（在 server.py 顶部）：

    from src.core.logging import *         # 日志最先就绪
    from src.core.tracing import setup_tracing
    setup_tracing()                         # ← 此时还没有 import LangChain
    from .api.router import api_router      # ← 安全了
"""

# import os  # [2026-09-09] 仅 exporter 启用时用于 os.environ 注入，已随禁用注释
import logging

logger = logging.getLogger(__name__)

_initialized = False


def setup_tracing() -> bool:
    """初始化 OpenInference 追踪，同时向 Phoenix 和 Langfuse 上报 trace。

    ★ 重要：不再使用 phoenix.otel.register() 来配置 Phoenix exporter，
    因为它的 TracerProvider.add_span_processor() 会替换已有 processor。
    改为手动创建 TracerProvider 并显式添加两个处理器。
    """
    global _initialized
    if _initialized:
        return True

    from src.core.config import settings

    try:
        from opentelemetry import trace as trace_api
        from opentelemetry.sdk import trace as trace_sdk
        from opentelemetry.sdk.resources import Resource
        # [2026-09-09] 以下 import 仅 exporter 启用时使用，已随禁用一并注释（恢复时取消）：
        # from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        #     OTLPSpanExporter as GrpcExporter,
        # )
        # from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        #     OTLPSpanExporter as HttpExporter,
        # )
        # from opentelemetry.sdk.trace.export import (
        #     SimpleSpanProcessor,
        #     BatchSpanProcessor,
        # )
        # # ── Metrics SDK（2026-09-03 补链路：此前只注册 trace → genai_*/http_server_* 全 0）──
        # from opentelemetry import metrics as metrics_api
        # from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        #     OTLPMetricExporter,
        # )
        # from opentelemetry.sdk.metrics import MeterProvider
        # from opentelemetry.sdk.metrics.export import (
        #     PeriodicExportingMetricReader,
        # )
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

        # ── 1. Phoenix gRPC exporter ──
        # [2026-09-09 开发环境临时禁用] 本地开发不接 Phoenix（本机 .env 指向生产
        # 10.10.10.67:4317，本地网络不通 → No route to host 无限刷屏）。
        # 生产部署由 deploy/.env 注入 http://alloy:4317，与此无关。
        # 恢复：取消本块注释 + 本地 .env 配 dev 工程 endpoint 即可。
        # [2026-09-09] 本行原为 `phoenix_endpoint = settings.phoenix_collector_endpoint`，
        # 被下方 Phoenix/metrics 注释块引用——恢复时先取消本行注释。
        # phoenix_endpoint = settings.phoenix_collector_endpoint
        # if phoenix_endpoint:
        #     os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = phoenix_endpoint
        #     tracer_provider.add_span_processor(
        #         SimpleSpanProcessor(
        #             GrpcExporter(endpoint=phoenix_endpoint)
        #         )
        #     )
        #     logger.info(
        #         "[TRACING] Phoenix gRPC exporter 已添加 — endpoint=%s",
        #         phoenix_endpoint,
        #     )

        # ── 2. Langfuse HTTP exporter ──
        # [2026-09-09 开发环境临时禁用] 同上——本地 .env 用的生产 Langfuse PK/SK 与
        # 10.10.10.67:3000 实例不匹配 → 401 Unauthorized 无限刷屏。
        # 恢复：取消本块注释 + 本地建 dev Langfuse 工程并把 PK/SK 配进 .env。
        # if settings.langfuse_secret_key and settings.langfuse_base_url:
        #     import base64
        #
        #     auth_bytes = base64.b64encode(
        #         f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
        #     )
        #     headers = {
        #         "Authorization": f"Basic {auth_bytes.decode()}",
        #         "x-langfuse-ingestion-version": "4",
        #     }
        #     langfuse_endpoint = (
        #         f"{settings.langfuse_base_url.rstrip('/')}"
        #         "/api/public/otel/v1/traces"
        #     )
        #     tracer_provider.add_span_processor(
        #         BatchSpanProcessor(
        #             HttpExporter(endpoint=langfuse_endpoint, headers=headers)
        #         )
        #     )
        #     logger.info(
        #         "[TRACING] Langfuse HTTP exporter 已添加 — endpoint=%s",
        #         langfuse_endpoint,
        #     )
        # else:
        #     logger.warning(
        #         "[TRACING] LANGFUSE 配置不完整 — Langfuse exporter 跳过"
        #     )

        # ── 3. Metrics：MeterProvider + OTLP gRPC exporter（2026-09-03 补链路）──
        # 根因：此前只注册了 TracerProvider → metrics API 走 no-op，进程从未发出任何
        # OTLP metrics 请求（Prometheus 中 genai_*/http_server_* 全 0 且无 series）。
        # metrics 与 trace 共用同一 OTLP gRPC 端点（生产 = alloy:4317 统一入口，
        # grpc 同端口按 OTLP service path 分流 trace/metrics，alloy 侧零改动）；
        # 15s 周期导出（PeriodicExportingMetricReader），兼顾观察时效与开销。
        # [2026-09-09 开发环境临时禁用] metrics_endpoint 复用 phoenix_endpoint，
        # 本地 .env 指向 10.10.10.67:4317 → No route to host 同步刷屏，一并注释。
        # 恢复：取消本块注释即可（生产走 alloy:4317，与此无关）。
        # [2026-09-09] 原 `metrics_registered = False` 初始化已随禁用注释；
        # 恢复 metrics 块时需先补回此行（保持 was_setup 语义）。
        # metrics_registered = False
        # if settings.otel_metrics_enabled:
        #     metrics_endpoint = settings.otel_metrics_endpoint or phoenix_endpoint
        #     if metrics_endpoint:
        #         try:
        #             metric_reader = PeriodicExportingMetricReader(
        #                 OTLPMetricExporter(
        #                     endpoint=metrics_endpoint,
        #                     timeout=5,
        #                 ),
        #                 export_interval_millis=15000,
        #             )
        #             meter_provider = MeterProvider(
        #                 metric_readers=[metric_reader],
        #                 resource=resource,
        #             )
        #             metrics_api.set_meter_provider(meter_provider)
        #             metrics_registered = True
        #             logger.info(
        #                 "[METRICS] MeterProvider 已注册 — OTLP gRPC endpoint=%s, 导出周期=15s",
        #                 metrics_endpoint,
        #             )
        #         except Exception as e:
        #             logger.warning("[METRICS] MeterProvider 初始化异常: %s", e)
        #     else:
        #         logger.warning(
        #             "[METRICS] otel_metrics_enabled=True 但无 endpoint "
        #             "（phoenix_collector_endpoint 为空）— metrics 跳过"
        #         )

        # ── 4. 激活 ──
        trace_api.set_tracer_provider(tracer_provider)
        LangChainInstrumentor().instrument()

        # HTTP server instrument（http_server_* metrics + HTTP server span）。
        # 须在 FastAPI app 实例化前 instrument——setup_tracing() 位于 server.py 顶部、
        # import api.router 之前（模块 docstring 已声明），顺序安全。
        if FastAPIInstrumentor is not None:
            try:
                FastAPIInstrumentor().instrument()
                logger.info(
                    "[TRACING] FastAPIInstrumentor 已注册（http_server_* metrics + HTTP span）"
                )
            except Exception as e:
                logger.warning("[TRACING] FastAPIInstrumentor 注册失败: %s", e)

        # [2026-09-09] exporter 全部注释（开发环境禁用）→ was_setup 恒 False，
        # 供 server.py 语义判断（当前未使用返回值，仅语义正确）。
        was_setup = False
        _initialized = True
        logger.info(
            "[TRACING] OpenInference 初始化完成 — "
            "auto_instrument=langchain, "
            "exporter=DISABLED(dev 2026-09-09, Phoenix/Langfuse/metrics 已注释)"
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
