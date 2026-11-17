"""Spike: _summarization_event LastValue → last-write-wins reducer（F4，2026-11-17）。

故障复现（生产 GY35362:3c4b5588，2026-11-17 15:54-15:57 反复）：
    InvalidUpdateError: At key '_summarization_event': Can receive only one
    value per step
deepagents 的 SummarizationState 用 PrivateStateAttr 注解（langgraph LastValue
channel），同一 super-step 收到两个更新即抛 InvalidUpdateError。触发场景：
crash 轮遗留的 pending writes 在恢复/快照读取时重放，与本次写入撞在同一 step →
_drain_astream 和 _persist_session→aget_state 全炸，线程永久 brick。

修复：自定义 state schema 把 `_summarization_event` 重注解为
Annotated[..., PrivateStateAttr, last-wins-reducer] —— langgraph
_is_field_binop（state.py:1901）识别 meta[-1] 为 2 参数 callable →
BinaryOperatorAggregate channel → 同 step 多个更新逐个 fold 不抛错。
官方支持路径：langchain factory.py:1229「base_state last so it wins any
field conflict. This lets the caller's explicit state_schema override
middleware annotations」。

运行: python tests/spikes/summarization_event_reducer.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated, NotRequired

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langchain.agents.middleware.types import PrivateStateAttr
from langgraph.channels.binop import BinaryOperatorAggregate
from langgraph.channels.last_value import LastValue
from langgraph.graph import START, StateGraph
from langgraph.errors import InvalidUpdateError

from deepagents.graph import DeepAgentState
from deepagents.middleware._state import private_state_field_names
from deepagents.middleware.summarization import (
    SUMMARIZATION_EVENT_KEY,
    SummarizationEvent,
    SummarizationState,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name} {detail}")


# ── 修复实现（与 src/services/agent.py 中一致）─────────────────────────

def _summarization_event_last_wins(old, new):
    """last-write-wins：None 视为「无更新」保留旧值。

    deepagents 写入点（summarization.py:1613/1752/2091）全部写非 None 的
    new_event，从不写 None 清空，故 None 只出现在「从未有过 event」的
    初始 state（BinaryOperatorAggregate 初始为 MISSING → 首值直接落位）。
    """
    return new if new is not None else old


class _AgentStateWithReducer(DeepAgentState):
    """覆盖 _summarization_event 为 last-write-wins（与 agent.py 落地版一致）。"""

    _summarization_event: Annotated[
        NotRequired[SummarizationEvent | None],
        PrivateStateAttr,
        _summarization_event_last_wins,
    ]


# ── 1. channel 类型：LastValue vs BinaryOperatorAggregate ─────────────

def _channel_type(schema: type) -> str:
    graph = StateGraph(schema)
    from langgraph.graph.state import _get_channels

    channels, _, _ = _get_channels(schema)
    ch = channels.get(SUMMARIZATION_EVENT_KEY)
    return type(ch).__name__ if ch is not None else "<missing>"


print("\n[1] channel 类型判定")
check(
    "deepagents 原 SummarizationState → LastValue（故障根源）",
    _channel_type(SummarizationState) == "LastValue",
    f"got {_channel_type(SummarizationState)}",
)
check(
    "reducer 版 schema → BinaryOperatorAggregate（修复生效）",
    _channel_type(_AgentStateWithReducer) == "BinaryOperatorAggregate",
    f"got {_channel_type(_AgentStateWithReducer)}",
)

# ── 2. 端到端：同 step 双写（复现 pending-writes 重放撞车）───────────────


def _make_graph(schema: type):
    g = StateGraph(schema)

    async def writer_a(state):
        return {
            SUMMARIZATION_EVENT_KEY: {
                "cutoff_index": 1,
                "summary_message": None,
                "file_path": "a.txt",
            }
        }

    async def writer_b(state):
        return {
            SUMMARIZATION_EVENT_KEY: {
                "cutoff_index": 2,
                "summary_message": None,
                "file_path": "b.txt",
            }
        }

    g.add_node("a", writer_a)
    g.add_node("b", writer_b)
    g.add_edge(START, "a")
    g.add_edge(START, "b")
    return g.compile()


async def _run_double_write(schema: type) -> tuple[bool, object | str]:
    """两个并行 node 同 step 各写一次 _summarization_event。
    LastValue → InvalidUpdateError；binop → 最后写入者赢。"""
    try:
        result = await _make_graph(schema).ainvoke({"messages": []})
        return True, result.get(SUMMARIZATION_EVENT_KEY)
    except InvalidUpdateError as e:
        return False, str(e)


async def _run_e2e() -> None:
    print("\n[2] 端到端：同 super-step 两个更新（GY35362 故障模式）")

    ok, detail = await _run_double_write(SummarizationState)
    check(
        "原 schema：同 step 双写必抛 InvalidUpdateError（故障复现）",
        (not ok) and "Can receive only one value per step" in detail,
        f"unexpected: ok={ok} detail={detail[:120]}",
    )

    ok, event = await _run_double_write(_AgentStateWithReducer)
    check(
        "reducer schema：双写不抛错",
        ok,
        f"got error: {event}",
    )
    check(
        "reducer schema：最后写入者赢（cutoff_index=2 / file_path=b.txt）",
        ok and event.get("cutoff_index") == 2 and event.get("file_path") == "b.txt",
        f"got {event}",
    )

    # None 防御：fold 中出现 None（防御性场景）→ 保留旧值
    from langgraph.channels.binop import BinaryOperatorAggregate

    ch = BinaryOperatorAggregate(
        NotRequired[SummarizationEvent | None], _summarization_event_last_wins
    )
    ev1 = {"cutoff_index": 5, "summary_message": None, "file_path": None}
    ch.update([ev1, None])
    check("reducer：fold 遇 None 保留旧 event（防误清）", ch.get() is ev1)


asyncio.run(_run_e2e())

# ── 3. PrivateStateAttr 仍被 private_state_field_names 识别 ────────────

print("\n[3] private state key 不受 reducer 注解影响")
# 真实聚合形态（deepagents graph.py:970-972）：
# state_schemas = [自定义 state_schema] + [各 middleware 的 state_schema]
# → private_state_field_names(*state_schemas) 全部传入。
# _summarization_session_id 由 deepagents SummarizationState 提供（我们无需重复声明）。
keys = private_state_field_names(_AgentStateWithReducer, SummarizationState)
check(
    "_summarization_event / _summarization_session_id 仍为 private",
    SUMMARIZATION_EVENT_KEY in keys and "_summarization_session_id" in keys,
    f"got {sorted(keys)}",
)

# ── 4. factory 聚合顺序下覆盖生效（模拟 langchain factory.py:1232）──────

print("\n[4] schema 聚合：middleware 在前、base_state 在后（后者覆盖前者）")
from langchain.agents.factory import _resolve_schemas
from langgraph.graph.state import _get_channels

# 模拟 create_deep_agent(state_schema=_AgentStateWithReducer) 的聚合：
# [*(m.state_schema for m in middleware), base_state]，SummarizationState
# 是 deepagents 默认 middleware 的 schema，自定义 schema 排最后
resolved, _, _ = _resolve_schemas([SummarizationState, _AgentStateWithReducer])
channels, _, _ = _get_channels(resolved)
check(
    "聚合后 _summarization_event 走 BinaryOperatorAggregate",
    isinstance(channels.get(SUMMARIZATION_EVENT_KEY), BinaryOperatorAggregate),
    f"got {type(channels.get(SUMMARIZATION_EVENT_KEY)).__name__}",
)

# ── 总结 ──────────────────────────────────────────────────────────────

print(f"\n{'=' * 60}")
print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    sys.exit(1)
print("ALL OK — F4 spike 验证通过，可将该实现落地 agent.py")
