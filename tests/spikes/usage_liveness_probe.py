"""生产容器内 liveness 探针：证明「引擎真实 token 计数」链路真的跑通。

与 tests/spikes/token_usage_extract.py 的区别：那个验证的是 _extract_tokens 纯函数，
本探针走**真实 model_call_guard 中间件**（生产实际装配的那一个），因此能同时验证：
  · _capture_usage 把引擎真实值写进每会话缓存（供 Summarization / dynamic max_tokens 读）
  · _record_genai_metrics 把 input/output tokens 记进 gen_ai.client.token.usage 指标
并由 setup_tracing() 注册真实 MeterProvider，指标经 OTLP 推到 alloy → Prometheus。

判定：缓存非 None 且 ok>=1 → 链路已通；指标是否落库由外部查 Prometheus 复核。
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")

SID = "probe-usage-liveness"


async def main() -> None:
    print("=" * 72)
    print("[1] setup_tracing（注册 tracer + meter，与生产同路径）")
    print("=" * 72)
    from src.core.tracing import setup_tracing

    print("    setup_tracing ->", setup_tracing())

    import src.core.model as M
    from langchain.agents import create_agent
    from langchain.agents.middleware import wrap_model_call
    from langchain_core.messages import HumanMessage

    model = await M.create_model()
    model._session_id = SID
    print("[2] 模型已挂 session_id =", SID)

    seen = []

    @wrap_model_call
    async def spy(request, handler):
        resp = await handler(request)
        seen.append(resp)
        return resp

    # 与生产同一段装配：spy 观察 handler 返回值，model_call_guard 是生产实际用的那一个
    agent = create_agent(
        model=model, tools=[], middleware=[spy, M.model_call_guard]
    )
    await agent.ainvoke(
        {"messages": [HumanMessage(content="用一句话说明什么是 PLC。")]}
    )

    resp = seen[0] if seen else None
    print("[3] handler 返回类型 =", type(resp).__name__,
          "| .result 类型 =", type(getattr(resp, "result", None)).__name__)

    cached = M.get_engine_prompt_tokens(SID)
    print("[4] ★ 每会话缓存 get_engine_prompt_tokens(%r) = %r" % (SID, cached))
    print("[5] 健康度计数 ok=%d miss=%d" % (M._usage_ok_count, M._usage_miss_count))

    from opentelemetry import metrics as metrics_api

    mp = metrics_api.get_meter_provider()
    flush = getattr(mp, "force_flush", None)
    print("[6] MeterProvider =", type(mp).__name__,
          "| force_flush ->", (flush(30000) if flush else "n/a"))

    verdict = (cached is not None and cached > 0 and M._usage_ok_count >= 1)
    print()
    print("【判定】", "✅ 链路已通（缓存写入 + 计数递增）" if verdict else "❌ 链路仍断")
    sys.exit(0 if verdict else 1)


asyncio.run(main())
