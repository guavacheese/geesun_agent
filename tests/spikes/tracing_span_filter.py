#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spike — tracing span 过滤器的端到端验证（2026-09-10）

被测对象（**直接 import 真实实现**，不做任何复刻）：
    src/core/tracing.py :: _OpenInferenceOnlySpanProcessor
                           _HTTP_EXCLUDED_URLS
                           _OPENINFERENCE_SPAN_KIND

背景：ea303fe 把三路 exporter 全注释，真实根因是容器 healthcheck 每 15s 探活
`/docs` 产生的 FastAPI HTTP server span 灌满观测后端（实测 Phoenix spans 表里
占 98.3%，真实 LLM trace 只剩 1.1%）。本次修法靠两层挡住它：

    ① excluded_urls —— 探活/文档端点在 ASGI middleware 入口直接 return（连 span
       都不创建），验证见场景 C；
    ② _OpenInferenceOnlySpanProcessor —— 只放行带 `openinference.span.kind` 的
       span，HTTP/ASGI span 一律丢弃，验证见场景 A / B。

本 spike 使用**真实 OTel SDK 链路 + 真实 LangChain Runnable + 真实 FastAPI 请求**，
不依赖任何 mock、不依赖外部 LLM 或网络（不碰 vLLM / Phoenix / Langfuse）。
只有场景 D/E 的边界与生命周期用例使用轻量替身对象（无法用真实 span 构造）。

运行：
    .venv/Scripts/python.exe tests/spikes/tracing_span_filter.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opentelemetry import trace as trace_api  # noqa: E402
from opentelemetry.sdk import trace as trace_sdk  # noqa: E402
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.util.http import parse_excluded_urls  # noqa: E402

from src.core.tracing import (  # noqa: E402
    _HTTP_EXCLUDED_URLS,
    _OPENINFERENCE_SPAN_KIND,
    _OpenInferenceOnlySpanProcessor,
)

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"\n         {detail}" if detail else ""))


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ────────────────────────────────────────────────────────────────────────────
# 场景 C：excluded_urls 对探活/文档端点命中、对业务 URL 不误伤
#   —— 直接用 OTel 自己的 parse_excluded_urls / ExcludeList，验证的是真实解析器
# ────────────────────────────────────────────────────────────────────────────
def scenario_c() -> None:
    section("C. excluded_urls 匹配语义（真实 ExcludeList，search 非 match）")
    print(f"  配置: _HTTP_EXCLUDED_URLS = {_HTTP_EXCLUDED_URLS!r}")

    excluded = parse_excluded_urls(_HTTP_EXCLUDED_URLS)

    # 应命中（探活 / 文档类）
    should_hit = [
        "http://127.0.0.1:8009/healthz",
        "http://127.0.0.1:8009/api/health",
        "http://127.0.0.1:8009/docs",
        "http://127.0.0.1:8009/docs/oauth2-redirect",
        "http://127.0.0.1:8009/openapi.json",
        "http://127.0.0.1:8009/redoc",
    ]
    # 不应命中（真实业务路由，取自 src/api/endpoints/*）
    should_miss = [
        "http://127.0.0.1:8009/api/v1/chat",
        "http://127.0.0.1:8009/api/v1/sessions",
        "http://127.0.0.1:8009/api/v1/models",
        "http://127.0.0.1:8009/api/v1/skills",
        "http://127.0.0.1:8009/api/v1/upload",
        "http://127.0.0.1:8009/api/v1/files/GY24428/abc/report.docx",
        # 易误伤样本：含 "doc" 但不是 /docs
        "http://127.0.0.1:8009/api/v1/documents",
        "http://127.0.0.1:8009/api/v1/healthcheck-detail",
    ]

    hit_fail = [u for u in should_hit if not excluded.url_disabled(u)]
    miss_fail = [u for u in should_miss if excluded.url_disabled(u)]

    check(
        f"命中探活/文档端点 {len(should_hit) - len(hit_fail)}/{len(should_hit)}",
        not hit_fail,
        f"未命中: {hit_fail}" if hit_fail else "全部正确排除",
    )
    check(
        f"不误伤业务 URL {len(should_miss) - len(miss_fail)}/{len(should_miss)}",
        not miss_fail,
        f"被误伤: {miss_fail}" if miss_fail else "全部正确保留",
    )


