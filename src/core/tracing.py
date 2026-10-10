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
from typing import Any

logger = logging.getLogger(__name__)

_initialized = False

#: setup_tracing() 建好的 TracerProvider —— 供 shutdown_tracing() 关停前 flush。
#: 为什么需要留引用：BatchSpanProcessor 把未导出的 span 放在自己的队列里，
#: 进程退出时若不 flush 就整段消失（Simple 时代逐条即时导出，没这个问题）。
_tracer_provider: Any = None
_shutdown_done = False

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

try:
    from opentelemetry.sdk.trace.export import SpanExporter as _SpanExporterBase
except ImportError:  # pragma: no cover — 同上
    _SpanExporterBase = object  # type: ignore[assignment,misc]


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


# ═══════════════════════════════════════════════════════════════════════════════
# span 体积防护（2026-10-10 事故后新增）
# ═══════════════════════════════════════════════════════════════════════════════
# 事故现场（会话 4863afff，14:31:55）：
#   RESOURCE_EXHAUSTED: grpc: received message larger than max (28027858 vs 4194304)
# 三条实测事实决定了这里为什么需要**多层**，而不是"换成 Batch"一行了事：
#
#   ① 4 MiB 是 alloy（grpc-go **服务端**）的 MaxRecvMsgSize，不是应用侧可调参数；
#      超限的请求被整体拒绝 → 该次导出携带的 span **全部**丢失。
#   ② Phoenix 当时用 SimpleSpanProcessor（同步导出）→ 失败的导出重试把承载请求的
#      worker 按住约 8 秒（14:31:55→14:32:03），正压在工具结果后处理那一刻。
#   ③ OTel SDK 的 SpanLimits **默认不限制属性值长度**
#      （max_span_attribute_length / max_attribute_length 缺省 None，见
#      opentelemetry/sdk/trace/__init__.py:642-645），所以 28 MiB 的 input.value
#      能一路走到 exporter。
#
# 层与层的分工（缺任何一层都有明确的失效路径）：
#   L1 SpanLimits（SDK 标准，零自定义代码）—— 在 set_attribute 源头截断单个属性值。
#      收益：绝大多数 span 根本长不到危险体量。局限：它只约束「单值长度 × 属性数 ×
#      事件数」的乘积上界 —— 128 属性 × 32768 字符 × 3 字节/中文 ≈ 12 MiB，仍然远超
#      4 MiB。**乘积上界不等于硬保证**，所以还需要 L2。
#   L2 _SpanPayloadGuardProcessor —— 量**真实**估算体积，超限整条丢弃并打 error。
#      这是"硬保证"的落点：与 execute 输出闸门同一条结论 —— 只守上界不算守，
#      必须量真东西；只做一层防御必然炸。
#   L3 _ChunkedSpanExporter —— 约束**一次网络请求**的体积。换 BatchSpanProcessor
#      会引入一个 Simple 时代不存在的新风险：默认 max_export_batch_size=512，
#      而 `BatchProcessor._export` 对导出失败**不重试**（span 已从队列弹出，见
#      opentelemetry/sdk/_shared_internal/__init__.py:167）→ 一次超限 = 512 条一起消失。
#      分片把单次损失上限压到一片。
#   L4 shutdown_tracing —— 关停前 flush。Simple 逐条即时导出不留尾巴，换 Batch 后
#      最后一段留在内存里，进程退出即丢（此前全仓无人调用 flush，Langfuse 那路
#      本来就是 Batch，一直在丢尾部）。
# ═══════════════════════════════════════════════════════════════════════════════

#: 估算系数：protobuf 的 tag/长度前缀 + gRPC 帧开销。刻意取偏大值 ——
#: 估算偏大只会多丢一条边缘 span，估算偏小则会让整批被服务端拒绝。
_ENCODING_OVERHEAD_FACTOR = 2.0
#: span 名称 + 元数据的固定开销（估算用，不求精确）
_SPAN_META_OVERHEAD_BYTES = 64
#: 单个 event 的固定开销（时间戳、名称等）
_EVENT_META_OVERHEAD_BYTES = 64


def _value_bytes(value: Any) -> int:
    """属性值编码后的近似字节数（**按 UTF-8 计**，中文 1 字符按 3 字节）。"""
    if isinstance(value, str):
        return len(value.encode("utf-8", "ignore"))
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, bool):
        return 1
    if isinstance(value, (int, float)):
        return 8
    if isinstance(value, (list, tuple)):
        return sum(_value_bytes(item) for item in value)
    return 0


def _attributes_bytes(attributes: Any) -> int:
    """一组属性的近似字节数（键 + 值）。容器异常时抛出，由调用方按 fail-closed 处理。"""
    if not attributes:
        return 0
    total = 0
    for key, value in attributes.items():
        total += len(str(key).encode("utf-8", "ignore")) + _value_bytes(value)
    return total


