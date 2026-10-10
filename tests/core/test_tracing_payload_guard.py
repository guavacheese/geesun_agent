"""span 体积防线（2026-10-10 事故后新增）—— 行为 + 接线双重契约。

事故（会话 4863afff，14:31:55）：单条 span 达 **28,027,858 字节**，撞 alloy 的
gRPC 接收上限 4 MiB：

    RESOURCE_EXHAUSTED: grpc: received message larger than max (28027858 vs 4194304)

而 Phoenix 当时走 `SimpleSpanProcessor`（**同步**导出）→ 失败重试把承载请求的
worker 按住约 8 秒（14:31:55→14:32:03），正压在工具结果后处理那一刻。

本文件守的是四层防线，其中每一层都对应一条**独立的**失效路径（不是同一件事的重复）：

    L1 SpanLimits           源头截断属性值（SDK 标准 API）
    L2 _SpanPayloadGuard    单条超限即丢弃（量真实体积，fail-closed）
    L3 _ChunkedSpanExporter 导出请求按体积分片（防"整批一起消失"）
    L4 shutdown_tracing     关停前 flush（换 Batch 引入的新风险）

为什么 L1 不能取代 L2：`SpanLimits` 约束的是「单值长度 × 属性数 × 事件数」的**乘积
上界** —— 128 属性 × 32768 字符 × 3 字节/中文 ≈ 12 MiB，仍然超过 4 MiB。
乘积上界 ≠ 硬保证；硬保证只能来自"量真东西"。
"""

from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import (  # noqa: E402
    SpanLimits,
    SpanProcessor,
    TracerProvider,
)
from opentelemetry.sdk.trace.export import (  # noqa: E402
    SpanExporter,
    SpanExportResult,
)

from src.core.config import settings  # noqa: E402
from src.core.tracing import (  # noqa: E402
    _ChunkedSpanExporter,
    _ENCODING_OVERHEAD_FACTOR,
    _SpanPayloadGuardProcessor,
    _estimate_span_bytes,
    _pack_span_chunks,
    _value_bytes,
)

TRACING_PY = PROJECT_ROOT / "src" / "core" / "tracing.py"
SERVER_PY = PROJECT_ROOT / "src" / "server.py"

#: alloy（grpc-go 服务端）的 MaxRecvMsgSize —— 硬上限，不是应用侧可调参数
ALLOY_MAX_RECV_BYTES = 4 * 1024 * 1024


# ─── 测试替身 ────────────────────────────────────────────────────────────────


class _CaptureProcessor(SpanProcessor):
    """下游捕获器：记录被放行到这一层的 span。"""

    def __init__(self) -> None:
        self.spans: list = []
        self.started = 0
        self.shutdown_called = False
        self.flush_calls = 0

    def on_start(self, span, parent_context=None) -> None:  # noqa: D102
        self.started += 1

    def on_end(self, span) -> None:  # noqa: D102
        self.spans.append(span)

    def shutdown(self) -> None:  # noqa: D102
        self.shutdown_called = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: D102
        self.flush_calls += 1
        return True


class _RecordingExporter(SpanExporter):
    """记录每次 export 调用收到的批次；可指定某几片失败。"""

    def __init__(self, fail_indices: frozenset[int] = frozenset()) -> None:
        self.calls: list[tuple] = []
        self._fail_indices = fail_indices

    def export(self, spans):  # noqa: D102
        index = len(self.calls)
        self.calls.append(tuple(spans))
        return (
            SpanExportResult.FAILURE
            if index in self._fail_indices
            else SpanExportResult.SUCCESS
        )

    def shutdown(self) -> None:  # noqa: D102
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: D102
        return True


class _StubSpan:
    """鸭子类型的 span（只提供估算用得到的字段），用于纯函数级用例。"""

    def __init__(self, *, name="stub", attributes=None, events=(), status=None) -> None:
        self.name = name
        self.attributes = {} if attributes is None else attributes
        self.events = tuple(events)
        self.status = status