# ────────────────────────────────────────────────────────────────────────────
# 场景 A/B：真实链路 —— LangChain span 放行、FastAPI HTTP span 丢弃
# ────────────────────────────────────────────────────────────────────────────
def scenario_ab() -> None:
    section("A/B. 真实链路：LangChain span 放行 / FastAPI HTTP span 丢弃")

    raw_exporter = InMemorySpanExporter()      # 对照组：不过滤，看到全部 span
    kept_exporter = InMemorySpanExporter()     # 实验组：经 _OpenInferenceOnlySpanProcessor

    provider = trace_sdk.TracerProvider(
        resource=Resource.create({"service.name": "spike-span-filter"})
    )
    provider.add_span_processor(SimpleSpanProcessor(raw_exporter))
    provider.add_span_processor(
        _OpenInferenceOnlySpanProcessor(SimpleSpanProcessor(kept_exporter))
    )
    trace_api.set_tracer_provider(provider)

    # ── 真实 LangChain 埋点（OpenInference auto-instrument）──
    langchain_spans_seen = 0
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor

        LangChainInstrumentor().instrument()

        from langchain_core.runnables import RunnableLambda

        chain = RunnableLambda(lambda x: x + 1) | RunnableLambda(lambda x: x * 2)
        chain.invoke(1)
        langchain_spans_seen = len(raw_exporter.get_finished_spans())
    except Exception as exc:  # pragma: no cover
        check("LangChain span 生成", False, f"异常: {exc!r}")

    check(
        "① 真实 LangChain 调用产生了 span（前置条件）",
        langchain_spans_seen > 0,
        f"raw exporter 收到 {langchain_spans_seen} 条",
    )

    # ── 真实 FastAPI 埋点 ──
    # ⚠️ 顺序铁律：FastAPIInstrumentor.instrument() 的实现是
    #     `fastapi.FastAPI = _InstrumentedFastAPI`（patch 类属性，见
    #     opentelemetry/instrumentation/fastapi/__init__.py:442-445），
    #     因此 `from fastapi import FastAPI` **必须在 instrument() 之后**执行 ——
    #     否则该名字绑定到未被 patch 的旧类，创建的 app 完全不被埋点，
    #     且**不报任何错**（本 spike 首轮就在此栽了：18 项里 2 项 FAIL，
    #     raw exporter 一条 HTTP span 都没有）。
    #     项目 src/server.py 的顺序恰好正确：setup_tracing() 在 L13，
    #     `from fastapi import FastAPI` 在 L17。
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    # 与 src/core/tracing.py 第 4 步完全一致的调用方式
    FastAPIInstrumentor().instrument(excluded_urls=_HTTP_EXCLUDED_URLS)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/healthz", include_in_schema=False)
    def _h() -> dict:
        return {"status": "ok"}

    @app.get("/api/v1/biz")
    def _biz() -> dict:
        return {"ok": True}

    before = len(raw_exporter.get_finished_spans())
    with TestClient(app) as client:
        r_health = client.get("/healthz")
        r_biz = client.get("/api/v1/biz")
    after = len(raw_exporter.get_finished_spans())

    raw_all = raw_exporter.get_finished_spans()
    kept_all = kept_exporter.get_finished_spans()
    http_raw = [s for s in raw_all if "openinference.span.kind" not in (s.attributes or {})]
    http_kept = [s for s in kept_all if "openinference.span.kind" not in (s.attributes or {})]
    oi_raw = [s for s in raw_all if "openinference.span.kind" in (s.attributes or {})]
    oi_kept = [s for s in kept_all if "openinference.span.kind" in (s.attributes or {})]

    print(f"  请求结果: /healthz -> {r_health.status_code}, /api/v1/biz -> {r_biz.status_code}")
    print(f"  raw  exporter: 总计 {len(raw_all)} 条 = OI {len(oi_raw)} + HTTP {len(http_raw)}")
    print(f"  kept exporter: 总计 {len(kept_all)} 条 = OI {len(oi_kept)} + HTTP {len(http_kept)}")

    check(
        "② /healthz 零 span —— excluded_urls 在 ASGI 入口直接 return",
        not any("/healthz" in (s.name or "") for s in raw_all),
        "raw exporter 中无任何 /healthz span",
    )
    check(
        "③ /api/v1/biz 在 raw 中产生 HTTP span（证明该请求未被 excluded 误杀）",
        any("/api/v1/biz" in (s.name or "") for s in http_raw),
        f"HTTP span 名称样本: {sorted({s.name for s in http_raw})[:5]}",
    )
    check(
        "④ HTTP span 全部被过滤丢弃（kept 中 HTTP 数为 0）",
        len(http_kept) == 0,
        f"raw 有 {len(http_raw)} 条 HTTP span，kept 有 {len(http_kept)} 条",
    )
    check(
        f"⑤ OpenInference span 一条不漏（kept={len(oi_kept)} == raw={len(oi_raw)}）",
        len(oi_kept) == len(oi_raw) and len(oi_raw) > 0,
        f"span.kind 取值: {sorted({(s.attributes or {}).get(_OPENINFERENCE_SPAN_KIND) for s in oi_kept})}",
    )
    check(
        "⑥ 过滤后 trace_id 保持不变（HTTP root 被丢弃不影响 trace 聚合）",
        {s.context.trace_id for s in oi_kept} == {s.context.trace_id for s in oi_raw},
        "kept 与 raw 的 OpenInference span trace_id 集合一致",
    )
    print(f"  过滤统计: 放行={_OpenInferenceOnlySpanProcessor._passed} "
          f"丢弃={_OpenInferenceOnlySpanProcessor._dropped}")

    # ── 场景 E 之一：on_start 转发 ──
    check(
        "⑦ on_start 正常转发（http send/receive 子 span 也在 raw 中出现）",
        any((s.name or "").endswith("http send") or "http receive" in (s.name or "")
            for s in http_raw),
        f"raw 中 HTTP 子 span 计数 = {len(http_raw)}",
    )


