"""Spike: monkey-patch deepagents 0.6.12 create_summarization_middleware（动作二，2026-11-17）。

背景（生产 1.0.49 止血后的根治）：
    deepagents 0.6.12 graph.py:801 主链无条件创建默认 SummarizationMiddleware
    （近似计数 + 默认 trigger=("fraction",0.85)）；user middleware 再传必然双实例
    （GY35362 pending-writes 双写 brick）；同名替换依赖 0.7+ 的
    _apply_custom_middleware，0.6.12 无此函数（1.0.48 生产 500）。
    修复：patch deepagents.graph.create_summarization_middleware，在默认实例创建位
    产出 _SweepThenSummarize(budget sweep → _SummarizationAccurate) 组合件。

断言（12 项）：
    [1] patch 生效：graph.create_summarization_middleware 指向 _patched_*
    [2] ctx 设定 + 主 model → 组合件；内层 _SummarizationAccurate 配置精确
        （trigger=0.6 / keep=0.10 / token_counter=引擎计数器 / partial=安全计数器）
    [3] 组合件 state_schema=SummarizationState（graph.py:841 private_state 收集依赖）
    [4] ctx 未设 → 原默认实例透传（进程内其它调用点行为不变）
    [5] ctx 设定但 model 不是主 model → 原默认实例透传
    [6] 原工厂未被调用（主 model 命中时）——默认实例不再产生
    [7] 线程并发：两线程各自 set 不同 trigger → 各自命中（ContextVar 隔离）
    [8] 真实 create_deep_agent 构建（ctx 设定 + GeesunAgentState + budget）不炸
        ——1.0.48 的 duplicate-name 500 只在真实 factory 断言触发，桩验不出
    [9] 构建产物 invoke 一轮完成（组合件 wrap_model_call 全链跑通）
    [10] 构建期间 _SummarizationAccurate 构造次数 ≥1（主链 + gp subagent 槽位）
    [11] budget 实例共享：组合件与 user middleware 列表是同一实例（幂等 sweep 前提）
    [12] 版本闸门：0.6.12 环境下 import 通过（RuntimeError 只在非 0.6.x 触发）

运行: python tests/spikes/summarization_patch_0612.py
（必须跑在与生产一致的 deepagents 0.6.12 venv 下；1.0.48 教训：只在 0.7.24
 venv 验证过的新代码进生产直接全量 500。）
"""

from __future__ import annotations

import itertools
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Settings 必填字段兜底（import src.services.agent 前必需）
import os

for _k, _v in (
    ("BASE_URL", "http://fake:8003/v1"),
    ("OPENAI_API_KEY", "spike-key"),
    ("MODEL_NAME", "Qwen3.6-35B-A3B"),
    ("AGENT_WORKSPACE", "./.spike-workspace"),
):
    os.environ.setdefault(_k, _v)

import deepagents.graph as dg
from deepagents.backends import StateBackend
from deepagents.middleware.summarization import (
    SummarizationState,
    _DeepAgentsSummarizationMiddleware,
)
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

import src.services.agent as agent_mod

PASS: list[str] = []
FAIL: list[str] = []