class _StubEvent:
    def __init__(self, attributes) -> None:
        self.attributes = attributes


def _stub_with_attr_bytes(target_bytes: int) -> _StubSpan:
    """构造一条属性载荷约为 ``target_bytes`` 字节的桩 span。

    注意 `_value_bytes` 按 UTF-8 计，"x" 是单字节字符，故字符数 == 字节数。
    """
    return _StubSpan(attributes={"k": "x" * target_bytes})


def _provider(processor=None, *, span_limits=None):
    """建一个真实 TracerProvider；processor 为 None 时用捕获器。"""
    provider = TracerProvider(
        resource=Resource.create({"service.name": "test-svc"}),
        span_limits=span_limits,
    )
    capture = _CaptureProcessor()
    provider.add_span_processor(capture if processor is None else processor)
    return provider, capture


def _emit(provider, *, name="span", attributes=None, events=()) -> None:
    """走真实 SDK 建一条 span 并结束它（结束即触发 on_end → 被测处理器）。"""
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        for ev_name, ev_attributes in events:
            span.add_event(ev_name, ev_attributes)


@pytest.fixture(autouse=True)
def _reset_guard_counters():
    """闸门计数是类级的（两个 exporter 共享统计），逐例复位避免互相污染。"""
    _SpanPayloadGuardProcessor._passed = 0
    _SpanPayloadGuardProcessor._dropped = 0
    yield


# ─── L2 前置：体积估算必须"既保守又不离谱" ───────────────────────────────────


def test_value_bytes_counts_utf8_bytes_not_chars():
    """中文必须按 3 字节算 —— SDK 的 SpanLimits 用字符数，若这里也用字符会低估到 1/3。"""
    assert _value_bytes("中" * 100) == 300
    assert _value_bytes("ab") == 2
    assert _value_bytes(b"12345") == 5
    assert _value_bytes(["a", "中"]) == 4
    assert _value_bytes(3.14) > 0


def test_estimate_is_conservative_and_not_absurdly_inflated():
    """估算 ≥ 真实载荷字节数（否则闸门会漏），且 ≤ 4 倍（否则误丢合法 span）。

    这条**不**拿估算函数去推期望值（那会自证），而是与我真正塞进 span 的载荷对比。
    """
    payload = "中" * 1000  # = 3000 字节
    provider, capture = _provider()
    _emit(provider, attributes={"k": payload})

    estimate = _estimate_span_bytes(capture.spans[0])
    raw = len(payload.encode("utf-8")) + len(b"k")

    assert estimate >= raw, "估算小于载荷本身的字节数 —— 闸门会漏过超限 span"
    assert estimate <= 4 * raw, f"估算 {estimate} 相对载荷 {raw} 过大，会误丢合法 span"


def test_estimate_covers_real_protobuf_size():
    """最强校验：估算必须 ≥ 真实 protobuf 序列化体积。

    需要 otlp exporter 包（生产镜像内有）；当前开发机解释器缺该依赖时跳过，
    不把"环境缺包"伪装成"逻辑正确"。
    """
    encoder = pytest.importorskip(
        "opentelemetry.exporter.otlp.proto.common._internal.trace_encoder"
    )
    provider, capture = _provider()
    _emit(
        provider,
        attributes={"k": "中" * 500},
        events=[("ev", {"detail": "y" * 500})],
    )

    real = len(encoder.encode_spans(capture.spans).SerializeToString())
    estimate = _estimate_span_bytes(capture.spans[0])

    assert estimate >= real, (
        f"估算 {estimate} < 真实编码体积 {real} —— 闸门会放行本来会被服务端拒绝的 span"
    )


