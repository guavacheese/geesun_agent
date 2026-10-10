"""LoopDetectionMiddleware 单元测试：验证两段式（软提醒 + 硬剥 tool_calls）检测逻辑。

覆盖：
1. Layer 1 hash 级：同一工具调用组重复 → 第 3 次软提醒、第 5 次硬剥
2. Layer 2 频次级：同工具名高频（异参）→ 触发频次提醒（不依赖同参）
3. 异参同工具：不误触 Layer 1（hash 不同），但仍可被 Layer 2 捕获
4. 分页读取型工具的行号分桶：同文件不同行段视为同一意图（防逐行刷屏）
5. 非工具消息：不触发
6. ★ 契约反查（2026-10-10）：字段名以工具真实 args_schema 为准，不硬编码
7. ★ 契约漂移告警：args 用 schema 外槽位字段时必须 ERROR（限流一次）

⚠ 测试数据必须用**真实工具契约**（deepagents ReadFileSchema：
  file_path / offset / limit）。旧版本这里照着 deer-flow 的 path/start_line/end_line
  构造假数据 —— 单测全绿只证明"对 deer-flow 契约的实现是对的"，对真实工具契约毫无
  约束力，于是 aaf95335 那个"read_file 指纹恒为 ':0-0' → 必然硬停"的缺陷一路漏到
  生产。移植带来的测试，断言源必须是**真实工具 schema**。

--- spike-then-verify：核心逻辑先 stub 单测再合入 agent.py ---
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from deepagents.middleware.filesystem import (
    EditFileSchema,
    ReadFileSchema,
    WriteFileSchema,
)
from langchain_core.messages import AIMessage

from src.core.loop_detection import (
    _PATH_FIELD_CANDIDATES,
    _SPAN_FIELD_CANDIDATES,
    _START_FIELD_CANDIDATES,
    LOOP_FORCED_STOP_KEY,
    STOP_REASON_REPEAT_CALLS,
    STOP_REASON_TOOL_FREQUENCY,
    LoopDetectionMiddleware,
    _detect_contract_drift,
    _extract_schema_fields,
    _hash_tool_calls,
    _resolve_field,
    _stable_tool_key,
)

# langchain 1.4 的 tool_calls 需要 name/args/id/type 四字段
_RUNTIME = SimpleNamespace(
    execution_info=SimpleNamespace(thread_id="test_thread")
)

# 模拟 langchain BaseTool 的最小形态（契约反查只依赖 name + args_schema）
def _tool(name, schema):
    """构造一个假工具对象，用于喂给契约反查（_extract_schema_fields）。"""
    return SimpleNamespace(name=name, args_schema=schema)


def _ai(*tool_calls):
    """构造带 tool_calls 的 AIMessage。接受单个或多个 tool_call dict。"""
    return AIMessage(content="", tool_calls=list(tool_calls))


def _call(name, args):
    """构造一个单工具调用 dict（langchain 1.4 要求 type='tool_call'）。"""
    return {"name": name, "args": args, "id": "call_x", "type": "tool_call"}


def _state_with(msgs):
    return {"messages": msgs}


#: read_file 的真实参数名集合 —— **从 deepagents 的真实 schema 反查得到**，
#: 不是手抄的副本（手抄正是 aaf95335 的成因：假数据与真契约各说各话）。
#: 提取失败会退化成空集，让下面依赖它的测试大面积失败 —— 守门人本该如此。
_READ_FILE_FIELDS: frozenset[str] = (
    _extract_schema_fields(_tool("read_file", ReadFileSchema)) or frozenset()
)


def _read(path, offset=0, limit=100):
    """按 read_file 真实契约构造一次读取调用。"""
    return _call("read_file", {"file_path": path, "offset": offset, "limit": limit})


def test_same_call_repeats_trigger_warning_then_hard_stop():
    """同一工具调用重复 → 第 3 次软提醒，第 5 次硬剥。"""
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    calls = [_read("/reports/u/s/a.txt")] * 6

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
    calls = [_read(f"/reports/u/s/ch{i}.txt") for i in range(6)]

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
    h1 = _hash_tool_calls([_read("/a")])
    h2 = _hash_tool_calls([_read("/b")])
    assert h1 != h2


def test_read_file_line_bucket():
    """read_file 行号分桶：同文件同桶同 key，不同文件不同 key。

    分桶是**刻意的粗粒度**：1-200 行区间内的不同起读位置视为同一意图（防逐行刷屏），
    跨桶才区分。用真实契约 file_path / offset / limit（limit 是行数，非绝对行号）。
    """
    k1 = _stable_tool_key({"file_path": "/a", "offset": 0, "limit": 100}, None, _READ_FILE_FIELDS)
    k2 = _stable_tool_key({"file_path": "/a", "offset": 0, "limit": 100}, None, _READ_FILE_FIELDS)
    assert k1 == k2  # 完全相同的调用 → 同 key
    # 不同文件 → 不同 key
    k3 = _stable_tool_key({"file_path": "/b", "offset": 0, "limit": 100}, None, _READ_FILE_FIELDS)
    assert k1 != k3
    # 同文件、跨桶行段（第 600 行起）→ 与桶0不同（模型在读新内容，不算重复）
    k4 = _stable_tool_key({"file_path": "/a", "offset": 600, "limit": 100}, None, _READ_FILE_FIELDS)
    assert k1 != k4
    # 只给路径（模型省略分页参数）→ 仍按文件区分
    assert _stable_tool_key({"file_path": "/a"}, None, _READ_FILE_FIELDS) != _stable_tool_key(
        {"file_path": "/b"}, None, _READ_FILE_FIELDS
    )


def test_non_tool_message_no_trigger():
    """纯文本 AIMessage（无 tool_calls）→ 不触发任何动作。"""
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    state = _state_with([AIMessage(content="just text")])
    result = mw._apply(state, _RUNTIME)
    assert result is None


# ─── ②A（2026-10-10）：硬停必须写 state 标记，否则 API 层无从止损 ───────────


def test_hard_stop_writes_loop_forced_stop_marker():
    """Layer 1 硬停 → state update 里带 loop_forced_stop（原因/次数/工具）。"""
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=3)
    call = _read("/reports/u/s/a.txt")

    result = None
    for _ in range(3):
        result = mw._apply(_state_with([_ai(call)]), _RUNTIME)

    assert result is not None
    marker = result[LOOP_FORCED_STOP_KEY]
    assert marker["reason"] == STOP_REASON_REPEAT_CALLS
    assert marker["count"] >= 3
    assert marker["tool_names"] == ["read_file"]
    assert isinstance(marker["at"], float)  # 时间戳供跨轮复盘
    # 原有语义不变：tool_calls 被剥空、附加强制停止文案
    assert result["messages"][0].tool_calls == []


def test_tool_frequency_hard_stop_reason_distinguished():
    """Layer 2 硬停 → 原因码与 Layer 1 可区分（复盘时能分辨"同参重复"与"换参高频"）。"""
    mw = LoopDetectionMiddleware(
        warn_threshold=99, hard_limit=99,
        tool_freq_warn=2, tool_freq_hard_limit=3,
    )
    calls = [_read(f"/reports/u/s/ch{i}.txt") for i in range(3)]

    result = None
    for c in calls:
        result = mw._apply(_state_with([_ai(c)]), _RUNTIME)

    assert result is not None
    marker = result[LOOP_FORCED_STOP_KEY]
    assert marker["reason"] == STOP_REASON_TOOL_FREQUENCY
    assert marker["tool_name"] == "read_file"


def test_soft_warning_does_not_write_marker():
    """软提醒（warn 段）不得写标记 —— 它只是注入提示，不该终止本轮。"""
    mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=5)
    call = _read("/reports/u/s/a.txt")
    mw._apply(_state_with([_ai(call)]), _RUNTIME)
    result = mw._apply(_state_with([_ai(call)]), _RUNTIME)
    assert result is None


def test_before_agent_clears_stale_marker():
    """开轮清零：上一 run 的硬停标记必须被清掉（否则误杀下一轮正常回复）。"""
    mw = LoopDetectionMiddleware()
    stale = _state_with([])
    stale[LOOP_FORCED_STOP_KEY] = {"reason": STOP_REASON_REPEAT_CALLS, "count": 5}
    assert mw.before_agent(stale, _RUNTIME) == {LOOP_FORCED_STOP_KEY: None}

    # 干净 state（无标记）→ 不做无谓的 state 写入
    assert mw.before_agent(_state_with([]), _RUNTIME) is None


def test_state_schema_exposes_marker_key():
    """middleware 必须把自定义 state 键交给 langchain 合并（接线契约）。"""
    assert LOOP_FORCED_STOP_KEY in LoopDetectionMiddleware.state_schema.__annotations__


# ─── 契约反查（2026-10-10，会话 GY24428:aaf95335 治本）────────────────────
#
# 背景：旧实现照抄 deer-flow，把 read_file 的字段硬编码为 path/start_line/end_line；
# 而本项目 read_file 来自 deepagents，真实契约是 file_path/offset/limit。三个字段一个
# 都取不到 → key 恒为 ":0-0" → 窗口内攒够 hard_limit 次 read_file **必然**硬停。
# 下面这组测试就是那次事故的守门人。


def test_real_deepagents_schema_resolves_read_file_slots():
    """用**真实** deepagents schema 反查槽位 —— 上游改签名时本测试直接红。

    这是契约漂移在 CI 上的守门人：候选槽位表与 deepagents 实际签名一旦错位
    （aaf95335 的成因），这里第一时间失败，而不是等生产以"任务被误杀"的形式浮出。
    """
    fields = _extract_schema_fields(_tool("read_file", ReadFileSchema))
    assert fields is not None
    assert _resolve_field({}, fields, _PATH_FIELD_CANDIDATES) == "file_path"
    assert _resolve_field({}, fields, _START_FIELD_CANDIDATES) == "offset"
    assert _resolve_field({}, fields, _SPAN_FIELD_CANDIDATES) == "limit"


def test_schema_extraction_handles_all_tool_shapes():
    """契约反查要能吃下 BaseTool / OpenAI function dict / 裸 properties 三种形态。"""
    assert _extract_schema_fields(_tool("read_file", ReadFileSchema)) == _READ_FILE_FIELDS
    # OpenAI function 格式（部分 provider / 序列化路径会把工具降级成 dict）
    assert _extract_schema_fields(
        {"type": "function", "function": {"name": "read_file",
         "parameters": {"type": "object", "properties": {"file_path": {}, "offset": {}, "limit": {}}}}}
    ) == _READ_FILE_FIELDS
    # 取不到（契约未知）必须返回 None，而不是空集合 —— 调用方据此走探测兜底
    assert _extract_schema_fields(SimpleNamespace(name="x")) is None


def test_read_file_key_never_collapses_to_constant():
    """★ aaf95335 回归守门人：任何 read_file 调用都不得塌成同一个指纹。

    旧实现下这些调用全部产出 ":0-0" —— 读哪个文件、读哪一段都不影响 ——
    于是"读取不同文件"被判成"重复调用"，硬停在所难免。
    """
    fields = _READ_FILE_FIELDS
    calls = [
        {"file_path": "/a.py", "offset": 0, "limit": 100},
        {"file_path": "/b.py", "offset": 0, "limit": 100},
        {"file_path": "/c.py", "offset": 600, "limit": 100},
        {"file_path": "/a.py", "offset": 1400, "limit": 100},
        {"path": "/d.py"},  # 旧契约字段名：也绝不能塌成常量
    ]
    keys = {_stable_tool_key(a, None, fields) for a in calls}
    assert len(keys) == len(calls), f"指纹塌缩，仅 {len(keys)} 个不同 key: {sorted(keys)}"


def test_legacy_field_names_fall_back_to_full_args_not_constant():
    """契约冲突时以 schema 为准，且绝不退化成常量指纹。

    args 带旧契约字段（path/start_line/end_line）时，schema 反查出的 file_path 在本次
    args 里并不存在 —— 此时必须落到"全参 hash"（路径敏感，最坏漏报），
    而不是取空串拼出 ":0-0"（必然误杀）。
    """
    legacy = [
        {"path": "/a.py", "start_line": 1, "end_line": 200},
        {"path": "/b.py"},
    ]
    keys = {_stable_tool_key(a, None, _READ_FILE_FIELDS) for a in legacy}
    assert len(keys) == 2, f"旧字段名下指纹塌缩: {sorted(keys)}"


def test_payload_sensitive_tools_use_full_args():
    """write_file / edit_file 内容敏感：同一文件写不同内容不是重复调用。

    旧实现把内容敏感分支写成 `{"write_file", "str_replace"}` —— str_replace 是 deer-flow
    的工具名，我们真实的工具叫 edit_file，于是 edit_file 从未命中该分支。现按 schema
    里是否存在 payload 类字段判定，两个工具都自动覆盖。
    """
    wf = _extract_schema_fields(_tool("write_file", WriteFileSchema))
    ef = _extract_schema_fields(_tool("edit_file", EditFileSchema))
    assert wf is not None and ef is not None
    assert _stable_tool_key({"file_path": "/x", "content": "1"}, None, wf) != _stable_tool_key(
        {"file_path": "/x", "content": "2"}, None, wf
    )
    assert _stable_tool_key(
        {"file_path": "/x", "old_string": "a", "new_string": "b"}, None, ef
    ) != _stable_tool_key({"file_path": "/x", "old_string": "c", "new_string": "d"}, None, ef)


def test_detect_contract_drift_flags_out_of_schema_field():
    """漂移探测：args 出现 schema 之外的槽位字段 → 报出该字段名；正常契约 → 不报。"""
    assert _detect_contract_drift({"path": "/a"}, _READ_FILE_FIELDS) == "path"
    assert _detect_contract_drift(
        {"path": "/a", "start_line": 1}, _READ_FILE_FIELDS
    ) == "path"
    # 真实契约的 args / 契约未知 → 均不告警
    assert _detect_contract_drift({"file_path": "/a", "offset": 0}, _READ_FILE_FIELDS) is None
    assert _detect_contract_drift({"path": "/a"}, None) is None


def test_wrap_model_call_learns_contract_from_real_tools():
    """接线契约：wrap_model_call 必须把 request.tools 的契约学进来（E 的落点）。

    若这条断了，_stable_tool_key 会一直走"args 探测"兜底 —— 功能仍在但契约不再权威，
    aaf95335 的守门人（漂移告警）也随之失效。
    """
    mw = LoopDetectionMiddleware()
    request = SimpleNamespace(
        tools=[_tool("read_file", ReadFileSchema), _tool("write_file", WriteFileSchema)],
        runtime=_RUNTIME,
        messages=[],
    )
    sentinel = object()
    assert mw.wrap_model_call(request, lambda _req: sentinel) is sentinel
    assert mw._schema_fields_for("read_file") == _READ_FILE_FIELDS
    assert mw._schema_fields_for("write_file") == frozenset({"file_path", "content"})
    # 未出现的工具 → None（交由探测兜底），不得误报成"无字段"
    assert mw._schema_fields_for("never_seen") is None


def test_no_false_hard_stop_when_reading_different_files():
    """★ aaf95335 端到端回归：连续读 6 个不同文件不得被判循环。

    旧实现下 read_file 指纹恒为 ":0-0" → 第 5 次必然硬剥（生产事故原形）。
    这里同时关掉 Layer 2 频次阈值，单独验证 Layer 1 指纹本身不再塌缩。
    """
    mw = LoopDetectionMiddleware(
        warn_threshold=3, hard_limit=5, tool_freq_warn=99, tool_freq_hard_limit=99,
    )
    mw._learn_tool_schemas([_tool("read_file", ReadFileSchema)])
    for i in range(6):
        result = mw._apply(_state_with([_ai(_read(f"/reports/f{i}.py"))]), _RUNTIME)
        assert result is None, f"第 {i + 1} 次读不同文件被误判为循环：{result}"


def test_real_repeat_still_caught_with_real_contract():
    """反向守门人：真·重复读同一文件同一段，仍必须被硬停（修复不得把检测能力一起废掉）。"""
    mw = LoopDetectionMiddleware(
        warn_threshold=3, hard_limit=5, tool_freq_warn=99, tool_freq_hard_limit=99,
    )
    mw._learn_tool_schemas([_tool("read_file", ReadFileSchema)])
    call = _read("/a.py")
    results = [mw._apply(_state_with([_ai(call)]), _RUNTIME) for _ in range(5)]
    assert results[4] is not None
    assert results[4]["messages"][0].tool_calls == []


def test_contract_drift_logged_once(caplog):
    """契约漂移必须可见且限流：同一 (工具, 字段) 只 ERROR 一次。

    aaf95335 的根因是"完全不报"—— 契约错位被伪装成"检测逻辑正常工作"。
    """
    mw = LoopDetectionMiddleware(warn_threshold=99, hard_limit=99)
    mw._learn_tool_schemas([_tool("read_file", ReadFileSchema)])
    with caplog.at_level(logging.ERROR, logger="src.core.loop_detection"):
        for _ in range(3):
            mw._apply(_state_with([_ai(_call("read_file", {"path": "/a"}))]), _RUNTIME)
    hits = [r for r in caplog.records if "契约漂移" in r.getMessage()]
    assert len(hits) == 1, f"漂移告警应限流为 1 次，实际 {len(hits)}"