def _estimate_span_bytes(span: Any) -> int:
    """估算一条 span 编码为 OTLP protobuf 后的字节数。

    只统计**主导项**：名称、属性、事件、状态描述。resource 属性不计 —— 它按
    Resource 全局共享、不随 span 增长（口径说明，便于日后核对）。

    刻意不追求精确：闸门只做量级判定。中文按 UTF-8 3 字节计这一点很关键 ——
    SDK 的 SpanLimits 用的是**字符数**，若这里也按字符估，会低估到 1/3。
    """
    name = getattr(span, "name", "") or ""
    total = len(str(name).encode("utf-8", "ignore")) + _SPAN_META_OVERHEAD_BYTES
    total += _attributes_bytes(getattr(span, "attributes", None))
    for event in getattr(span, "events", None) or ():
        total += _EVENT_META_OVERHEAD_BYTES + _attributes_bytes(
            getattr(event, "attributes", None)
        )
    status = getattr(span, "status", None)
    description = getattr(status, "description", None)
    if description:
        total += len(str(description).encode("utf-8", "ignore"))
    return int(total * _ENCODING_OVERHEAD_FACTOR)


class _SpanPayloadGuardProcessor(_SpanProcessorBase):  # type: ignore[misc]
    """单条 span 体积闸门：超限即丢弃，绝不放过（fail-closed）。

    挂在 `_OpenInferenceOnlySpanProcessor` **内层** —— 先按 span kind 过滤掉 98% 的
    HTTP/ASGI span（历史实测 Phoenix spans 表 106,368/108,209 是 HTTP 形态），
    只对本就要上报的 span 做测量，不做无用功。

    为什么是"丢弃"而不是"就地截断"：on_end 拿到的 ReadableSpan 已结束，官方文档
    明确 *"Users should NOT be creating these objects directly"*
    （opentelemetry/sdk/trace/__init__.py:410），改写属性不是受支持的用法；
    而源头截断已由 L1 SpanLimits 负责。本层只回答一个问题：
    **"这条 span 会不会把整批拖下水？会 → 现在就丢掉它，并留下证据。"**

    估算本身失败时同样丢弃（fail-closed）：估不出体积 = 未知体量，
    放行它等于把 4 MiB 上限交给运气。
    """

    _dropped = 0
    _passed = 0

    def __init__(self, downstream: Any, *, limit_bytes: int) -> None:
        self._downstream = downstream
        self._limit_bytes = limit_bytes

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        try:
            self._downstream.on_start(span, parent_context)
        except Exception:  # 观测链路异常不得影响业务
            logger.debug("[TRACING] on_start 转发下游异常", exc_info=True)

    def on_end(self, span: Any) -> None:
        try:
            size = _estimate_span_bytes(span)
        except Exception:
            _SpanPayloadGuardProcessor._dropped += 1
            logger.error(
                "[TRACING] span 体积估算失败，已丢弃（fail-closed）：name=%r —— "
                "估不出体积就不能放行，否则等于把 4 MiB 上限交给运气",
                getattr(span, "name", "?"),
                exc_info=True,
            )
            return

        if self._limit_bytes > 0 and size > self._limit_bytes:
            _SpanPayloadGuardProcessor._dropped += 1
            logger.error(
                "[TRACING] 丢弃超限 span：name=%r 估算=%d 字节 > 上限=%d 字节。"
                "该 span 若导出会撞 alloy 4 MiB 接收上限（RESOURCE_EXHAUSTED），"
                "且 BatchSpanProcessor 失败**不重试**（span 已出队）→ 会让同批其余 span "
                "一起丢失。请检查该 span 的属性来源（多为工具结果/对话内容被整段"
                "记进 input.value / output.value）。",
                getattr(span, "name", "?"),
                size,
                self._limit_bytes,
            )
            return

        _SpanPayloadGuardProcessor._passed += 1
        try:
            self._downstream.on_end(span)
        except Exception:
            logger.debug("[TRACING] on_end 转发下游异常", exc_info=True)

    def shutdown(self) -> None:
        logger.info(
            "[TRACING] span 体积闸门统计 — 放行=%d 丢弃=%d（单条上限 %d 字节）",
            _SpanPayloadGuardProcessor._passed,
            _SpanPayloadGuardProcessor._dropped,
            self._limit_bytes,
        )
        try:
            self._downstream.shutdown()
        except Exception:
            logger.debug("[TRACING] 下游 shutdown 异常", exc_info=True)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._downstream.force_flush(timeout_millis)


