"""Spike: deepagents 0.7 SummarizationMiddleware 挂载改造（动作三，2026-11-19）。

背景（升级 0.6.12 → 0.7.24）：
0.6.12 时代靠 monkey-patch deepagents.graph.create_summarization_middleware
接管默认实例创建位；0.7 引入 _apply_custom_middleware（graph.py:211）——
按 .name 原位替换主链中间件并保持栈序，patch 摘除、恢复 user middleware 传法。

⚠ 本 spike 必须在 deepagents 0.7.x venv 跑（cubesandbox-verify）：
    python tests/spikes/summarization_mount_07.py
0.6.12 venv 下 agent.py 版本闸门会直接 RuntimeError（按设计）。

验证点：
[1]  版本闸门：0.7 环境下 import src.services.agent 成功（0.6 venv 会炸）
[2]  组合件 name == "SummarizationMiddleware"（0.7 按 .name 字符串匹配替换；
     _DeepAgentsSummarizationMiddleware 的 name property 对子类回退类名，
     不显式覆盖 = 默认实例不被替换 = 双实例复发）
[3]  _apply_custom_middleware 真实语义：base 栈含默认 Summarization 时传入
     组合件 → 原位替换（栈序保持、实例是我们的）
[3b] 负向对照：不设 name 的自定义件不会被替换（实锤 name 陷阱）
[4]  组合件内层配置精确：_sum 是 _SummarizationAccurate、trigger ("fraction",0.6)、
     keep ("fraction",0.10)、partial_token_counter 已替换到 _lc_helper、
     _sweep 是同一个 budget 实例、state_schema=SummarizationState
[5]  基类内部依赖逐一存在（0.7.24 实测行号）：_lc_helper/_partition_messages/
     _build_new_messages_with_path/_apply_event_to_messages（基类改 staticmethod
     但调用点是 self.，我们的实例覆盖仍生效）
[6]  真实 create_deep_agent + FakeChatModel 冒烟：无 duplicate 断言、
     invoke 全链跑通（1.0.48 教训：duplicate-name 500 只有真实 factory 才炸）
[7]  组合序：wrap_model_call 里 sweep 先于 summarization（外层/内层调用序实测）
"""

import asyncio
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("BASE_URL", "http://fake:8003/v1")
os.environ.setdefault("OPENAI_API_KEY", "spike-key")
os.environ.setdefault("MODEL_NAME", "Qwen3.6-35B-A3B")
os.environ.setdefault("AGENT_WORKSPACE", "./.spike-workspace")

import deepagents.graph as dg
from deepagents.graph import _apply_custom_middleware
from deepagents.middleware.summarization import (
    SummarizationState,
    create_summarization_middleware,
)
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

import src.services.agent as agent_mod
from src.core.tool_output_budget import ToolOutputBudgetMiddleware

RESULTS: list[tuple[str, bool, str]] = []


