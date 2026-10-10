"""②A 集成验证：硬停标记真的落 state、真的能被 updates 流读到、真的开轮清零。

为什么要图级验证（不能只测 middleware 单方法）：
    ②A 的价值全在"**API 层能拿到这个信号**"这一条链路上，而链路有三段都可能断：
      ① middleware 自定义 state_schema 是否真被 langchain 合并进 graph state
         （factory.py:1154 `state_schemas = [*(m.state_schema for m in middleware), ...]`）
      ② after_model 节点返回的自定义键是否真随 `stream_mode="updates"` 推出来
         （chat.py 的消费点就在这个流上）
      ③ 下一轮 run 的 before_agent 是否真的把标记清掉（否则会误杀正常轮次）
    只测 `_apply()` 的返回值，这三段一个都没覆盖 —— 典型的"单测全绿但没接上"。

判据：
    1. updates 流里出现 `LoopDetectionMiddleware.after_model` 且其输出含
       loop_forced_stop（reason=repeat_calls / tool_frequency）
    2. run 结束后 checkpoint state 里该键存在（随 checkpoint 走）
    3. 同一 thread 再跑一轮（模型不再重复调用）→ state 里该键被清零为 None

运行：
    docker run --rm -v D:/workspace/geesun_agent:/mnt -w /mnt \
      -e PYTHONPATH=/mnt/.testdeps --entrypoint /app/.venv/bin/python \
      172.16.220.74:8333/geesun_ai/geesun-agent:1.0.22 \
      -m pytest tests/core/test_loop_detection_integration.py -q
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.loop_detection import (  # noqa: E402
    LOOP_FORCED_STOP_KEY,
    STOP_REASON_REPEAT_CALLS,
    LoopDetectionMiddleware,
)

_ids = itertools.count(1)


@tool
def ping(x: int) -> str:
    """永远成功返回的小工具（用于制造「工具全成功但反复调用」的循环）。"""
    return f"pong:{x}"


class _LoopingModel(BaseChatModel):
    """假模型：前 ``repeat`` 次调用都发同一个 tool_call，之后转为纯文本收尾。

    为什么要能收尾：硬停剥空 tool_calls 后图会走向 END，若模型永远发 tool_call，
    硬停反而会被"下一轮模型调用"重新触发 —— 那不是我们要验证的链路。
    """

    repeat: int = 5
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "loop-detection-fake"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    def _generate(  # noqa: ANN001, ANN003
        self, messages, stop=None, run_manager=None, **kwargs
    ) -> ChatResult:
        self.calls += 1
        if self.calls <= self.repeat:
            msg = AIMessage(
                content="",
                tool_calls=[
                    {"name": "ping", "args": {"x": 1}, "id": f"call_{next(_ids)}"}
                ],
            )
        else:
            msg = AIMessage(content="已用现有结果作答。")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _build(mw: LoopDetectionMiddleware):
    """返回 (agent, model)：model 需留给判据 3 改行为（模拟"这轮不再循环"）。"""
    model = _LoopingModel(repeat=8)
    agent = create_agent(
        model=model,
        tools=[ping],
        middleware=[mw],
        checkpointer=InMemorySaver(),
    )
    return agent, model


def test_hard_stop_marker_visible_in_updates_stream_and_reset_next_run():
    """三段链路一次跑通：写入 → updates 可见 → 读取 → 下一轮清零。"""
    mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=3)
    agent, model = _build(mw)
    config = {"configurable": {"thread_id": "t-loop-stop"}, "recursion_limit": 50}

    seen: list[dict[str, Any]] = []
    # 单一 run：反复调 ping 直到硬停（模型 repeat=8，硬停阈值 3）
    for _mode, data in agent.stream(
        {"messages": [HumanMessage("开始")]},
        config=config,
        stream_mode=["updates"],
    ):
        if _mode != "updates":
            continue
        for node_name, node_output in data.items():
            if isinstance(node_output, dict) and node_output.get(LOOP_FORCED_STOP_KEY):
                seen.append({"node": node_name, "marker": node_output[LOOP_FORCED_STOP_KEY]})

    # 判据 1：updates 流里拿到标记，且 node 名与 chat.py 的消费路径一致
    assert seen, (
        "updates 流里没有出现 loop_forced_stop —— chat.py 的消费点永远拿不到信号"
        "（检查 state_schema 是否被 factory 合并）"
    )
    assert seen[0]["node"] == "LoopDetectionMiddleware.after_model", seen[0]["node"]
    assert seen[0]["marker"]["reason"] == STOP_REASON_REPEAT_CALLS
    assert seen[0]["marker"]["count"] >= 3

    # 判据 2：run 结束后标记落在 checkpoint state 上
    state = agent.get_state(config)
    assert state.values.get(LOOP_FORCED_STOP_KEY), (
        "硬停标记没有写进 state —— 说明自定义键被 graph 丢弃"
    )

    # 判据 3：下一轮 run 的 before_agent 清零（防误杀正常轮次）
    #   模型改为直接作答（repeat=0），模拟"这轮本来不循环"：若上一轮标记残留，
    #   chat.py 会把这一轮正常回复也判成硬停轮而提前终止。
    model.repeat = 0
    reset_seen = False
    for _mode, data in agent.stream(
        {"messages": [HumanMessage("再来")]}, config=config, stream_mode=["updates"]
    ):
        if _mode != "updates":
            continue
        for node_name, node_output in data.items():
            if (
                isinstance(node_output, dict)
                and LOOP_FORCED_STOP_KEY in node_output
                and node_output[LOOP_FORCED_STOP_KEY] is None
            ):
                assert node_name == "LoopDetectionMiddleware.before_agent"
                reset_seen = True

    assert reset_seen, "没有观察到开轮清零更新 —— 硬停标记会跨轮残留"
    state2 = agent.get_state(config)
    assert not state2.values.get(LOOP_FORCED_STOP_KEY), (
        "上一轮硬停标记残留在 state 里 —— 会把下一轮正常回复误判成硬停轮而提前终止"
    )


if __name__ == "__main__":  # pragma: no cover - 便于手工单跑
    raise SystemExit(pytest.main([__file__, "-q"]))