def check(idx: int, name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(f"[{idx}] {name}")
    else:
        FAIL.append(f"[{idx}] {name} {detail}")


def _make_fake_model() -> GenericFakeChatModel:
    def _gen():
        while True:
            yield AIMessage(content="ok")

    # profile 必须给：fraction trigger/keep 无 profile 时 langchain __init__ 直接
    # ValueError（summarization.py:367）。生产由 switch_model/resolve_max_len 注入，
    # spike 用与窗口一致的 262144。
    return _BindToolsFakeChatModel(
        messages=_gen(), profile={"max_input_tokens": 262144}
    )


class _BindToolsFakeChatModel(GenericFakeChatModel):
    """factory._get_bound_model 每轮调 model.bind_tools（空 tools 也调），
    GenericFakeChatModel 未实现 → NotImplementedError；返回 self 即可。"""

    def bind_tools(self, tools, **kwargs):
        return self


def _counters_stub():
    """模拟 create_agent 闭包：引擎计数器 + 安全计数器（用假 model 可安全调用）。"""
    model = _make_fake_model()

    def _safe_token_counter(messages, *, tools=None) -> int:
        total = 0
        for m in messages:
            content = str(getattr(m, "content", "")) if getattr(m, "content", None) else ""
            if not content:
                continue
            try:
                total += model.get_num_tokens(content)
            except Exception:
                total += len(content)
        return total

    def _engine_grounded_counter(messages, *, tools=None) -> int:
        return _safe_token_counter(messages, tools=tools)

    return model, _safe_token_counter, _engine_grounded_counter


# ─────────────────────────── [1] patch 生效 ───────────────────────────
check(1, "patch 生效", dg.create_summarization_middleware is agent_mod._patched_create_summarization_middleware)

model, safe_counter, engine_counter = _counters_stub()
backend = StateBackend()
budget_mw = agent_mod.ToolOutputBudgetMiddleware(
    backend=backend, enabled=True, externalize_min_chars=4096, fallback_max_chars=100_000
)

kwargs = dict(
    trigger=("fraction", 0.6),
    keep=("fraction", 0.10),
    token_counter=engine_counter,
    partial_token_counter=safe_counter,
    inventory_provider=lambda: "清单",
)

# ─────────────── [2][3][6] ctx 设定 + 主 model → 组合件 ───────────────
_orig_calls: list[tuple] = []
_orig_fn = agent_mod._ORIGINAL_CREATE_SUMMARIZATION_MW


def _recording_orig(m, b, **kw):
    _orig_calls.append((id(m),))
    return _orig_fn(m, b, **kw)


agent_mod._ORIGINAL_CREATE_SUMMARIZATION_MW = _recording_orig
token = agent_mod._SUMMARY_MW_CTX.set({"model": model, "budget": budget_mw, "kwargs": kwargs})
try:
    mw = dg.create_summarization_middleware(model, backend)
finally:
    agent_mod._ORIGINAL_CREATE_SUMMARIZATION_MW = _orig_fn
    agent_mod._SUMMARY_MW_CTX.reset(token)

check(2, "组合件类型+内层配置", isinstance(mw, agent_mod._SweepThenSummarize)
      and type(mw._sum) is agent_mod._SummarizationAccurate
      and mw._sum._lc_helper._trigger_clauses == [{"fraction": 0.6}]
      and mw._sum._lc_helper.keep == ("fraction", 0.10)
      and mw._sum._lc_helper.token_counter is engine_counter
      and mw._sum._lc_helper._partial_token_counter is safe_counter,
      f"type={type(mw).__name__} helper={type(mw._sum._lc_helper).__name__}")
check(3, "组合件 state_schema=SummarizationState", mw.state_schema is SummarizationState,
      f"got={getattr(mw.state_schema, '__name__', mw.state_schema)}")
check(6, "主 model 命中时原工厂未被调用", _orig_calls == [], f"orig_calls={_orig_calls}")

# ─────────────────── [4] ctx 未设 → 默认透传 ───────────────────
mw_default = dg.create_summarization_middleware(_make_fake_model(), StateBackend())
check(4, "ctx 未设 → 默认实例", isinstance(mw_default, _DeepAgentsSummarizationMiddleware),
      f"got={type(mw_default).__name__}")

# ──────────────── [5] ctx 设定但 model 不同 → 透传 ────────────────
token = agent_mod._SUMMARY_MW_CTX.set({"model": model, "budget": budget_mw, "kwargs": kwargs})
try:
    mw_other = dg.create_summarization_middleware(_make_fake_model(), StateBackend())
finally:
    agent_mod._SUMMARY_MW_CTX.reset(token)
check(5, "model 不匹配 → 默认实例", isinstance(mw_other, _DeepAgentsSummarizationMiddleware),
      f"got={type(mw_other).__name__}")

# ──────────────── [7] 线程并发 ContextVar 隔离 ────────────────
results: dict[int, tuple[str, tuple]] = {}


def _worker(tag: int, trigger: tuple):
    m = _make_fake_model()
    b = StateBackend()
    kw = dict(kwargs)
    kw["trigger"] = trigger
    tok = agent_mod._SUMMARY_MW_CTX.set({"model": m, "budget": budget_mw, "kwargs": kw})
    try:
        got = dg.create_summarization_middleware(m, b)
        inner = got._sum if isinstance(got, agent_mod._SweepThenSummarize) else got
        results[tag] = (type(inner).__name__, tuple(inner._lc_helper._trigger_clauses))
    finally:
        agent_mod._SUMMARY_MW_CTX.reset(tok)


t1 = threading.Thread(target=_worker, args=(1, ("fraction", 0.6)))
t2 = threading.Thread(target=_worker, args=(2, ("fraction", 0.85)))
t1.start(); t2.start(); t1.join(); t2.join()
check(7, "线程并发 ContextVar 隔离",
      results.get(1) == ("_SummarizationAccurate", ({"fraction": 0.6},))
      and results.get(2) == ("_SummarizationAccurate", ({"fraction": 0.85},)),
      f"results={results}")

# ─────────── [8][9][10][11] 真实 create_deep_agent 构建 + invoke ───────────
constructed: list[agent_mod._SummarizationAccurate] = []
_real_cls = agent_mod._SummarizationAccurate


class _CountingAccurate(_real_cls):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        constructed.append(self)


agent_mod._SummarizationAccurate = _CountingAccurate
try:
    # ⚠ 必须 set ctx：模拟 create_agent 的真实调用序（ctx.set → create_deep_agent → reset），
    # 否则 patched 透传原工厂，构建的是默认实例而非组合件
    build_token = agent_mod._SUMMARY_MW_CTX.set(
        {"model": model, "budget": budget_mw, "kwargs": kwargs}
    )
    try:
        built_agent = dg.create_deep_agent(
            model=model,
            tools=[],
            backend=backend,
            system_prompt="spike",
            middleware=[budget_mw],
            state_schema=agent_mod.GeesunAgentState,
        )
    finally:
        agent_mod._SUMMARY_MW_CTX.reset(build_token)
    check(8, "真实 create_deep_agent 构建不炸（无 duplicate-name 500）", True)
    check(10, "_SummarizationAccurate 构造 ≥1（主链+gp 槽位）", len(constructed) >= 1,
          f"n={len(constructed)}")
finally:
    agent_mod._SummarizationAccurate = _real_cls

check(11, "组合件与 user middleware 共享同一 budget 实例",
      all(c is not None for c in constructed) and mw._sweep is budget_mw)

out = built_agent.invoke({"messages": [("user", "你好")]})
last = out["messages"][-1]
check(9, "invoke 一轮完成（组合件全链跑通）",
      getattr(last, "content", None) == "ok", f"last={type(last).__name__}: {last}")

# ─────────────────────────── [12] 版本闸门 ───────────────────────────
import importlib.metadata as _im

ver = _im.version("deepagents")
check(12, "版本闸门（0.6.12 通过）", ver.startswith("0.6."),
      f"deepagents={ver}")

print()
for line in PASS:
    print("  PASS", line)
for line in FAIL:
    print("  FAIL", line)
print(f"\n{'=' * 60}\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
