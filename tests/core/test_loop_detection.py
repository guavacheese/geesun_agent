"""LoopDetectionMiddleware 单元测试：验证两段式（软提醒 + 硬剥 tool_calls）检测逻辑。

覆盖：
1. Layer 1 hash 级：同一工具调用组重复 → 第 3 次软提醒、第 5 次硬剥
2. Layer 2 频次级：同工具名高频（异参）→ 触发频次提醒（不依赖同参）
3. 异参同工具：不误触 Layer 1（hash 不同），但仍可被 Layer 2 捕获
4. read_file 行号分桶：同文件不同行段视为同一意图（防逐行刷屏）
5. 非工具消息：不触发

--- spike-then-verify：核心逻辑先 stub 单测再合入 agent.py ---
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from src.core.loop_detection import (
    _hash_tool_calls,
    _stable_tool_key,
    LoopDetectionMiddleware,
)

# langchain 1.4 的 tool_calls 需要 name/args/id/type 四字段
_RUNTIME = SimpleNamespace(
    execution_info=SimpleNamespace(thread_id="test_thread")
)


def _ai(*tool_calls):
    """构造带 tool_calls 的 AIMessage。接受单个或多个 tool_call dict。"""
    return AIMessage(content="", tool_calls=list(tool_calls))


def _call(name, args):
    """构造一个单工具调用 dict（langchain 1.4 要求 type='tool_call'）。"""
    return {"name": name, "args": args, "id": "call_x", "type": "tool_call"}


def _state_with(msgs):
    return {"messages": msgs}


def test_same_call_repeats_trigger_warning_then_hard_stop():
    """同一工具调用重复 → 第 3 次软提醒，第 5 次硬剥。"""
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    calls = [_call("read_file", {"path": "/reports/u/s/a.txt"})] * 6

    results = []
    for i in range(6):
        state = _state_with([_ai(calls[i])])
        result = mw._apply(state, _RUNTIME)
        results.append(result)

    assert results[0] is None  # 第1次无动作
    assert results[1] is None  # 第2次无动作
    assert results[2] is None  # 第3次软提醒（入队 pending，不动 state）
    assert results[4] is not None  # 第5次硬剥
    assert results[4]["messages"][0].tool_calls == []


def test_different_args_same_tool_caught_by_frequency_layer():
    """同工具异参（逐章节读不同文件）→ Layer 1 不触发，Layer 2 频次触发。"""
    mw = LoopDetectionMiddleware(
        warn_threshold=3, hard_limit=5,
        tool_freq_warn=3, tool_freq_hard_limit=5,
    )
    calls = [_call("read_file", {"path": f"/reports/u/s/ch{i}.txt"}) for i in range(6)]

    results = []
    for i in range(6):
        state = _state_with([_ai(calls[i])])
        result = mw._apply(state, _RUNTIME)
        results.append(result)

    # Layer 1：参数不同 → hash 不同 → 不触发
    assert results[0] is None and results[1] is None
    # Layer 2：第 5 次频次硬剥触发（tool_freq_hard=5）
    assert results[4] is not None
    assert results[4]["messages"][0].tool_calls == []


def test_hash_call_different_args_do_not_match():
    """同工具异参 → hash 不同（验 Layer 1 不误报）。"""
    h1 = _hash_tool_calls([_call("read_file", {"path": "/a"})])
    h2 = _hash_tool_calls([_call("read_file", {"path": "/b"})])
    assert h1 != h2


def test_read_file_line_bucket():
    """read_file 行号分桶：同文件同桶同 key，不同文件不同 key。"""
    # 同文件 1-200 行与 1-200 行 → 同 key（桶0）
    k1 = _stable_tool_key("read_file", {"path": "/a", "start_line": 1, "end_line": 200}, None)
    k2 = _stable_tool_key("read_file", {"path": "/a", "start_line": 1, "end_line": 200}, None)
    assert k1 == k2
    # 不同文件 → 不同 key
    k3 = _stable_tool_key("read_file", {"path": "/b", "start_line": 1, "end_line": 200}, None)
    assert k1 != k3
    # 同文件 201-400 行 → 桶1，与桶0不同（模型在读新内容，不算重复）
    k4 = _stable_tool_key("read_file", {"path": "/a", "start_line": 201, "end_line": 400}, None)
    assert k1 != k4


def test_non_tool_message_no_trigger():
    """纯文本 AIMessage（无 tool_calls）→ 不触发任何动作。"""
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    state = _state_with([AIMessage(content="just text")])
    result = mw._apply(state, _RUNTIME)
    assert result is None
