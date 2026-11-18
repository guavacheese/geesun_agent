"""Spike: model_call_guard 400 上下文超限恢复链（F0+F1+G1/G2/G3，2026-11-18）。

故障复现（生产 f508e75c，2026-11-17 15:18-15:31，5 轮 × 2 次）：
旧兜底 `keep = max(4, len(msgs) - (attempt+1)*10)` 砍最老消息，而膨胀源
（巨型 pptx tool result）在尾部 → 重试 real_input 恒 262145 → 全部无效
→ 原样上抛 → 用户连发 4 条全部「本轮无 AI 产出」。

新实现验证点：
[F1] ① _is_context_overflow_exc 多模式归类（vLLM/OpenAI/DeepSeek 文案 + 非
        超限 400 不误伤）；
     ② _trim_large_tool_results 确定性裁剪（头 4096/尾 1024、幂等、只裁
        ToolMessage）；
     ③ guard 端到端：fake handler 首次抛 vLLM 400 → 恢复裁剪 → 重试成功；
[G1] ④ 全程 400 → 重试耗尽 → **换型** ContextOverflowError 上抛（激活
        deepagents 0.6.12 summarization.py:1569 自带的摘要+offload 救援；
        vLLM 文案不匹配 langchain_openai 的映射，原始异常穿不进该 except，
        f508e75c 2026-11-18 08:24 实证）；
[G2] ⑤ 400 的 real_input 回填每会话引擎缓存（重启冷启动 + 估算失真组合
        曾让 trigger 永不触发）；
[G3] ⑥ 裁剪量以 real_input 锚定（scale = real_input/est_before），本地估算
        低估 2.6 倍时仍砍得够（旧实现 est gate 直接跳过砍量，两次重试全败）；
[F0] ⑦ _proportional_margin（262144→16384 不变，32768→3276）；
     ⑧ per-model 修正回写 _MAX_LEN_CACHE，settings.model_max_len 不被改写。

运行: python tests/spikes/context_overflow_recovery.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Settings 必填字段兜底（不依赖 .env，spike 独立可跑）
import os

for _k, _v in (
    ("BASE_URL", "http://fake:8003/v1"),
    ("OPENAI_API_KEY", "spike-key"),
    ("MODEL_NAME", "Qwen3.6-35B-A3B"),
    ("AGENT_WORKSPACE", "./.spike-workspace"),
):
    os.environ.setdefault(_k, _v)

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.core import model as m
from src.core.config import settings

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name} {detail}")


VLLM_MSG = (
    "Error code: 400 - {'error': {'message': \"This model's maximum context length "
    "is 262144 tokens. However, your prompt contains at least 262145 input tokens.\"}}"
)
OPENAI_MSG = (
    "Error code: 400 - This model's maximum context length is 128000 tokens. "
    "However, your messages resulted in 130000 tokens."
)
DEEPSEEK_MSG = "Error 400: context_length_exceeded: prompt exceeds model limit"
UNSUPPORTED_MSG = "Error code: 400 - invalid_request_error: malformed tool arguments"
NO_STATUS_MSG = "Request failed: maximum context length exceeded by input"


class FakeBadRequest(Exception):
    def __init__(self, msg: str, status_code: int = 400):
        super().__init__(msg)
        self.status_code = status_code


print("\n[1] _is_context_overflow_exc 归类")
check(
    "vLLM 400 文案（f508e75c 实录）→ True",
    m._is_context_overflow_exc(FakeBadRequest(VLLM_MSG)),
)
check(
    "OpenAI 400 文案 → True",
    m._is_context_overflow_exc(FakeBadRequest(OPENAI_MSG)),
)
check(
    "DeepSeek context_length_exceeded → True",
    m._is_context_overflow_exc(FakeBadRequest(DEEPSEEK_MSG)),
)
check(
    "非超限 400（malformed arguments）→ False（不误伤）",
    not m._is_context_overflow_exc(FakeBadRequest(UNSUPPORTED_MSG)),
)
check(
    "无 status_code + 强特征文案 → True（包装后退化路径）",
    m._is_context_overflow_exc(Exception(NO_STATUS_MSG)),
)
check(
    "无 status_code + 无关文案 → False",
    not m._is_context_overflow_exc(Exception("some random failure")),
)
check(
    "500 无关错误 → False",
    not m._is_context_overflow_exc(FakeBadRequest("internal error", 500)),
)

print("\n[2] _trim_large_tool_results 确定性裁剪")
big = "X" * 50000
msgs = [
    HumanMessage(content="hello"),
    AIMessage(
        content="",
        tool_calls=[{"name": "read_file", "args": {}, "id": "call1", "type": "tool_call"}],
    ),
    ToolMessage(content=big, tool_call_id="call1"),
    ToolMessage(content="small result", tool_call_id="call2"),
    HumanMessage(content="world"),
]
trimmed = m._trim_large_tool_results(msgs)
check("列表长度不变", len(trimmed) == len(msgs))
t_tool = trimmed[2]
check(
    "大 ToolMessage 被裁到 head+marker+tail",
    isinstance(t_tool, ToolMessage)
    and len(t_tool.content) < 6000
    and "trimmed" in t_tool.content
    and t_tool.content.startswith("X" * 100)
    and t_tool.content.endswith("X" * 100)
    and t_tool.tool_call_id == "call1",
    f"len={len(t_tool.content)}",
)
check("小 ToolMessage 不动", trimmed[3].content == "small result")
check("HumanMessage 不动", trimmed[0].content == "hello" and trimmed[4].content == "world")
# 幂等：对裁剪结果再裁，长度不变（已 ≤ threshold）
again = m._trim_large_tool_results(trimmed)
check("幂等：二次裁剪无变化", again == trimmed)
# 无命中返回原对象（零开销路径）
small_only = [HumanMessage(content="hi"), ToolMessage(content="ok", tool_call_id="c")]
check("无大结果时返回原列表", m._trim_large_tool_results(small_only) is small_only)

print("\n[3] _proportional_margin 比例化")
check("262144 → 16384（配置值生效）", m._proportional_margin(262144) == 16384)
check("32768 → 3276（10% 而非 16384=50%）", m._proportional_margin(32768) == 3276)
check("1000000 → 16384（配置值封顶）", m._proportional_margin(1_000_000) == 16384)
check("极小窗口 → ≥1", m._proportional_margin(10) == 1)

print("\n[4] guard 端到端：400 → 恢复 → 重试成功")


class FakeChatModel:
    """最小 fake：model_call_guard 只用到 _session_id / get_num_tokens_from_messages /
    model_name / openai_api_base / openai_api_key / model_settings 默认。"""

    _session_id = "spike-session"
    model_name = "Qwen3.6-35B-A3B"
    openai_api_base = "http://fake:8003/v1"
    openai_api_key = "test-key"

    def get_num_tokens_from_messages(self, msgs):
        return sum(len(str(getattr(x, "content", ""))) // 2 for x in msgs)


def _make_request():
    from langchain.agents.middleware import ModelRequest

    return ModelRequest(
        model=FakeChatModel(),
        messages=[
            HumanMessage(content="hi"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {}, "id": "c1", "type": "tool_call"}
                ],
            ),
            ToolMessage(content="Y" * 50000, tool_call_id="c1"),
        ],
        model_settings={},
    )


async def run_guard_once():
    """构造 guard 闭包场景：handler 首调抛 vLLM 400，重试后成功。
    返回 (resp, handler 收到的 messages, handler 收到的 max_tokens)。"""
    calls = {"n": 0, "msgs": [], "max_tokens": []}
    m._MAX_LEN_CACHE[("http://fake:8003/v1", "Qwen3.6-35B-A3B")] = 262144
    m._session_prompt_tokens.pop("spike-session", None)

    async def handler(request):
        calls["n"] += 1
        calls["msgs"] = list(request.messages)
        calls["max_tokens"].append(request.model_settings.get("max_tokens"))
        if calls["n"] == 1:
            raise FakeBadRequest(VLLM_MSG)
        return "OK-RESPONSE"

    from src.core.model import model_call_guard

    req = _make_request()

    async def call():
        return await model_call_guard.awrap_model_call(req, handler)

    resp = await call()
    return resp, calls


resp, calls = asyncio.run(run_guard_once())
check("guard 正常返回", resp == "OK-RESPONSE")
check("handler 被调用 2 次（1 次 400 + 1 次恢复）", calls["n"] == 2, f"n={calls['n']}")
t = calls["msgs"][2]
check(
    "恢复后的请求里大 tool result 已被裁剪",
    isinstance(t, ToolMessage) and len(t.content) < 6000,
    f"len={len(getattr(t, 'content', ''))}",
)
# G3 精确值核对（真值锚定口径）：est_before = 1+0+25000 = 25001（确定性），
# scale = real_input/est_before；被裁真实 token = removed_chars × scale / 2；
# safe_max = limit − (real_input − 被裁真实 token) − margin(16384)
_trimmed_tool_len = len(calls["msgs"][2].content)
_removed_chars = 50000 - _trimmed_tool_len
_est_before = 1 + 0 + 50000 // 2  # Human("hi")=1 + AIMessage("")=0 + Tool 25000
_scale = 262145 / _est_before
_removed_real = int(_removed_chars * _scale / 2)
_expected_safe_max = max(
    m._MODEL_MIN_OUTPUT_TOKENS, 262144 - (262145 - _removed_real) - 16384
)
check("重试 max_tokens 被收紧到安全值", calls["max_tokens"][-1] < 262144, str(calls["max_tokens"]))
check(
    "safe_max 数值 = limit − (real_input − 被裁真实token[scale折算]) − margin（G3）",
    calls["max_tokens"][-1] == _expected_safe_max,
    f"got {calls['max_tokens'][-1]}, expected {_expected_safe_max}",
)
check(
    "per-model 缓存仍为探活值（未被污染）",
    m._MAX_LEN_CACHE[("http://fake:8003/v1", "Qwen3.6-35B-A3B")] == 262144,
)
check("全局 settings.model_max_len 未被改写", settings.model_max_len == 262144)

print("\n[4b] G2：400 的 real_input 回填每会话引擎缓存")
check(
    "会话缓存 = 400 真值 real_input（重启冷启动后下一轮 trigger 可用）",
    m._session_prompt_tokens.get("spike-session") == 262145,
    f"got {m._session_prompt_tokens.get('spike-session')}",
)

print("\n[5] guard 端到端：全程 400 → 重试耗尽 → G1 换型 ContextOverflowError 上抛")


async def run_guard_exhausted():
    m._MAX_LEN_CACHE[("http://fake:8003/v1", "Qwen3.6-35B-A3B")] = 262144
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        raise FakeBadRequest(VLLM_MSG)

    from src.core.model import model_call_guard

    try:
        await model_call_guard.awrap_model_call(_make_request(), handler)
        return "no-raise"
    except Exception as e:  # 换型断言：类型必须是 ContextOverflowError，且原文保留
        return f"raised:{calls['n']}:{type(e).__module__}.{type(e).__name__}:{str(e)[:120]}"


result = asyncio.run(run_guard_exhausted())
check(
    "重试 2 次耗尽后换型 ContextOverflowError 上抛（vLLM 原文保留）",
    result.startswith("raised:3:langchain_core.exceptions.ContextOverflowError")
    and "maximum context length" in result,
    f"got {result}",
)

print("\n[6] per-model 400 修正回写 _MAX_LEN_CACHE")


async def run_guard_limit_fix():
    key = ("http://fake32k:8003/v1", "small-model")
    m._MAX_LEN_CACHE[key] = 262144  # 探活缓存是错的（旧值）
    calls = {"n": 0}

    class FakeSmall(FakeChatModel):
        model_name = "small-model"
        openai_api_base = "http://fake32k:8003/v1"
        _session_id = "spike-32k"

    async def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FakeBadRequest(
                "Error code: 400 - This model's maximum context length is 32768 tokens. "
                "However, your prompt contains at least 40000 input tokens."
            )
        return "OK"

    from src.core.model import model_call_guard
    from langchain.agents.middleware import ModelRequest

    req = ModelRequest(
        model=FakeSmall(), messages=[HumanMessage(content="hi")], model_settings={}
    )
    await model_call_guard.awrap_model_call(req, handler)
    return m._MAX_LEN_CACHE[key]


fixed = asyncio.run(run_guard_limit_fix())
check(
    "400 解析的 real_limit=32768 回写 per-model 缓存（纠正探活旧值）",
    fixed == 32768,
    f"got {fixed}",
)

print("\n[7] 恢复成功后缓存补回被裁 token（防裁剪视图掩盖 trigger）")


async def run_guard_cache_comp():
    """400 → 裁剪恢复 → 成功返回带 usage 的 AIMessage。
    断言会话缓存 = usage.input_tokens + 被裁字符的 token 估算。"""
    m._MAX_LEN_CACHE[("http://fake:8003/v1", "Qwen3.6-35B-A3B")] = 262144
    m._session_prompt_tokens.pop("spike-session", None)
    state = {"n": 0, "trimmed_tool_len": None}

    async def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            raise FakeBadRequest(VLLM_MSG)
        state["trimmed_tool_len"] = len(request.messages[2].content)
        return AIMessage(
            content="ok",
            usage_metadata={
                "input_tokens": 123456,
                "output_tokens": 100,
                "total_tokens": 123556,
            },
        )

    from src.core.model import model_call_guard

    await model_call_guard.awrap_model_call(_make_request(), handler)
    return m._session_prompt_tokens.get("spike-session"), state


cache_val, st = asyncio.run(run_guard_cache_comp())
_scale7 = 262145 / (1 + 0 + 50000 // 2)
_removed_real7 = int((50000 - st["trimmed_tool_len"]) * _scale7 / 2)
check(
    "恢复成功后缓存 = usage.input_tokens + 被裁真实token折算（G3 scale 口径）",
    cache_val == 123456 + _removed_real7,
    f"got {cache_val}, expected {123456 + _removed_real7}",
)

print("\n[8] G3：本地估算低估 2.6 倍（f508e75c 同型）→ 仍砍得够")


async def run_guard_g3():
    """复刻 f508e75c 08:24 形态：150 条中小 ToolMessage（无单条 >8192，第 1 步
    裁剪 0 字符），chars//2 本地估算 ≈97.5k 远低于 157k trigger 阈值，但引擎真值
    262145。旧实现：est gate 跳过砍量 → 两次重试全败。新实现：real_input 锚定
    gate + est_budget 换算 → 第 2 步真砍 → 第 3 次调用消息数变少。"""
    m._MAX_LEN_CACHE[("http://fake:8003/v1", "Qwen3.6-35B-A3B")] = 262144
    m._session_prompt_tokens.pop("spike-session", None)

    msgs = [HumanMessage(content="hi")]
    for i in range(150):
        msgs.append(AIMessage(content="", tool_calls=[
            {"name": "execute", "args": {}, "id": f"c{i}", "type": "tool_call"}
        ]))
        msgs.append(ToolMessage(content="X" * 1300, tool_call_id=f"c{i}"))

    from langchain.agents.middleware import ModelRequest

    req = ModelRequest(model=FakeChatModel(), messages=msgs, model_settings={})
    seen_counts = []

    async def handler(request):
        seen_counts.append(len(request.messages))
        raise FakeBadRequest(VLLM_MSG)  # 恒 400：耗尽走 G1，但裁剪量可断言

    from src.core.model import model_call_guard

    from langchain_core.exceptions import ContextOverflowError

    try:
        await model_call_guard.awrap_model_call(req, handler)
        return "no-raise", seen_counts
    except ContextOverflowError:
        return "ctx-overflow", seen_counts


result8, counts8 = asyncio.run(run_guard_g3())
est_before8 = 1 + 150 * (1300 // 2)  # Human + 150×Tool（AIMessage content 空）
scale8 = 262145 / est_before8
real_budget8 = 262144 - 16384 - m._MODEL_MIN_OUTPUT_TOKENS
est_budget8 = int(real_budget8 / scale8)
# 第 2 步必须把本地 est 砍到 est_budget 以下（旧实现 est 97.5k + margin < limit
# 直接跳过，消息数不变）
check("G1 换型正常返回 ContextOverflowError", result8 == "ctx-overflow", str(result8))
check(
    "砍量实际发生（消息数减少，而非旧实现的 0 裁剪）",
    len(counts8) == 3 and counts8[2] < counts8[0],
    f"counts={counts8}",
)
kept8 = counts8[2] if len(counts8) > 2 else counts8[0]
est_kept8 = (1 + (kept8 - 1) // 2 * 650)  # 粗口径：裁掉的按 650 est/组
check(
    "砍后本地 est ≤ est_budget（换算回真实口径 ≤ real_budget）",
    est_kept8 <= est_budget8 + 700,  # 700 = 单组容差（cut 粒度为一对 AIM+Tool）
    f"est_kept={est_kept8}, est_budget={est_budget8}, counts={counts8}",
)

print(f"\n{'=' * 60}")
print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    sys.exit(1)
print("ALL OK — F0+F1 spike 验证通过")