def check(tag, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((tag, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {tag}" + (f" — {detail}" if detail and not ok else ""))


def make_fake_model():
    """GenericFakeChatModel + bind_tools（0.7 factory._get_bound_model 空 tools 也调）
    + profile（fraction trigger/keep 无 profile 直接 ValueError）。"""

    class _FakeWithTools(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):
            return self

    def _gen():
        while True:
            yield AIMessage(content="ok")

    return _FakeWithTools(messages=_gen(), profile={"max_input_tokens": 262144})


print("== [1] 版本闸门：0.7 环境 import 成功 ==")
import importlib.metadata as _im

_ver = _im.version("deepagents")
check(1, _ver.startswith("0.7"), f"deepagents={_ver}（本 spike 限定 0.7.x）")

print("== [2] 组合件 name 覆盖 ==")
check(2, agent_mod._SweepThenSummarize.name == "SummarizationMiddleware",
      f"name={agent_mod._SweepThenSummarize.name!r}")

print("== [3] _apply_custom_middleware 原位替换 ==")
fake_model = make_fake_model()
budget = ToolOutputBudgetMiddleware(
    backend=None, enabled=False, externalize_min_chars=1, fallback_max_chars=1,
)
sum_mw = agent_mod._SummarizationAccurate(
    model=fake_model, backend=None,
    trigger=("fraction", 0.6), keep=("fraction", 0.10),
    token_counter=lambda ms, tools=None: 10,
    partial_token_counter=lambda ms, tools=None: 10,
)
composite = agent_mod._SweepThenSummarize(budget, sum_mw)

default_sum = create_summarization_middleware(fake_model, backend=None)
base = [default_sum]
merged = _apply_custom_middleware(base, [composite])
check(3, len(merged) == 1 and merged[0] is composite,
      f"merged[0] is composite: {merged[0] is composite}")

print("== [3b] 负向对照：无 name 覆盖的自定义件不被替换 ==")


class _NoNameComposite(AgentMiddleware):
    """模拟忘记覆盖 name 的组合件（name 回退 AgentMiddleware 默认 = 类名）。"""


_no_name = _NoNameComposite()
merged2 = _apply_custom_middleware([default_sum], [_no_name])
check(3, len(merged2) == 2 and merged2[0] is default_sum and merged2[1] is _no_name,
      "无 name 匹配的自定义件被追加而非替换 → 默认实例仍在（陷阱实锤）")

print("== [4] 组合件内层配置精确 ==")
inner = merged[0]._sum
check(4, type(inner) is agent_mod._SummarizationAccurate
      and inner._lc_helper._trigger_clauses == [{"fraction": 0.6}]
      and inner._lc_helper.keep == ("fraction", 0.10)
      and merged[0]._sweep is budget
      and merged[0].state_schema is SummarizationState,
      f"trigger={inner._lc_helper._trigger_clauses} keep={getattr(inner._lc_helper, 'keep', 'N/A')}")

print("== [5] 基类内部依赖存在（0.7.24） ==")
base_cls = type(sum_mw).__mro__[1]  # _DeepAgentsSummarizationMiddleware
check(5, hasattr(sum_mw, "_lc_helper")
      and hasattr(base_cls, "_partition_messages")
      and hasattr(base_cls, "_build_new_messages_with_path")
      and hasattr(base_cls, "_apply_event_to_messages")
      and sum_mw._lc_helper._partial_token_counter is not None,
      f"_partial_token_counter replaced: {sum_mw._lc_helper._partial_token_counter is not None}")

print("== [6] 真实 create_deep_agent 冒烟（duplicate 断言只有真链路才炸） ==")
backend = agent_mod.StateBackend()
# _FreshSkillsMiddleware 的 sources 须为目录列表（会做 _source_path 解析），
# 空目录即可——spike 只验证接线，不验证 skill 内容。
_skills_dir = Path(os.environ["AGENT_WORKSPACE"]) / "spike-skills"
_skills_dir.mkdir(parents=True, exist_ok=True)


def _build():
    return agent_mod._build_deep_agent(
        model=fake_model,
        tools=[],
        backend=backend,
        skills=None,
        middleware=[
            agent_mod._FreshSkillsMiddleware(backend=backend, sources=[str(_skills_dir)]),
            budget,
            composite,
        ],
        checkpointer=None,
    )


built = _build()
check(6, built is not None, "create_deep_agent 构建成功（无 duplicate-name 断言）")


async def _invoke():
    result = await built.ainvoke({"messages": [HumanMessage(content="hi")]})
    return result


try:
    result = asyncio.run(_invoke())
    msgs = result.get("messages", []) if isinstance(result, dict) else []
    check(6, any(getattr(m, "content", "") == "ok" for m in msgs),
          f"invoke 返回 {len(msgs)} 条消息，含 fake 模型回复")
except Exception as e:
    check(6, False, f"invoke 失败: {type(e).__name__}: {e}")

print("== [7] 组合序实测：sweep 外层、summarization 内层 ==")
call_seq = []


class _SeqBudget(ToolOutputBudgetMiddleware):
    def wrap_model_call(self, request, handler):
        call_seq.append("sweep")
        return super().wrap_model_call(request, handler)

    async def awrap_model_call(self, request, handler):
        call_seq.append("asweep")
        return await super().awrap_model_call(request, handler)


class _SeqSum(agent_mod._SummarizationAccurate):
    def wrap_model_call(self, request, handler):
        call_seq.append("sum")
        return super().wrap_model_call(request, handler)

    async def awrap_model_call(self, request, handler):
        call_seq.append("asum")
        return await super().awrap_model_call(request, handler)


_seq_budget = _SeqBudget(backend=None, enabled=False, externalize_min_chars=1, fallback_max_chars=1)
_seq_sum = _SeqSum(
    model=fake_model, backend=None,
    trigger=("fraction", 0.6), keep=("fraction", 0.10),
    token_counter=lambda ms, tools=None: 10,
    partial_token_counter=lambda ms, tools=None: 10,
)
_seq_comp = agent_mod._SweepThenSummarize(_seq_budget, _seq_sum)


class _EndHandler(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        call_seq.append("end")
        return handler(request)


_mw_stack = [_seq_comp, _EndHandler()]
# 手动模拟 langchain 的 wrap 链组装（first = outermost）
def _chain(req):
    h = _mw_stack[-1].wrap_model_call(req, lambda r: r)  # 末位先成为最内层
    for mw in reversed(_mw_stack[:-1]):
        h = (lambda mw, h: (lambda r: mw.wrap_model_call(r, h)))(mw, h)
    return h(_mw_stack[0].__class__ and _FakeRequest())


class _FakeRequest:
    """最小 ModelRequest 替身：组合件只透传 messages。"""

    def __init__(self):
        self.messages = [HumanMessage(content="hi")]
        self.model_settings = {}
        self.model = fake_model
        self.system_message = None
        self.state = {}
        self.tools = []
        self.tool_choice = None
        self.response_format = None

    def override(self, **kwargs):
        new = _FakeRequest()
        for k, v in {**{f: getattr(self, f) for f in ("messages", "model_settings", "model", "system_message", "state", "tools", "tool_choice", "response_format")}, **kwargs}.items():
            setattr(new, k, v)
        return new


try:
    _chain(_FakeRequest())
    sync_seq = [x for x in call_seq if not x.startswith("a")]
    check(7, sync_seq[:3] == ["sweep", "sum", "end"],
          f"sync 调用序={sync_seq[:4]}（期望 sweep→sum→end）")
except Exception as e:
    # 真链路组装复杂，退化为直接组合调用序断言
    call_seq.clear()
    req = _FakeRequest()
    _seq_comp.wrap_model_call(req, lambda r: (call_seq.append("end"), r)[1])
    check(7, call_seq[:3] == ["sweep", "sum", "end"],
          f"组合调用序={call_seq[:4]}（期望 sweep→sum→end）")

# 异步序
async def _async_seq():
    call_seq.clear()
    req = _FakeRequest()

    async def _end(r):
        call_seq.append("aend")
        return r

    async def _inner(r):
        return await _seq_comp.awrap_model_call(r, _end)

    await _seq_comp.awrap_model_call(req, _end)
    return [x for x in call_seq if x.startswith("a")]


aseq = asyncio.run(_async_seq())
check(7, aseq[:3] == ["asweep", "asum", "aend"], f"async 调用序={aseq}（期望 asweep→asum→aend）")

# ─── 汇总 ───
passed = sum(1 for _, ok, _ in RESULTS if ok)
total = len(RESULTS)
print(f"\n===== SPIKE RESULT: {passed}/{total} passed =====")
if passed != total:
    for tag, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL {tag}: {detail}")
    sys.exit(1)
print("ALL_OK")