def _pack_span_chunks(spans: Any, limit_bytes: int) -> list[list[Any]]:
    """按估算体积把 span 序列贪心装箱，保证每片 ≤ ``limit_bytes``。

    单条自身就超限的 span 会独占一片 —— 本函数**不再兜第二层**：那种 span 已由
    `_SpanPayloadGuardProcessor` 在上游丢弃，两层做同一件事只会互相掩盖问题。
    """
    chunks: list[list[Any]] = []
    current: list[Any] = []
    current_bytes = 0
    for span in spans:
        try:
            size = _estimate_span_bytes(span)
        except Exception:
            logger.error(
                "[TRACING] 分片时体积估算失败，该 span 独占一片", exc_info=True
            )
            size = limit_bytes if limit_bytes > 0 else 0
        if current and limit_bytes > 0 and current_bytes + size > limit_bytes:
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(span)
        current_bytes += size
    if current:
        chunks.append(current)
    return chunks


class _ChunkedSpanExporter(_SpanExporterBase):  # type: ignore[misc]
    """把一次导出拆成多次请求，避免"整批一起超限 → 整批一起消失"。

    这是**换 BatchSpanProcessor 引入的新风险**的对应措施，不是锦上添花：
    SimpleSpanProcessor 每次只导出 1 条 span，永远撞不到"批量体积"问题；Batch 默认
    ``max_export_batch_size=512``，若每条约 100 KiB，单次请求可达 50 MiB。而
    ``BatchProcessor._export`` 对失败**不重试**（只记日志，span 已从队列弹出），
    因此一次超限就是 512 条静默消失。

    返回值语义：全成功 → SUCCESS；任一片失败 → FAILURE。
    当前 SDK 不重试，故不会重复上报；即便上游将来加重试也安全 ——
    Phoenix / Langfuse 均按 span_id 落库，重复上报幂等。
    """

    def __init__(self, downstream: Any, *, limit_bytes: int) -> None:
        self._downstream = downstream
        self._limit_bytes = limit_bytes
        self._split_count = 0

    @property
    def split_count(self) -> int:
        """因超限而被拆分的次数（便于测试与排障）。"""
        return self._split_count

    def export(self, spans: Any) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        if self._limit_bytes <= 0:
            return self._downstream.export(spans)

        spans = tuple(spans)
        chunks = _pack_span_chunks(spans, self._limit_bytes)
        if len(chunks) > 1:
            self._split_count += 1
            logger.info(
                "[TRACING] 导出批次 %d 条估算超上限，已拆为 %d 次请求（上限 %d 字节/次）",
                len(spans),
                len(chunks),
                self._limit_bytes,
            )

        result = SpanExportResult.SUCCESS
        for chunk in chunks:
            try:
                if self._downstream.export(chunk) is not SpanExportResult.SUCCESS:
                    result = SpanExportResult.FAILURE
            except Exception:
                logger.exception("[TRACING] 分片导出异常（该片丢失，其余照常）")
                result = SpanExportResult.FAILURE
        return result

    def shutdown(self) -> None:
        self._downstream.shutdown()

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

        # ── L1：span 属性值长度上限（SDK 标准 SpanLimits，零自定义代码）──
        # 为什么必须显式设：SDK 的 max_span_attribute_length / max_attribute_length
        # **默认 None = 不截断**（opentelemetry/sdk/trace/__init__.py:642-645），
        # 这正是 28 MiB 的 input.value 能一路走到 alloy 的原因。
        # 语义：SDK 按**字符**截断（BoundedAttributes._clean_attribute 做 value[:max_len]），
        # 中文 1 字符 ≈ 3 字节，故注释与配置项都按字符表述。
        # 计数类上限（属性数/事件数）保持 SDK 默认 128 —— 276 线业务 span 远未触顶，
        # 收紧它们只会丢合法信息，真正的硬保证交给 L2 量体积。
        span_limits = None
        if settings.otel_span_limits_enabled:
            span_limits = trace_sdk.SpanLimits(
                max_span_attribute_length=settings.otel_span_attribute_value_limit,
                max_attribute_length=settings.otel_event_attribute_value_limit,
            )
        else:
            logger.warning(
                "[TRACING] otel_span_limits_enabled=False — 属性值长度不设上限，"
                "已退回 2026-10-10 之前的行为（大 span 仍可能撞 alloy 4 MiB 上限）"
            )

        tracer_provider = trace_sdk.TracerProvider(
            resource=resource, span_limits=span_limits
        )

        def _build_processor(exporter: Any) -> Any:
            """统一装配链条：过滤(最外) → 体积闸门 → Batch → 分片 → exporter(最内)。

            顺序不是随意排的：
            · 过滤放最外 —— 先丢掉 98% 的 HTTP/ASGI span，后面的测量与分片不做无用功；
            · 闸门在 Batch 之外 —— 被丢弃的 span 根本不进队列，不占队列也不参与分片；
            · 分片紧贴 exporter —— 它约束的是**一次网络请求**的体积，必须最靠内，
              才能看到 Batch 真正要发出去的那一批。
            """
            chunked = _ChunkedSpanExporter(
                exporter, limit_bytes=settings.otel_export_request_bytes_limit
            )
            guarded = _SpanPayloadGuardProcessor(
                BatchSpanProcessor(chunked),
                limit_bytes=settings.otel_span_payload_limit_bytes,
            )
            return _OpenInferenceOnlySpanProcessor(guarded)

        # ── 1. Phoenix gRPC exporter ──
        phoenix_endpoint = settings.phoenix_collector_endpoint
        if phoenix_endpoint:
            os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = phoenix_endpoint
            tracer_provider.add_span_processor(
                _build_processor(GrpcExporter(endpoint=phoenix_endpoint))
            )
            logger.info(
                "[TRACING] Phoenix gRPC exporter 已添加 — endpoint=%s"
                "（OpenInference 过滤 + 体积闸门 + 分片；Batch 导出）",
                phoenix_endpoint,
            )
        else:
            logger.warning(
                "[TRACING] phoenix_collector_endpoint 为空 — Phoenix exporter 跳过"
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
                _build_processor(HttpExporter(endpoint=langfuse_endpoint, headers=headers))
            )
            logger.info(
                "[TRACING] Langfuse HTTP exporter 已添加 — endpoint=%s"
                "（OpenInference 过滤 + 体积闸门 + 分片；Batch 导出）",
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
        # 留引用供 shutdown_tracing() 关停前 flush（Batch 的尾巴在这里面）
        global _tracer_provider
        _tracer_provider = tracer_provider
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
            "Phoenix=%s, Langfuse=%s, Metrics=%s, span_filter=OpenInferenceOnly, "
            "span_limits=%s（属性值上限 %d 字符 / 事件 %d 字符）, "
            "单条 span 上限 %d 字节, 单次导出上限 %d 字节",
            bool(phoenix_endpoint),
            bool(settings.langfuse_secret_key and settings.langfuse_base_url),
            metrics_registered,
            "on" if span_limits is not None else "off",
            settings.otel_span_attribute_value_limit,
            settings.otel_event_attribute_value_limit,
            settings.otel_span_payload_limit_bytes,
            settings.otel_export_request_bytes_limit,
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


def shutdown_tracing(timeout_millis: int = 3000) -> bool:
    """关停追踪：flush 掉 Batch 里还没导出的 span，然后逐层 shutdown。

    ★ 为什么必须有（换 BatchSpanProcessor 引入的**新**风险）：
      SimpleSpanProcessor 是逐条即时导出，进程退出不留尾巴；换成 Batch 后，最后
      一段时间内的 span 留在内存队列里（默认 schedule_delay 5s + 未满批的部分），
      进程退出即消失。此前**全仓没有任何地方**调用 force_flush/shutdown
      （grep 实测），也就是说 Langfuse 那一路（本来就是 Batch）一直在丢尾部 trace，
      只是没人统计过丢了多少。

    ★ 为什么默认超时是 3s 而不是 SDK 惯用的 30s：
      `deploy/docker-compose.yml` **没有**设置 `stop_grace_period`，即 `docker stop`
      走默认 10s 宽限期，超时直接 SIGKILL。若这里给 10s（甚至 30s），flush 会与
      SIGKILL 抢时间，而且会把宽限期吃光、让其余关停工作（连接池 aclose）没时间做。
      本地 alloy 在同一个 compose 网络里，正常 flush 是毫秒级，3s 是充裕的上限。
      若确实需要更长，请同时给 app 服务加 `stop_grace_period`。

    调用点：src/server.py 的 lifespan（yield 之后，放在 finally 里）。用 to_thread
    包住，因为 force_flush 会阻塞最多 ``timeout_millis``。

    :return: 是否 flush 成功（未初始化时返回 True —— 没启用不算失败）
    """
    global _shutdown_done
    if _shutdown_done:
        return True
    if _tracer_provider is None:
        logger.debug("[TRACING] 未初始化，shutdown_tracing 跳过")
        return True

    ok = True
    try:
        flushed = _tracer_provider.force_flush(timeout_millis)
        if flushed:
            logger.info(
                "[TRACING] 关停前 flush 完成（超时上限 %d ms）", timeout_millis
            )
        else:
            ok = False
            logger.warning(
                "[TRACING] 关停前 flush 超时（%d ms）— 队列里剩余的 span 会丢失",
                timeout_millis,
            )
    except Exception:
        ok = False
        logger.error("[TRACING] 关停前 flush 异常", exc_info=True)

    try:
        # 逐层转发：过滤 → 闸门 → Batch → 分片 → exporter，
        # 顺带打出「过滤统计」「体积闸门统计」两条 info，便于确认防护是否生效。
        _tracer_provider.shutdown()
    except Exception:
        logger.error("[TRACING] provider.shutdown 异常", exc_info=True)

    _shutdown_done = True
    return ok