def test_estimate_includes_events_and_status_description():
    """事件与状态描述也是体积来源，漏算会让闸门低估。"""
    base = _StubSpan(attributes={"k": "x" * 100})
    with_event = _StubSpan(
        attributes={"k": "x" * 100},
        events=[_StubEvent({"log": "y" * 1000})],
    )
    assert _estimate_span_bytes(with_event) > _estimate_span_bytes(base)

    class _Status:
        description = "z" * 2000

    with_status = _StubSpan(attributes={"k": "x" * 100}, status=_Status())
    assert _estimate_span_bytes(with_status) > _estimate_span_bytes(base)


def test_encoding_overhead_factor_is_at_least_one():
    """系数 < 1 等于在估算里"偷偷缩小"体积，会让闸门失灵。"""
    assert _ENCODING_OVERHEAD_FACTOR >= 1.0


# ─── L1：SpanLimits 确实在源头截断（含"默认不截断"这个根因前提）─────────────


def test_default_span_limits_do_not_truncate_attribute_value():
    """根因前提验证：SDK 默认**不限**属性值长度。

    这条不是在测我们自己的代码，而是在钉死"为什么需要显式设 SpanLimits" ——
    28 MiB 的 input.value 能一路走到 alloy，就是因为默认值是 None。
    若哪天 SDK 改了默认行为，这里会红，提醒我们重新评估 L1 是否还必要。
    """
    provider, capture = _provider()  # span_limits=None → SDK 默认
    _emit(provider, attributes={"k": "x" * 100_000})

    assert len(capture.spans[0].attributes["k"]) == 100_000


def test_span_limits_truncate_span_attribute_value():
    """L1 生效：单个 span 属性值被截断到配置长度。"""
    provider, capture = _provider(span_limits=SpanLimits(max_span_attribute_length=100))
    _emit(provider, attributes={"k": "x" * 100_000})

    assert len(capture.spans[0].attributes["k"]) == 100


def test_span_limits_truncate_event_attribute_value():
    """L1 覆盖**事件属性**（日志类 span 的体积主要来自 events）——

    这条断言来自源码阅读（SDK 把 max_attribute_length 传给 event 的
    BoundedAttributes），此处用真实 SDK 复验，避免把"读代码以为"当结论。
    """
    provider, capture = _provider(span_limits=SpanLimits(max_attribute_length=50))
    _emit(provider, events=[("ev", {"k": "y" * 1000})])

    event = capture.spans[0].events[0]
    assert len(event.attributes["k"]) == 50


# ─── L2：单条 span 体积闸门 ──────────────────────────────────────────────────


def test_guard_drops_oversized_span(caplog):
    """超限 span 必须被拦下，且**留下 error 级证据**（不许静默）。"""
    capture = _CaptureProcessor()
    guard = _SpanPayloadGuardProcessor(capture, limit_bytes=20_000)
    provider, _ = _provider(processor=guard)

    with caplog.at_level(logging.ERROR, logger="src.core.tracing"):
        _emit(provider, name="huge", attributes={"k": "x" * 100_000})

    assert capture.spans == [], "超限 span 被放行 —— 4 MiB 上限会被撞穿"
    assert _SpanPayloadGuardProcessor._dropped == 1
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("丢弃超限 span" in m and "huge" in m for m in messages), (
        "丢弃没有留下可检索的证据：" + repr(messages)
    )


def test_guard_passes_span_under_limit():
    """未超限的 span 必须原样通过，且被计数为放行。"""
    capture = _CaptureProcessor()
    guard = _SpanPayloadGuardProcessor(capture, limit_bytes=1_000_000)
    provider, _ = _provider(processor=guard)

    _emit(provider, name="normal", attributes={"k": "x" * 1000})

    assert len(capture.spans) == 1 and capture.spans[0].name == "normal"
    assert _SpanPayloadGuardProcessor._passed == 1
    assert _SpanPayloadGuardProcessor._dropped == 0