# ────────────────────────────────────────────────────────────────────────────
# 场景 D/E：边界与生命周期（轻量替身，无法用真实 span 构造）
# ────────────────────────────────────────────────────────────────────────────
class _Recorder:
    """记录下游调用的替身 processor。"""

    def __init__(self) -> None:
        self.ended: list = []
        self.started: list = []
        self.shutdown_called = False
        self.flush_called = False

    def on_start(self, span, parent_context=None) -> None:
        self.started.append(span)

    def on_end(self, span) -> None:
        self.ended.append(span)

    def shutdown(self) -> None:
        self.shutdown_called = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flush_called = True
        return True


class _FakeSpan:
    def __init__(self, attributes) -> None:
        self.attributes = attributes


def scenario_de() -> None:
    section("D/E. 边界（attributes 缺失/为空）与生命周期转发")

    cases = [
        ("带 openinference.span.kind=LLM", {_OPENINFERENCE_SPAN_KIND: "LLM"}, True),
        ("带 openinference.span.kind=CHAIN", {_OPENINFERENCE_SPAN_KIND: "CHAIN"}, True),
        ("值为空字符串（仅判存在性）", {_OPENINFERENCE_SPAN_KIND: ""}, True),
        ("普通 HTTP span（无该属性）", {"http.method": "GET", "http.url": "/docs"}, False),
        ("attributes 为空 dict", {}, False),
        ("attributes 为 None", None, False),
    ]
    for label, attrs, should_pass in cases:
        rec = _Recorder()
        proc = _OpenInferenceOnlySpanProcessor(rec)
        proc.on_end(_FakeSpan(attrs))
        got = len(rec.ended) == 1
        check(f"    {label} -> {'放行' if should_pass else '丢弃'}", got == should_pass,
              f"下游收到 {len(rec.ended)} 条")

    rec = _Recorder()
    proc = _OpenInferenceOnlySpanProcessor(rec)
    fake = _FakeSpan({_OPENINFERENCE_SPAN_KIND: "LLM"})
    proc.on_start(fake, None)
    check("on_start 转发到下游", len(rec.started) == 1)
    check("force_flush 转发并返回下游结果", proc.force_flush(1000) is True and rec.flush_called)
    proc.shutdown()
    check("shutdown 转发到下游", rec.shutdown_called)


def main() -> int:
    print("spike: tracing span 过滤器端到端验证")
    scenario_c()
    scenario_ab()
    scenario_de()

    section("汇总")
    failed = [r for r in RESULTS if not r[0]]
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name}  {detail}")
    print(f"\n  总计 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
