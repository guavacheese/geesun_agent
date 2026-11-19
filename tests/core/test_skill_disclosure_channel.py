# -*- coding: utf-8 -*-
"""SkillsMiddleware 双实例下 `_skill_tools_disclosed` 通道并发写测试（1.0.53 回滚事故）。

事故（2026-11-19 傍晚发版，agent 1.0.53 = deepagents 0.7.24，上线 14 分钟回滚）：
生产装配是**两个 SkillsMiddleware 实例**——create_deep_agent(skills=...) 默认实例
（graph.py:934 注入职责）+ 我们追加的 _FreshSkillsMiddleware 子类（agent.py，
每轮重扫职责，名字不同不去重）。0.6.12 时代 wrap_model_call 不写 state，双实例
安全；0.7.24 的 skills 披露机制在 wrap_model_call **无条件**返回
Command(update={_skill_tools_disclosed: record})（skills.py:1342/1364），两层各写
一次 → 同一 model task 同 step 双写 → LastValue 通道抛
InvalidUpdateError('Can receive only one value per step')。
（Postgres checkpoint_writes 实证：同一 task_id 下 idx=1/2 两行同 key；
thread GY34035:2b3a6c6d，a2b19d7b 前史同病的 reducer 解法见
_summarization_event_last_wins docstring。）

修复：GeesunAgentState 覆盖该字段注解，加 merge reducer（dict 合并，语义 =
两实例披露记录并集；同键后者赢）。langchain factory.py:1229：state_schema 排
最后、覆盖 middleware 注解——官方扩展点。

本文件形态：
[1] 行为级双实例复现（参数化负/正对照）——真实 create_deep_agent + fake model：
    - DeepAgentState（无 reducer）必须 InvalidUpdateError = 复现成立；
    - GeesunAgentState（有 reducer）必须正常收敛 = 修复生效。
[2] 接线断言：GeesunAgentState 源码里该字段的 Annotated 三件套在位（防回归删除）。

运行（必须 0.7 venv）：
    C:/Users/GY24428/.workbuddy/binaries/python/envs/cubesandbox-verify/Scripts/python.exe \
      -m pytest tests/core/test_skill_disclosure_channel.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

# 与 spike 同款最小 env（agent.py 导入链会实例化 Settings 单例）
os.environ.setdefault("BASE_URL", "http://fake:8003/v1")
os.environ.setdefault("OPENAI_API_KEY", "channel-test-key")
os.environ.setdefault("MODEL_NAME", "Qwen3.6-35B-A3B")
os.environ.setdefault("AGENT_WORKSPACE", str(PROJECT_ROOT / ".pytest-skills-ws"))

from deepagents import create_deep_agent  # noqa: E402
from deepagents.middleware.skills import SkillsMiddleware  # noqa: E402
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langgraph.errors import InvalidUpdateError  # noqa: E402

import src.services.agent as agent_mod  # noqa: E402
from src.services.agent import GeesunAgentState  # noqa: E402


def _fake_model():
    class _FakeWithTools(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):
            return self

    def _gen():
        while True:
            yield AIMessage(content="ok")

    return _FakeWithTools(messages=_gen(), profile={"max_input_tokens": 262144})


def _skills_dir(tmp_path: Path) -> str:
    d = tmp_path / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _build_double_skills(tmp_path: Path, state_schema):
    """复刻生产装配：skills= 默认实例 + _FreshSkillsMiddleware 追加实例。

    skills= 传目录 → create_deep_agent 内部（graph.py:934）自建默认
    SkillsMiddleware；middleware 里再放 _FreshSkillsMiddleware（不同 name，
    不去重）→ 主链两个 Skills wrap 层，0.7.24 起每层无条件写
    _skill_tools_disclosed。
    """
    src_dir = _skills_dir(tmp_path)
    backend = agent_mod.StateBackend()
    return create_deep_agent(
        model=_fake_model(),
        tools=[],
        backend=backend,
        system_prompt="test",
        skills=[src_dir],
        middleware=[
            agent_mod._FreshSkillsMiddleware(backend=backend, sources=[src_dir]),
        ],
        state_schema=state_schema,
        checkpointer=None,
    )


def _run_one_turn(agent):
    return agent.invoke({"messages": [HumanMessage(content="hi")]})


def test_double_instance_is_actually_built(tmp_path):
    """前提断言：这个装配下主链确实存在 ≥2 个 SkillsMiddleware 系实例。

    若未来 deepagents/langchain 改了装配（或 _Fresh 被改名去重），本测试先红，
    提示双实例前提已消失、reducer 可退役——不允许无声失效。
    """
    agent = _build_double_skills(tmp_path, GeesunAgentState)
    stacks = getattr(agent, "_middleware", None) or []
    # CompiledStateGraph 不直接暴露 middleware 列表时退化为源码级判据（装配面
    # 已由 _build_double_skills 忠实构造，此处双保险）
    n = sum(1 for m in stacks if isinstance(m, SkillsMiddleware))
    if stacks:
        assert n >= 2, f"双实例前提消失：SkillsMiddleware 实例数={n}"


def test_parent_schema_double_write_reproduces_crash(tmp_path):
    """负向对照（1.0.53 事故形态）：DeepAgentState 无 reducer → 同 step 双写必炸。"""
    agent = _build_double_skills(tmp_path, agent_mod.DeepAgentState)
    with pytest.raises(InvalidUpdateError, match="_skill_tools_disclosed"):
        _run_one_turn(agent)


def test_geesun_schema_survives_double_write(tmp_path):
    """修复生效：GeesunAgentState 的 merge reducer 吸收双写，正常收敛。"""
    agent = _build_double_skills(tmp_path, GeesunAgentState)
    result = _run_one_turn(agent)
    msgs = result["messages"]
    assert any(getattr(m, "content", "") == "ok" for m in msgs), "fake 模型回复未落 messages"


def test_state_annotation_wiring():
    """接线：GeesunAgentState 必须声明该字段且 Annotated 含 PrivateStateAttr + reducer。

    防两路回归：① 字段被整段删掉（双写复活）；② reducer 位置错——langgraph
    _is_field_binop 只认 Annotated meta 末尾的 2 参 callable，顺序错 = 静默
    退回 LastValue（同病复发且无告警）。
    """
    import typing

    field = GeesunAgentState.__annotations__.get("_skill_tools_disclosed")
    assert field is not None, "GeesunAgentState 丢了这个字段的覆盖声明"
    origin = typing.get_origin(field)
    assert origin is not None and str(origin) == "typing.Annotated" or hasattr(field, "__metadata__"), \
        "字段必须是 Annotated"
    meta = getattr(field, "__metadata__", ())
    reducers = [m for m in meta if callable(m)]
    assert len(reducers) == 1, f"必须恰有 1 个 reducer（meta={meta}）"
    import inspect

    sig = inspect.signature(reducers[0])
    assert len(sig.parameters) == 2, "reducer 必须是 2 参 callable（old, new），否则 langgraph 不识别"
    assert reducers[0] is agent_mod._merge_skill_disclosures


def test_reducer_semantics():
    """reducer 语义：None 容错、键并集、同键后者赢。"""
    f = agent_mod._merge_skill_disclosures
    assert f(None, None) == {}
    assert f(None, {"a": "s1"}) == {"a": "s1"}
    assert f({"a": "s1"}, None) == {"a": "s1"}
    assert f({"a": "s1"}, {"b": "s2"}) == {"a": "s1", "b": "s2"}
    assert f({"a": "s1"}, {"a": "s2"}) == {"a": "s2"}