def test_guard_limit_non_positive_disables_dropping():
    """limit ≤ 0 = 关闭闸门（留一个可回退的开关，便于排障时对照）。"""
    capture = _CaptureProcessor()
    guard = _SpanPayloadGuardProcessor(capture, limit_bytes=0)
    provider, _ = _provider(processor=guard)

    _emit(provider, attributes={"k": "x" * 100_000})

    assert len(capture.spans) == 1


def test_guard_estimation_failure_is_fail_closed(caplog):
    """估不出体积 = 未知体量 → 必须丢弃。

    若这里放行，等于把 4 MiB 上限交给运气 —— 而"估算代码本身出错"恰恰是最需要
    保守的时刻（例如属性容器类型变化）。
    """

    class _ExplodingAttributes(dict):
        def items(self):  # noqa: D102
            raise RuntimeError("属性容器异常")

    capture = _CaptureProcessor()
    guard = _SpanPayloadGuardProcessor(capture, limit_bytes=1_000_000)
    stub = _StubSpan(name="bad", attributes=_ExplodingAttributes({"k": "v"}))

    with caplog.at_level(logging.ERROR, logger="src.core.tracing"):
        guard.on_end(stub)

    assert capture.spans == []
    assert _SpanPayloadGuardProcessor._dropped == 1
    assert any(
        "体积估算失败" in r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.ERROR
    )


def test_guard_forwards_lifecycle_to_downstream():
    """shutdown / force_flush 必须逐层转发，否则 Batch 的 flush 会断在这层。"""
    capture = _CaptureProcessor()
    guard = _SpanPayloadGuardProcessor(capture, limit_bytes=1000)

    assert guard.force_flush(1234) is True
    assert capture.flush_calls == 1
    guard.shutdown()
    assert capture.shutdown_called is True


# ─── L3：导出请求按体积分片 ──────────────────────────────────────────────────


def test_pack_chunks_never_exceeds_limit_and_preserves_order():
    spans = [_stub_with_attr_bytes(n) for n in (200, 400, 600, 800, 1000)]
    sizes = [_estimate_span_bytes(s) for s in spans]
    limit = max(sizes) + sizes[1]  # 恰好够装一条最大 + 一条次小

    chunks = _pack_span_chunks(spans, limit)

    assert len(chunks) > 1, "总量明显超限却没有分片 —— 用例本身失效"
    for chunk in chunks:
        assert sum(_estimate_span_bytes(s) for s in chunk) <= limit
    assert [s for chunk in chunks for s in chunk] == spans, "分片改变了顺序或丢了 span"


def test_pack_single_oversized_span_gets_own_chunk():
    """单条自身超限的 span 独占一片（它已由 L2 在上游丢弃，这里不重复兜第二层）。"""
    spans = [
        _stub_with_attr_bytes(10),
        _stub_with_attr_bytes(100_000),
        _stub_with_attr_bytes(10),
    ]
    chunks = _pack_span_chunks(spans, 1000)

    assert [len(c) for c in chunks] == [1, 1, 1]


def test_chunked_exporter_splits_and_delivers_each_span_exactly_once():
    spans = [_stub_with_attr_bytes(n) for n in (500, 500, 500, 500, 500, 500)]
    limit = sum(_estimate_span_bytes(s) for s in spans[:3])  # 每片最多 3 条
    downstream = _RecordingExporter()
    exporter = _ChunkedSpanExporter(downstream, limit_bytes=limit)

    result = exporter.export(spans)

    assert result is SpanExportResult.SUCCESS
    assert exporter.split_count == 1
    assert len(downstream.calls) == 2, f"期望拆成 2 次请求，实际 {len(downstream.calls)}"
    for call in downstream.calls:
        assert sum(_estimate_span_bytes(s) for s in call) <= limit
    assert [s for call in downstream.calls for s in call] == spans


