"""生产装配路径验证：create_deep_agent 是否保留 LoopDetectionMiddleware 的 state 扩展。

为什么必须单独验这一条：
    ②A 依赖「middleware 自定义的 ``loop_forced_stop`` 能进 graph state，并随
    ``stream_mode="updates"`` 推出」。上面的集成测试用的是 **langchain 原生
    create_agent**，而生产走的是 **create_deep_agent**（src/services/agent.py:950）。
    deepagents 会自己追加一串 middleware（TodoList / Filesystem / Skills /
    Summarization / Anthropic caching…），还带 private_state_field_names 之类的
    state 处理 —— 它完全可能用自己的 state schema 覆盖/过滤掉我们的键。
    只验原生路径 = 验证了不是生产实际跑的那条路。

判据：
    1. 编译图的 channels 里存在 loop_forced_stop
    2. 真实跑一轮触发硬停，updates 流里能拿到该标记（值为 dict）
    3. 下一轮 before_agent 把它清零

运行：
    docker run --rm -v D:/workspace/geesun_agent:/mnt -w /mnt \
      -e PYTHONPATH=/mnt/.testdeps --entrypoint /app/.venv/bin/python \
      172.16.220.74:8333/geesun_ai/geesun-agent:1.0.22 \
      tests/spikes/deep_agent_loop_marker.py
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from src.core.loop_detection import LOOP_FORCED_STOP_KEY, LoopDetectionMiddleware

_ids = itertools.count(1)
FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


@tool
def ping(x: int) -> str:
    """永远成功返回的小工具。"""
    return f"pong:{x}"


class _LoopingModel(BaseChatModel):
    repeat: int = 8
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "deep-loop-fake"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ANN001, ANN003
        self.calls += 1
        if self.calls <= self.repeat:
            msg = AIMessage(
                content="",
                tool_calls=[{"name": "ping", "args": {"x": 1}, "id": f"call_{next(_ids)}"}],
            )
        else:
            msg = AIMessage(content="已用现有结果作答。")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def main() -> int:
    from deepagents import create_deep_agent

    mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=3)
    model = _LoopingModel()
    agent = create_deep_agent(
        model=model,
        tools=[ping],
        middleware=[mw],
        system_prompt="你是测试用 agent。",
        checkpointer=InMemorySaver(),
    )

    print("=== 1) 编译图是否保留自定义 state 键 ===")
    channels = getattr(agent, "channels", {}) or {}
    check(
        f"graph.channels 含 {LOOP_FORCED_STOP_KEY}",
        LOOP_FORCED_STOP_KEY in channels,
        f"channels 数={len(channels)}",
    )

    print("=== 2) 真实跑到硬停，看 updates 流能否拿到标记 ===")
    config = {"configurable": {"thread_id": "deep-loop-1"}, "recursion_limit": 60}
    seen: list[dict] = []
    for _mode, data in agent.stream(
        {"messages": [HumanMessage("开始")]}, config=config, stream_mode=["updates"]
    ):
        if _mode != "updates":
            continue
        for _node, out in data.items():
            if isinstance(out, dict) and out.get(LOOP_FORCED_STOP_KEY):
                seen.append(out[LOOP_FORCED_STOP_KEY])
    check("updates 流里拿到 loop_forced_stop", bool(seen), f"命中 {len(seen)} 次")
    if seen:
        print(f"       marker = {seen[0]}")

    print("=== 3) 下一轮是否清零 ===")
    state = agent.get_state(config)
    check("run 结束后 state 里有标记", bool(state.values.get(LOOP_FORCED_STOP_KEY)))

    model.repeat = 0  # 下一轮不再循环
    cleared = False
    for _mode, data in agent.stream(
        {"messages": [HumanMessage("再来")]}, config=config, stream_mode=["updates"]
    ):
        if _mode != "updates":
            continue
        for node, out in data.items():
            if (
                isinstance(out, dict)
                and LOOP_FORCED_STOP_KEY in out
                and out[LOOP_FORCED_STOP_KEY] is None
            ):
                cleared = True
                print(f"       清零节点 = {node}")
    check("开轮观察到清零更新", cleared)
    check(
        "第二轮结束后 state 无残留标记",
        not agent.get_state(config).values.get(LOOP_FORCED_STOP_KEY),
    )

    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败: {FAILS}")
        return 1
    print("✅ 生产装配路径（create_deep_agent）三段链路全通")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
