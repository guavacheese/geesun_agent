"""OpenInference 追踪初始化模块（双 exporter：Phoenix + Langfuse）。

必须在任何 LangChain / LangGraph / deepagents 被 import 之前调用 setup_tracing()。
这是因为 OpenInference 的 auto-instrumentation 需要在模块加载时 hook 进去。

使用方法（在 server.py 顶部）：

    from src.core.logging import *         # 日志最先就绪
    from src.core.tracing import setup_tracing
    setup_tracing()                         # ← 此时还没有 import LangChain
    from .api.router import api_router      # ← 安全了
"""

import os
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
        from openinference.instrumentation.langchain import LangChainInstrumentor

        tracer_provider = trace_sdk.TracerProvider(
            resource=Resource.create({
                "openinference.project.name": settings.otel_project_name,
            })
        )

        # ── 1. Phoenix gRPC exporter ──
        phoenix_endpoint = settings.phoenix_collector_endpoint
        if phoenix_endpoint:
            os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = phoenix_endpoint
            tracer_provider.add_span_processor(
                SimpleSpanProcessor(
                    GrpcExporter(endpoint=phoenix_endpoint)
                )
            )
            logger.info(
                "[TRACING] Phoenix gRPC exporter 已添加 — endpoint=%s",
                phoenix_endpoint,
            )

        # ── 2. Langfuse HTTP exporter ──
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
                BatchSpanProcessor(
                    HttpExporter(endpoint=langfuse_endpoint, headers=headers)
                )
            )
            logger.info(
                "[TRACING] Langfuse HTTP exporter 已添加 — endpoint=%s",
                langfuse_endpoint,
            )
        else:
            logger.warning(
                "[TRACING] LANGFUSE 配置不完整 — Langfuse exporter 跳过"
            )

        # ── 3. 激活 ──
        trace_api.set_tracer_provider(tracer_provider)
        LangChainInstrumentor().instrument()

        was_setup = bool(
            phoenix_endpoint
            or (settings.langfuse_secret_key and settings.langfuse_base_url)
        )
        _initialized = True
        logger.info(
            "[TRACING] OpenInference 初始化完成 — "
            "auto_instrument=langchain, "
            "Phoenix=%s, Langfuse=%s",
            bool(phoenix_endpoint),
            bool(settings.langfuse_secret_key and settings.langfuse_base_url),
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