def test_chunked_exporter_bounds_loss_when_one_chunk_fails():
    """某片失败时：结果 FAILURE，但**其余片仍被送出**（损失上限 = 一片）。

    这是分片的核心收益 —— 不分片时一次超限就是整批 512 条一起消失。
    """
    spans = [_stub_with_attr_bytes(n) for n in (500, 500, 500, 500)]
    limit = sum(_estimate_span_bytes(s) for s in spans[:2])
    downstream = _RecordingExporter(fail_indices=frozenset({0}))
    exporter = _ChunkedSpanExporter(downstream, limit_bytes=limit)

    result = exporter.export(spans)

    assert result is SpanExportResult.FAILURE
    assert len(downstream.calls) == 2, "首片失败后其余片未被送出 —— 损失未被限制在一片"


def test_chunked_exporter_passthrough_when_disabled():
    """limit ≤ 0 = 关闭分片，原样透传（可回退开关）。"""
    spans = [_stub_with_attr_bytes(100) for _ in range(5)]
    downstream = _RecordingExporter()
    exporter = _ChunkedSpanExporter(downstream, limit_bytes=0)

    assert exporter.export(spans) is SpanExportResult.SUCCESS
    assert len(downstream.calls) == 1
    assert exporter.split_count == 0


def test_chunked_exporter_forwards_lifecycle():
    downstream = _RecordingExporter()
    exporter = _ChunkedSpanExporter(downstream, limit_bytes=1000)
    assert exporter.force_flush(500) is True
    exporter.shutdown()


# ─── 接线契约：配置必须真的被用上（防"写完没接"与"改回去"）─────────────────────


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _callee(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee(node) == name
    ]


