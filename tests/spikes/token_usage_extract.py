"""Spike: 验证「引擎真实 token 计数」链路——_extract_tokens 解包与 _capture_usage 落缓存。

背景（2026-09-11 生产实证）：`stream_usage=True` 早已设置、vLLM 也确实回 usage，
但 token 系列指标零 series、缓存永为空。根因是 `_extract_tokens` 解包少剥一层——
langchain 1.x 的 `ModelResponse.result` 类型是 **list[BaseMessage]**（@dataclass），
旧代码把 resp 替换成整个 list，随后对 list 取 usage_metadata 恒为 None。

运行环境：**生产同款镜像**（.venv 是 Linux 布局，Windows 本机 Python 用不了）：
  docker run --rm -v 'D:/workspace/geesun_agent/src:/app/src:ro' \
    -v 'D:/workspace/geesun_agent/tests:/app/tests:ro' --entrypoint sh \
    172.16.220.74:8333/geesun_ai/geesun-agent:<TAG> \
    -c 'cd /app && /app/.venv/bin/python tests/spikes/token_usage_extract.py'

覆盖：
  A) _extract_tokens 的形态矩阵（含本 bug 的回归位、结构化输出混合 ToolMessage、空列表）
  B) _capture_usage 把引擎真实值写进每会话缓存（修复前恒不写入）
  C) 健康度计数：miss/ok 都留痕（替代原「全局只告警一次」的误判源）
  D) 端到端：真实 middleware 链（create_agent + wrap_model_call）取到真实 vLLM usage
  E) 端到端：astream（生产真实 SSE 路径）同样取到
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, "/app")

from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

import src.core.model as M
from src.core.model import _capture_usage, _extract_tokens, get_engine_prompt_tokens

PASS = 0
FAIL = 0


def check(label: str, actual: Any, expected: Any) -> None:
    global PASS, FAIL
    ok = actual == expected
    if ok:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}\n      期望: {expected!r}\n      实际: {actual!r}")


USAGE = {"input_tokens": 111, "output_tokens": 22, "total_tokens": 133}


def ai(usage: dict | None = USAGE, **kw) -> AIMessage:
    return AIMessage(content="hi", usage_metadata=usage, **kw)


print("=" * 74)
print("[A] _extract_tokens 形态矩阵")
print("=" * 74)

# A1 裸 AIMessage（非 middleware 路径 / 探针直调——修复前唯一能过的形态）
check("A1 裸 AIMessage", _extract_tokens(ai()), (111, 22))

# A2 ★ 本 bug 的回归位：生产真实形态（修复前 = (None, None)）
check("A2 ModelResponse(result=[AIMessage])  ← 回归位",
      _extract_tokens(ModelResponse(result=[ai()])), (111, 22))

# A3/A4 结构化输出场景：可能与 ToolMessage 混排，位置不保证
tm = ToolMessage(content="tool out", tool_call_id="t1")
check("A3 result=[AIMessage, ToolMessage]",
      _extract_tokens(ModelResponse(result=[ai(), tm])), (111, 22))
check("A4 result=[ToolMessage, AIMessage]",
      _extract_tokens(ModelResponse(result=[tm, ai()])), (111, 22))

# A5 第一条无 usage、第二条有 → 必须挑带 usage 的，不能盲取 [0]
check("A5 result=[AIMessage(无usage), AIMessage(有usage)]",
      _extract_tokens(ModelResponse(result=[ai(None), ai()])), (111, 22))

# A6 空列表：不得抛错
try:
    check("A6 result=[] 不抛错", _extract_tokens(ModelResponse(result=[])), (None, None))
except Exception as e:  # noqa: BLE001
    FAIL += 1
    print(f"  ✗ A6 result=[] 抛错: {type(e).__name__}: {e}")

# A7 兼容旧形态（单条消息）
check("A7 ModelResponse(result=AIMessage)", _extract_tokens(ModelResponse(result=ai())), (111, 22))

# A8/A9 第 2、3 层来源仍然可用（provider 差异兜底）
m2 = AIMessage(content="x")
m2.usage = {"prompt_tokens": 7, "completion_tokens": 3}
check("A8 usage dict 兜底 (prompt_tokens)", _extract_tokens(m2), (7, 3))
m3 = AIMessage(content="x", response_metadata={"usage": {"prompt_tokens": 9, "completion_tokens": 4}})
check("A9 response_metadata.usage 兜底", _extract_tokens(m3), (9, 4))

# A10 全空 → (None, None)，调用方据此跳过指标（不记 0）
check("A10 全空返回 (None, None)", _extract_tokens(ai(None)), (None, None))

print()
print("=" * 74)
print("[B] _capture_usage 落每会话缓存")
print("=" * 74)
M._session_prompt_tokens.clear()
sid = "spike-session-1"
req = SimpleNamespace(model=SimpleNamespace(_session_id=sid))
_capture_usage(ModelResponse(result=[ai()]), req)
check("B1 缓存已写入引擎真实值", get_engine_prompt_tokens(sid), 111)

# 无 session id 时不得写（防串号）
req_no = SimpleNamespace(model=SimpleNamespace(_session_id=None))
before = dict(M._session_prompt_tokens)
_capture_usage(ModelResponse(result=[ai()]), req_no)
check("B2 无 session_id 不写缓存", M._session_prompt_tokens == before, True)

print()
print("=" * 74)
print("[C] 健康度计数（替代「全局只告警一次」）")
print("=" * 74)
M._usage_ok_count = 0
M._usage_miss_count = 0
_capture_usage(ModelResponse(result=[ai(None)]), SimpleNamespace(
    model=SimpleNamespace(_session_id="s-miss")))
check("C1 缺失计入 miss", (M._usage_ok_count, M._usage_miss_count), (0, 1))
_capture_usage(ModelResponse(result=[ai()]), SimpleNamespace(
    model=SimpleNamespace(_session_id="s-ok")))
check("C2 成功计入 ok", (M._usage_ok_count, M._usage_miss_count), (1, 1))
check("C3 阈值常量存在", M._USAGE_MISS_LOG_EVERY, 50)

print()
print("=" * 74)
print("[D/E] 端到端：真实 middleware 链 + 真实 vLLM")
print("=" * 74)

BASE = os.environ.get("BASE_URL", "")
KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = os.environ.get("MODEL_NAME", "")
if not (BASE and KEY and MODEL):
    print("  ⊘ 跳过：未提供 BASE_URL / OPENAI_API_KEY / MODEL_NAME（纯离线段 A/B/C 已覆盖回归位）")
else:
    from langchain.agents import create_agent
    from langchain.agents.middleware import wrap_model_call
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI

    async def run(stream: bool):
        seen = []

        @wrap_model_call
        async def probe(request, handler):
            resp = await handler(request)   # ← 与 model_call_guard 同一位置
            seen.append(resp)
            return resp

        model = ChatOpenAI(base_url=BASE, api_key=KEY, model=MODEL, temperature=0,
                           max_tokens=24, stream_usage=True)
        agent = create_agent(model=model, tools=[], middleware=[probe])
        payload = {"messages": [HumanMessage(content="说一句话")]}
        if stream:
            async for _ in agent.astream(payload, stream_mode="values"):
                pass
        else:
            await agent.ainvoke(payload)
        return seen[0] if seen else None

    for label, stream in (("[D] ainvoke 路径", False), ("[E] astream 路径（生产 SSE）", True)):
        try:
            resp = asyncio.run(run(stream))
            if resp is None:
                FAIL += 1
                print(f"  ✗ {label}: middleware 未被调用")
                continue
            got = _extract_tokens(resp)
            print(f"  {label}: handler 返回 {type(resp).__name__}, "
                  f".result 是 {type(getattr(resp, 'result', None)).__name__}, "
                  f"_extract_tokens -> {got}")
            check(f"{label} 取到真实 token 且 input>0",
                  isinstance(got[0], int) and got[0] > 0, True)
            check(f"{label} input/output 均非空", got[1] is not None, True)
        except Exception as e:  # noqa: BLE001
            FAIL += 1
            print(f"  ✗ {label} 异常: {type(e).__name__}: {e}")

print()
print("=" * 74)
print(f"结果: {PASS} PASS / {FAIL} FAIL")
print("=" * 74)
sys.exit(1 if FAIL else 0)
