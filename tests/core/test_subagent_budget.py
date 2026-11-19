# -*- coding: utf-8 -*-
"""SubagentBudgetMiddleware 行为测试（D3，2026-11-19）。

判据全部落真实消息形态（langchain 1.4 tool_calls 四字段），不测实现细节：
放行/剥离两个分支 + 剥离后的序列化形状（tool_calls 清空、additional_kwargs
原生字段清掉、finish_reason 改 stop——这套形状是主链硬停被 a2719d7b 事故
验证过的，子代理复用同一构造就必须复验同一形状）。
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.core.subagent_budget import SubagentBudgetMiddleware


def _ai_with_tool_call(name: str = "execute", call_id: str = "t1") -> AIMessage:
    return AIMessage(
        content="继续干活",
        tool_calls=[{"name": name, "args": {"command": "ls"}, "id": call_id, "type": "tool_call"}],
        additional_kwargs={"tool_calls": [{"id": call_id, "function": {"name": name}}]},
        response_metadata={"finish_reason": "tool_calls"},
    )


def _history(n_ai: int, *, last_with_tool_call: bool = True) -> dict:
    """构造 n_ai 条 AIMessage 的子代理消息列表（Human 开局、中间 Tool 穿插）。"""
    messages = [HumanMessage(content="任务描述")]
    for i in range(n_ai):
        if i == n_ai - 1 and not last_with_tool_call:
            messages.append(AIMessage(content="最终报告文本"))
            continue
        messages.append(_ai_with_tool_call(call_id=f"t{i}"))
        # after_model 触发时刻最后一条就是**新** AIMessage（工具还没跑）；
        # 只有中间轮才补 ToolMessage。
        if i < n_ai - 1:
            messages.append(ToolMessage(content="ok", tool_call_id=f"t{i}"))
    return {"messages": messages}


class TestBudgetPassThrough:
    def test_below_budget_no_update(self):
        mw = SubagentBudgetMiddleware(max_model_calls=5)
        assert mw.after_model(_history(4), None) is None
        assert mw.after_model(_history(1), None) is None

    def test_no_tool_calls_never_touched(self):
        """已到预算但模型在写文本 → 收尾中，不拦（也不该拦，拦了会破坏配对）。"""
        mw = SubagentBudgetMiddleware(max_model_calls=3)
        assert mw.after_model(_history(3, last_with_tool_call=False), None) is None

    def test_empty_state(self):
        mw = SubagentBudgetMiddleware(max_model_calls=3)
        assert mw.after_model({"messages": []}, None) is None
        assert mw.after_model({}, None) is None


class TestBudgetHardStop:
    def test_at_budget_strips_tool_calls(self):
        mw = SubagentBudgetMiddleware(max_model_calls=5)
        state = _history(5)
        update = mw.after_model(state, None)
        assert update is not None
        stripped = update["messages"][0]
        assert stripped.tool_calls == []
        assert "模型调用上限" in stripped.content
        # 原文保留（追加式），报告里模型自己说过的话不能丢
        assert "继续干活" in stripped.content

    def test_strip_shape_matches_main_chain_hard_stop(self):
        """剥离形状：additional_kwargs 原生 tool_calls 清掉、finish_reason 改 stop。

        子代理报告要序列化成纯 assistant 文本回传给主代理，形状不对会让
        ChatML 校验器在后续轮炸配对（主链同款事故的形状判据）。
        """
        mw = SubagentBudgetMiddleware(max_model_calls=2)
        state = _history(2)
        stripped = mw.after_model(state, None)["messages"][0]
        assert "tool_calls" not in stripped.additional_kwargs
        assert stripped.response_metadata.get("finish_reason") == "stop"

    def test_above_budget_also_strips(self):
        """AIMessage 计数只增不减，越过 max 后每一轮继续拦（幂等）。"""
        mw = SubagentBudgetMiddleware(max_model_calls=3)
        assert mw.after_model(_history(7), None) is not None

    def test_human_message_count_not_included(self):
        """预算数的是模型调用（AIMessage），Human/Tool 消息不占额度。"""
        mw = SubagentBudgetMiddleware(max_model_calls=2)
        # 2 条 AI（=2 次模型调用）→ 到量
        assert mw.after_model(_history(2), None) is not None
        mw2 = SubagentBudgetMiddleware(max_model_calls=3)
        # 同样形态但 max=3：2 次模型调用 < 3 → 放行（Human/Tool 不占额度的
        # 直接推论：_history(2) 实际含 1 Human + 2 AI + 2 Tool 共 5 条消息）。
        assert mw2.after_model(_history(2), None) is None


class TestAsyncParity:
    def test_aafter_model_same_behavior(self):
        mw = SubagentBudgetMiddleware(max_model_calls=2)
        update = asyncio.run(mw.aafter_model(_history(2), None))
        assert update is not None and update["messages"][0].tool_calls == []
        assert asyncio.run(mw.aafter_model(_history(1), None)) is None


class TestConfigGuard:
    def test_invalid_budget_rejected_at_construction(self):
        """0/负数启动即报错——静默 <1 的预算等于剥光子代理一切工具。"""
        import pytest

        with pytest.raises(ValueError):
            SubagentBudgetMiddleware(max_model_calls=0)
        with pytest.raises(ValueError):
            SubagentBudgetMiddleware(max_model_calls=-5)