def _func_def(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _nesting_chain(node: ast.AST, var_map: dict[str, ast.AST]) -> list[str]:
    """从最外层调用向内层逐层收集被调用名。

    沿"第一个 Call 实参"下钻；遇到局部变量名时用 ``var_map`` 解析回它的赋值表达式
    —— 装配链在源码里是分成几行用局部变量拼起来的（``chunked`` / ``guarded``），
    检测器必须跟着数据流走，否则只能看到最外层那一个名字、断言形同虚设。
    """
    chain: list[str] = []
    seen: set[str] = set()
    while node is not None:
        if isinstance(node, ast.Name):
            if node.id in seen:  # 防自引用成环
                break
            seen.add(node.id)
            node = var_map.get(node.id)
            continue
        if isinstance(node, ast.Call):
            name = _callee(node)
            if name:
                chain.append(name)
            node = next(
                (a for a in node.args if isinstance(a, (ast.Call, ast.Name))), None
            )
            continue
        break
    return chain


def _local_call_map(fn: ast.AST) -> dict[str, ast.AST]:
    """函数体内的 ``局部变量 = 表达式`` 映射，供 _nesting_chain 解引用。"""
    mapping: dict[str, ast.AST] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    mapping[target.id] = node.value
    return mapping


def _references_name(node: ast.AST, name: str) -> bool:
    """``name`` 是否被引用 —— **调用**或**作为参数传递**都算。

    后者不是可有可无的宽松：本项目里 ``shutdown_tracing`` 是以
    ``await asyncio.to_thread(shutdown_tracing)`` 的形式传进去的，
    ``_on_detached_save_done`` 也是同一形状。只认 Call 会让这类接线漏检。
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == name:
            return True
        if isinstance(child, ast.Attribute) and child.attr == name:
            return True
    return False


def test_processor_chain_order_is_filter_guard_batch_chunker():
    """装配链顺序就是设计本身，必须钉死。

    顺序理由：过滤最外（先丢 98% HTTP span，后面不做无用功）→ 闸门在 Batch 之外
    （被丢的 span 不进队列）→ 分片最靠内（它约束的是**一次网络请求**的体积）。
    顺序错了不会报错，只会静默失效 —— 所以必须由测试守。
    """
    tree = _tree(TRACING_PY)
    builder = _func_def(tree, "_build_processor")
    assert builder is not None, "未找到统一装配函数 _build_processor"

    var_map = _local_call_map(builder)
    expected = [
        "_OpenInferenceOnlySpanProcessor",
        "_SpanPayloadGuardProcessor",
        "BatchSpanProcessor",
        "_ChunkedSpanExporter",
    ]
    chains = [
        _nesting_chain(node.value, var_map)
        for node in ast.walk(builder)
        if isinstance(node, ast.Return) and node.value is not None
    ]
    assert expected in chains, f"未找到预期装配链 {expected}；实际各条链：{chains}"



def test_both_exporter_channels_use_the_processor_builder():
    """Phoenix 与 Langfuse 两路都必须走同一装配函数 —— 漏一路就等于没修一半。"""
    tree = _tree(TRACING_PY)
    args = []
    for call in _calls(tree, "add_span_processor"):
        assert call.args, "add_span_processor 无实参"
        args.append(_callee(call.args[0]))

    assert len(args) == 2, f"期望两路 exporter，实际 {len(args)}"
    assert all(name == "_build_processor" for name in args), (
        f"有一路绕过了统一装配：{args}"
    )


def test_no_simple_span_processor_remains():
    """事故的放大器（同步导出）必须彻底消失，且检测器本身要能识别它。"""
    tree = _tree(TRACING_PY)
    used = {_callee(node) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "SimpleSpanProcessor" not in used, (
        "仍有 SimpleSpanProcessor —— 同步导出会把导出重试的开销压在请求路径上"
    )

    # 负向对照：证明上面的检测不是空转（若 _callee 坏了，这条会红）
    decoy = ast.parse("x = SimpleSpanProcessor(GrpcExporter(endpoint=e))")
    decoy_used = {_callee(n) for n in ast.walk(decoy) if isinstance(n, ast.Call)}
    assert "SimpleSpanProcessor" in decoy_used


def test_span_limits_are_built_from_settings_and_passed_to_provider():
    tree = _tree(TRACING_PY)

    limits_calls = _calls(tree, "SpanLimits")
    assert limits_calls, "未构造 SpanLimits —— L1 缺失（默认不截断属性值）"
    kwargs = {kw.arg for call in limits_calls for kw in call.keywords}
    assert {"max_span_attribute_length", "max_attribute_length"} <= kwargs, (
        f"SpanLimits 未设置值长度上限，实际关键字：{sorted(kwargs)}"
    )

    provider_calls = _calls(tree, "TracerProvider")
    assert provider_calls, "未构造 TracerProvider"
    assert any(
        any(kw.arg == "span_limits" for kw in call.keywords) for call in provider_calls
    ), "SpanLimits 构造了却没传给 TracerProvider —— 配置写完没接上"


def test_guard_and_chunker_read_limits_from_settings():
    """闸门/分片的阈值必须来自配置，不能是硬编码常量（否则无法排障时调整）。"""
    tree = _tree(TRACING_PY)
    guard_kwargs = {
        kw.arg for call in _calls(tree, "_SpanPayloadGuardProcessor") for kw in call.keywords
    }
    chunk_kwargs = {
        kw.arg for call in _calls(tree, "_ChunkedSpanExporter") for kw in call.keywords
    }
    assert "limit_bytes" in guard_kwargs
    assert "limit_bytes" in chunk_kwargs

    src = TRACING_PY.read_text(encoding="utf-8")
    assert "settings.otel_span_payload_limit_bytes" in src
    assert "settings.otel_export_request_bytes_limit" in src


def test_shutdown_tracing_flushes_before_provider_shutdown():
    """L4：必须先 force_flush 再 shutdown，顺序反了 flush 就落在关闭之后（无效）。"""
    tree = _tree(TRACING_PY)
    fn = _func_def(tree, "shutdown_tracing")
    assert fn is not None, "缺少 shutdown_tracing —— 换 Batch 后尾部 trace 必丢"

    lifecycle = [
        (node.lineno, _callee(node))
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and _callee(node) in ("force_flush", "shutdown")
    ]
    names = [name for _, name in lifecycle]
    assert "force_flush" in names and "shutdown" in names
    assert names.index("force_flush") < names.index("shutdown"), (
        f"shutdown 出现在 force_flush 之前：{lifecycle}"
    )


def _flushes_tracing_in_finally(fn: ast.AST) -> bool:
    """lifespan 是否在某个 ``finally`` 块里引用了 ``shutdown_tracing``。

    用「引用」而非「调用」判定：真实写法是
    ``await asyncio.to_thread(shutdown_tracing)`` —— 它是被**传递**进去的，
    只认 Call 会漏检（本项目 _on_detached_save_done 也是同一形状）。
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try) or not node.finalbody:
            continue
        if any(_references_name(stmt, "shutdown_tracing") for stmt in node.finalbody):
            return True
    return False


def test_shutdown_flush_timeout_leaves_room_in_container_stop_grace():
    """flush 超时必须明显小于容器停止宽限期，否则与 SIGKILL 抢时间、白做。

    `deploy/docker-compose.yml` 未设 `stop_grace_period` → `docker stop` 用默认
    **10s**，超时 SIGKILL。若这里给到 10s（或照抄 SDK 惯例的 30s），flush 会被
    强杀，而且会把宽限期吃光、让连接池 aclose 没时间做。故要求 ≤ 8s 留出余量。
    """
    import inspect

    from src.core.tracing import shutdown_tracing

    default = inspect.signature(shutdown_tracing).parameters["timeout_millis"].default
    assert isinstance(default, int) and default > 0
    assert default <= 8000, (
        f"flush 默认超时 {default} ms 逼近 docker 默认 10s 宽限期 —— "
        "要么调小，要么在 compose 里显式设置 stop_grace_period"
    )


def test_server_lifespan_flushes_tracing_in_finally():
    """server.py 的 lifespan 必须在**关停段**（finally）flush 追踪。

    为什么必须是 finally 而不是 try 主体：连接池 aclose 抛异常就会跳过 flush，
    尾部 trace 随进程消失 —— 这类"路径依赖的遗漏"只能由结构断言守住。
    """
    fn = _func_def(_tree(SERVER_PY), "lifespan")
    assert fn is not None, "未找到 lifespan"
    assert _flushes_tracing_in_finally(fn), "lifespan 未在 finally 中引用 shutdown_tracing"

    # 负向对照：把调用放进 try 主体 —— 检测器必须检不出来（否则这条断言等于没约束）
    decoy = _func_def(
        ast.parse(
            "async def lifespan(app):\n"
            "    try:\n"
            "        await asyncio.to_thread(shutdown_tracing)\n"
            "    finally:\n"
            "        pass\n"
        ),
        "lifespan",
    )
    assert not _flushes_tracing_in_finally(decoy), (
        "检测器把 try 主体里的引用也算作 finally —— 断言失去意义"
    )


# ─── 配置取值的关系式（比"魔法数字"更能防漂移）───────────────────────────────


def test_limits_are_ordered_below_the_server_hard_limit():
    """三条关系式，任何一条被破坏都会让防线失效。

    ① 单条 span 上限 < 单次请求上限 —— 否则每片都恰好超限，分片失去意义；
    ② 单次请求上限 < alloy 的 4 MiB —— 留余量，这是硬上限不可协商；
    ③ 属性值上限 > 0 且事件上限 < 属性值上限 —— 事件是日志行，不该占大额度。
    """
    span_limit = settings.otel_span_payload_limit_bytes
    request_limit = settings.otel_export_request_bytes_limit

    assert 0 < span_limit < request_limit, (span_limit, request_limit)
    assert request_limit < ALLOY_MAX_RECV_BYTES, (
        f"单次导出上限 {request_limit} 未低于 alloy 硬上限 {ALLOY_MAX_RECV_BYTES}"
    )
    assert 0 < settings.otel_event_attribute_value_limit < settings.otel_span_attribute_value_limit
