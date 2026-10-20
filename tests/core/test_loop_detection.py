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
    tool_call_signature,
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


# ═══════════════════════════════════════════════════════════════════════════
# tool_call_signature：图内中间件与图外 P0 共用的**唯一**指纹口径（2026-10-17）
#
# 会话 GY24428:4f5ede6a 实锤：模型连发 10 条各不相同的 pymupdf 命令排查这份
# 262 页扫描件 PDF，排查路径正确、且已在收敛；但每次执行输出的**第一行**恒为那句
# 98 字符的 `fitz` 弃用警告，而 P0 旧实现取"结果前 60 字符" ⇒ 98 > 60 ⇒
# 取样窗口被噪声整行占满，真正的差异全在窗口之外 ⇒ 10 次去重后只剩 1 种指纹
# ⇒ `_repeat_count` 必达阈值 ⇒ **必然误报**，把任务掐断在离成功一步之遥。
#
# 这个模式已发作两次，根因一致（指纹区分度退化 ⇒ 概率误报变必然误报）：
#   · aaf95335 炸在**入参侧**（read_file 字段名契约错位 ⇒ 稳定 key 恒为 ":0-0"）
#   · 4f5ede6a 炸在**结果侧**（噪声占满取样窗口）
# 故本组用例同时断言"旧口径确实会退化"——否则它在新代码上也会是绿的（自证式测试）。
# ═══════════════════════════════════════════════════════════════════════════

# 真实日志里每次 pymupdf 执行输出的固定首行（98 字符）
_FITZ_WARNING = (
    "warning: The `fitz` API is deprecated and will be removed in future. "
    "Use `import pymupdf` instead."
)
assert len(_FITZ_WARNING) >= 60, "噪声行必须长于旧实现的 60 字符取样窗口"

# 4f5ede6a 里模型真实发出的那批命令（按工具日志还原，内容各不相同）
_REAL_COMMANDS = [
    'python3 -c "import fitz; d=fitz.open(\'a.pdf\'); print(%s)"' % expr
    for expr in (
        "d.page_count",
        "d[0].get_text()",
        "d[1].get_text()",
        "len(d[0].get_text())",
        "d[0].get_images()",
        "[i for i in range(d.page_count) if '光源' in d[i].get_text()]",
        "sum(len(d[i].get_text()) for i in range(10))",
        "d[0].get_text('text')[:200]",
        "d.metadata",
        "d[2].get_text()",
    )
]


def _legacy_sig(tool_name: str, result_str: str) -> str:
    """旧实现（2026-10-17 之前）：取工具结果前 60/80 字符。

    仅用于**负向对照** —— 证明这一组输入在旧代码下会退化，用例才有判别力。
    """
    s = (result_str or "").strip()
    if tool_name == "execute":
        return f"{tool_name}:{s[:60]}"
    return f"{tool_name}:{s[:80]}" if s else tool_name


def test_signature_distinguishes_commands_despite_identical_result_prefix():
    """回归锁（会话 4f5ede6a）：结果前缀被噪声占满时，不同命令必须仍是不同指纹。"""
    assert len(_REAL_COMMANDS) == 10

    # 每次执行输出都以同一行噪声开头（与真实日志一致），差异在 60 字符窗口之外
    noisy_results = [
        f"{_FITZ_WARNING}\n总页数: 262\n第{i}页: 文本长度=663" for i in range(10)
    ]

    # ① 负向对照：旧口径在这组输入下**确实**只剩 1 种指纹（用例判别力的来源）
    legacy = {_legacy_sig("execute", r) for r in noisy_results}
    assert len(legacy) == 1, (
        "负向对照失效：旧口径本应退化成 1 种指纹，用例不再能复现 4f5ede6a"
    )

    # ② 新口径：10 条不同命令必须算出 10 种指纹
    new = {tool_call_signature("execute", {"command": c}) for c in _REAL_COMMANDS}
    assert len(new) == len(_REAL_COMMANDS), (
        f"10 条不同命令只算出 {len(new)} 种指纹 —— 指纹又退化了（4f5ede6a 复发）"
    )


def test_signature_stable_for_identical_args():
    """反向守门人：真·空转（同一条命令反复发）仍必须同指纹，否则兜底失效。"""
    args = {"command": 'python3 -c "print(1)"'}
    assert tool_call_signature("execute", args) == tool_call_signature("execute", args)
    # 参数内容真变了 → 指纹必须跟着变（不能是某个常量）
    other = {"command": 'python3 -c "print(2)"'}
    assert tool_call_signature("execute", args) != tool_call_signature("execute", other)


def test_signature_never_collapses_to_bare_tool_name():
    """args 为空 dict 也不能塌成裸工具名 —— 那会让"同工具连打 N 次"变成必然误判。"""
    sig = tool_call_signature("execute", {})
    assert sig != "execute", "空 args 退化成了裸工具名（aaf95335 的形态）"
    assert sig.startswith("execute:")


def test_hash_tool_calls_reuses_tool_call_signature():
    """物理同源：`_hash_tool_calls` 的逐条结果必须与 `tool_call_signature` 一致。

    两处若各写一套 name/key 拼装，图内/图外口径会分叉 —— 同一个"空转"在两处
    得出相反结论，而两处的止损动作都不可逆。
    """
    import hashlib
    import json as _json

    calls = [
        {"name": "execute", "args": {"command": "ls /reports"}},
        {"name": "read_file", "args": {"file_path": "/a.py", "offset": 1, "limit": 100}},
    ]
    sigs = sorted(tool_call_signature(c["name"], c["args"]) for c in calls)
    expected = hashlib.md5(
        _json.dumps(sigs, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
    assert _hash_tool_calls(calls) == expected


# ─── Layer 2c：占比 / 交替循环判据（2026-10-17，83345f0c 实证）────────────
#
# 背景：绝对次数阈值 `tool_freq_hard=20` 有一个**结构性**漏洞 ——
# `_tool_freq_window` 是"滑动窗口内的同工具计数"，模型只要穿插调用任意第二种
# 工具就被稀释。83345f0c 实测：`execute` 累计 24 次，但 20 窗口内最多只有
# 16 次 < 20 ⇒ 硬停在结构上不可能触发，只能等 recursion_limit=200 烧满。
#
# 下面这组测试的第一条**直接复放该会话的真实工具名序列**（execute×8,
# write_file, execute, write_file, execute×5, write_file, write_file,
# execute×7, write_file, execute, execute, write_file, execute），
# 且每次 args 都不同 —— 这正是"Layer 1 抓不到、Layer 2 绝对次数也抓不到"的形态。
from langchain_core.messages import ToolMessage  # noqa: E402

from src.core.loop_detection import (  # noqa: E402
    STOP_REASON_TOOL_FREQ_PATTERN,
    _alternating_run,
    _max_tool_ratio,
    _window_has_deliverable,
)

#: 83345f0c 的真实工具名序列（32 次调用，取自 ex_83345f0c_msgs.jsonl 逐条统计）
_83345F0C_TOOL_SEQ = (
    ["read_file", "upload_to_sandbox"]
    + ["execute"] * 8
    + ["write_file", "execute", "write_file"]
    + ["execute"] * 5
    + ["write_file", "write_file"]
    + ["execute"] * 7
    + ["write_file", "execute", "execute", "write_file", "execute"]
)
assert len(_83345F0C_TOOL_SEQ) == 32, len(_83345F0C_TOOL_SEQ)


def _seq_calls(seq):
    """把工具名序列变成"每次参数都不同"的调用列表（复现 83345f0c 的异参形态）。"""
    out = []
    for i, name in enumerate(seq):
        if name == "write_file":
            out.append(_call(name, {"file_path": f"/home/user/s{i}.py",
                                    "content": f"# variant {i}\nprint({i})"}))
        else:
            out.append(_call(name, {"command": f"echo step-{i}", "timeout": "60"}))
    return out


def _tool_results(seq, marker=None):
    """按序列造对应的 ToolMessage 串；marker 非 None 时写进每条结果正文。"""
    msgs = []
    for i, name in enumerate(seq):
        body = f"result-{i}" if marker is None else f"{marker} step-{i}"
        msgs.append(ToolMessage(content=body, tool_call_id=f"c{i}", name=name))
    return msgs


def test_83345f0c_real_sequence_is_caught_by_pattern_layer():
    """复放 83345f0c：异参 + execute/write_file 交错 ⇒ 必须在烧满 32 次前硬停。"""
    mw = LoopDetectionMiddleware(
        warn_threshold=3, hard_limit=5,
        tool_freq_warn=12, tool_freq_hard_limit=20,   # 生产默认
        tool_freq_ratio=0.6, min_window_fill=12, alternating_min_run=6,
    )
    calls = _seq_calls(_83345F0C_TOOL_SEQ)
    # 工具结果全部不含 /reports/（复现"零交付物"）
    results = _tool_results(_83345F0C_TOOL_SEQ)

    first_hard = None
    for i, call in enumerate(calls):
        state = _state_with([*results[:i], _ai(call)])
        res = mw._apply(state, _RUNTIME)
        if res is not None and LOOP_FORCED_STOP_KEY in res:
            first_hard = (i + 1, res[LOOP_FORCED_STOP_KEY])
            break

    assert first_hard is not None, "32 次异参交错调用竟无任何硬停 —— 正是 83345f0c 的形态"
    n, marker = first_hard
    assert marker["reason"] == STOP_REASON_TOOL_FREQ_PATTERN, marker
    # 必须远早于 32 次（recursion_limit=200 的容量），否则止损没有意义
    assert n <= 24, f"硬停来得太晚（第 {n} 次），省不下步数"


def test_pattern_hard_stop_reason_distinct_from_absolute_count():
    """占比/交替硬停的原因码必须与绝对次数可区分（复盘时能分辨是哪种判据开火）。"""
    mw = LoopDetectionMiddleware(
        warn_threshold=99, hard_limit=99,
        tool_freq_warn=12, tool_freq_hard_limit=20,
        tool_freq_ratio=0.6, min_window_fill=12, alternating_min_run=6,
    )
    res = None
    for i, call in enumerate(_seq_calls(_83345F0C_TOOL_SEQ)):
        res = mw._apply(_state_with([_ai(call)]), _RUNTIME)
        if res is not None and LOOP_FORCED_STOP_KEY in res:
            break
    assert res is not None
    marker = res[LOOP_FORCED_STOP_KEY]
    assert marker["reason"] == STOP_REASON_TOOL_FREQ_PATTERN
    assert marker["ratio"] >= 0.6
    assert marker["tool_name"] in ("execute", "write_file")


def test_no_pattern_hard_stop_when_deliverable_present():
    """零交付物豁免：同样高频，但只要窗口内出现过 /reports/ 交付物就不得硬停。"""
    mw = LoopDetectionMiddleware(
        warn_threshold=99, hard_limit=99,
        tool_freq_warn=12, tool_freq_hard_limit=20,
        tool_freq_ratio=0.6, min_window_fill=12, alternating_min_run=6,
    )
    results = _tool_results(_83345F0C_TOOL_SEQ, marker="/reports/u/s/out.xlsx")
    for i, call in enumerate(_seq_calls(_83345F0C_TOOL_SEQ)):
        res = mw._apply(_state_with([*results[: i + 1], _ai(call)]), _RUNTIME)
        assert res is None or LOOP_FORCED_STOP_KEY not in res, f"第 {i+1} 次误杀：有交付物仍硬停"


def test_alternating_cycle_caught_even_when_ratio_below_threshold():
    """两种工具各占一半（占比不过线）但周期震荡 ⇒ 仍须硬停（deer-flow #2590 的洞）。"""
    mw = LoopDetectionMiddleware(
        warn_threshold=99, hard_limit=99,
        tool_freq_warn=99,                    # 关掉绝对次数与占比的次数前提
        tool_freq_hard_limit=99,
        tool_freq_ratio=0.99,                 # 占比几乎不可能达到
        min_window_fill=12, alternating_min_run=6,
    )
    seq = ["execute", "write_file"] * 10     # 完美交替，占比 0.5
    for i, call in enumerate(_seq_calls(seq)):
        res = mw._apply(_state_with([_ai(call)]), _RUNTIME)
        if res is not None and LOOP_FORCED_STOP_KEY in res:
            marker = res[LOOP_FORCED_STOP_KEY]
            assert marker["reason"] == STOP_REASON_TOOL_FREQ_PATTERN
            assert marker["alternating_run"] >= 6, marker
            return
    raise AssertionError("A,B,A,B 周期震荡未被捕获")


def test_pattern_requires_min_window_fill():
    """窗口未填满不得判定（防开局小样本噪声把正常开头当成循环）。"""
    mw = LoopDetectionMiddleware(
        warn_threshold=99, hard_limit=99,
        tool_freq_warn=1, tool_freq_hard_limit=99,
        tool_freq_ratio=0.5, min_window_fill=12, alternating_min_run=99,
    )
    # 6 次同一工具、零交付物 —— 形态像循环，但窗口没填满
    for call in _seq_calls(["execute"] * 6):
        res = mw._apply(_state_with([_ai(call)]), _RUNTIME)
        assert res is None or LOOP_FORCED_STOP_KEY not in res


def test_absolute_count_hard_stop_still_works():
    """回归：纯同工具 20 次仍走原绝对次数硬停（新判据是补充，不是替换）。

    注意：纯同工具 + 零交付物时，**占比判据会更早开火**（100% 占比，窗口一满即停），
    这是期望行为。要单独验证老的绝对次数路径，必须让占比判据被"有交付物"豁免掉 ——
    此时工具仍在被高频锤（deer-flow 原语义：单工具调用量本身就该有上限）。
    """
    mw = LoopDetectionMiddleware(
        warn_threshold=99, hard_limit=99,
        tool_freq_warn=12, tool_freq_hard_limit=20,
        tool_freq_ratio=0.6, min_window_fill=12, alternating_min_run=6,
    )
    seq = ["execute"] * 20
    results = _tool_results(seq, marker="/reports/u/s/out.txt")  # 有交付物 ⇒ 占比判据豁免
    res = None
    for i, call in enumerate(_seq_calls(seq)):
        res = mw._apply(_state_with([*results[: i + 1], _ai(call)]), _RUNTIME)
        if res is not None and LOOP_FORCED_STOP_KEY in res:
            break
    assert res is not None
    assert res[LOOP_FORCED_STOP_KEY]["reason"] == STOP_REASON_TOOL_FREQUENCY
    assert res[LOOP_FORCED_STOP_KEY]["count"] == 20


def test_pattern_layer_preempts_absolute_count_on_pure_single_tool():
    """纯同工具 + 零交付物：占比判据应先于绝对次数开火（更早止损，且原因码不同）。"""
    mw = LoopDetectionMiddleware(
        warn_threshold=99, hard_limit=99,
        tool_freq_warn=12, tool_freq_hard_limit=20,
        tool_freq_ratio=0.6, min_window_fill=12, alternating_min_run=6,
    )
    res = None
    n = 0
    for i, call in enumerate(_seq_calls(["execute"] * 20)):
        n = i + 1
        res = mw._apply(_state_with([_ai(call)]), _RUNTIME)
        if res is not None and LOOP_FORCED_STOP_KEY in res:
            break
    assert res is not None
    assert res[LOOP_FORCED_STOP_KEY]["reason"] == STOP_REASON_TOOL_FREQ_PATTERN
    assert n < 20, f"占比判据应早于绝对次数开火，实际第 {n} 次"


def test_max_tool_ratio_helper():
    assert _max_tool_ratio([]) == ("", 0, 0.0)
    name, n, ratio = _max_tool_ratio(["a", "b", "a", "a", "b"])
    assert (name, n) == ("a", 3)
    assert abs(ratio - 0.6) < 1e-9


def test_alternating_run_helper():
    assert _alternating_run([]) == 0
    assert _alternating_run(["a", "b", "a", "b"]) == 0 or _alternating_run(["a", "b", "a", "b"]) >= 1
    # 真周期震荡：run 应接近序列长度
    assert _alternating_run(["a", "b"] * 8) >= 12
    # 周期 3 也能抓到
    assert _alternating_run(["a", "b", "c"] * 6) >= 12
    # 渐进式探索不应被判为震荡
    assert _alternating_run([f"t{i}" for i in range(20)]) == 0
    # 短序列不判
    assert _alternating_run(["a", "b"]) == 0


def test_window_has_deliverable_helper():
    """交付物判据：/reports/ 正文、download_from_sandbox 工具名、以及"两者皆无"。"""
    plain = [ToolMessage(content="ok", tool_call_id="c", name="execute")]
    assert _window_has_deliverable(plain, 20) is False

    with_path = [ToolMessage(content="saved /reports/u/s/a.txt", tool_call_id="c", name="execute")]
    assert _window_has_deliverable(with_path, 20) is True

    dl = [ToolMessage(content="ok", tool_call_id="c", name="download_from_sandbox")]
    assert _window_has_deliverable(dl, 20) is True

    # list 形态 content（MCP 工具常返回 list[{'type','text'}]）
    rich = [ToolMessage(content=[{"type": "text", "text": "path=/reports/u/s/x"}],
                        tool_call_id="c", name="download_from_sandbox")]
    assert _window_has_deliverable(rich, 20) is True

    # 超出扫描上限的早期交付物不应豁免后期空转
    old = [ToolMessage(content="/reports/early", tool_call_id="c0", name="execute")]
    late = [ToolMessage(content="nothing", tool_call_id="c1", name="execute")]
    assert _window_has_deliverable(old + late, 1) is False


# ────────── 有客观进展的续查豁免（2026-10-20 add0229b）──────────
# 背景：ocr_pdf_to_text 是 submit-and-poll，262 页 ≈110 分钟 ⇒ 约 22 次同参
# 续查（每次 wait_seconds=300）。而 Layer1 的 hard_limit=5、Layer2 的
# tool_freq_warn=12 都只看"调用形态"，会在第 5/12 次掐死一个正在推进的任务。
# 生产后果（add0229b）：模型被硬停后回答"PDF 还没上传，让我先上传"，
# 而实际上后台 OCR 一直在跑、文件早在沙箱里。

_OCR_ARGS = {
    "sandbox_id": "sbx-1",
    "sandbox_pdf_path": "/home/user/_ocr/x.pdf",
    "pages": "all",
    "dpi": 150,
    "wait_seconds": 300,
}


def _tool_result(payload):
    import json as _json

    from langchain_core.messages import ToolMessage

    return ToolMessage(
        content=_json.dumps(payload, ensure_ascii=False), tool_call_id="call_x"
    )


def _replay_resume_loop(mw, rounds, payloads, args=None):
    """重放 submit-and-poll 续查：AI(call) → Tool(result) → AI(同一 call)…

    返回每轮 _apply 的结果（None = 未介入）。
    """
    args = args or _OCR_ARGS
    msgs = []
    results = []
    for i in range(rounds):
        msgs.append(_ai(_call("ocr_pdf_to_text", dict(args))))
        results.append(mw._apply(_state_with(list(msgs)), _RUNTIME))
        msgs.append(_tool_result(payloads[i] if i < len(payloads) else {}))
    return results


def test_running_resume_is_exempt_from_hard_stop():
    """同参续查、每轮有客观进展（status=running + pages_ocred 增长）⇒ 不介入。

    这是 add0229b 的正解：让模型按 progress_hint 反复续查直到 done。
    """
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    payloads = [
        {"success": True, "status": "running", "pages_ocred": i * 10}
        for i in range(10)
    ]
    results = _replay_resume_loop(mw, 10, payloads)
    intervened = [i for i, r in enumerate(results) if r is not None]
    assert not intervened, (
        "有进展的续查不得软提醒/硬停，被介入的轮次：%s" % intervened
    )


def test_real_loop_without_progress_still_hard_stops():
    """真死循环（结果立即报错、零进展）仍必须在第 5 次硬剥。"""
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    payloads = [{"success": False, "error": "file not found"}] * 8
    results = _replay_resume_loop(mw, 6, payloads)
    hard = [i for i, r in enumerate(results) if r is not None and r.get("messages")]
    assert hard, "零进展的重复调用必须被硬停"
    assert hard[0] == 4, "第 5 次（下标 4）应硬剥，实际：%s" % hard


def test_progress_exempt_respects_max():
    """超过 progress_exempt_max 仍硬停（防"后台死了但每轮照样 running"）。"""
    mw = LoopDetectionMiddleware(
        warn_threshold=3, hard_limit=5, progress_exempt_max=5
    )
    payloads = [{"success": True, "status": "running", "pages_ocred": 1}] * 12
    results = _replay_resume_loop(mw, 12, payloads)
    hard = [i for i, r in enumerate(results) if r is not None and r.get("messages")]
    assert hard, "超过 progress_exempt_max 必须硬停"
    assert hard[0] >= 5, "上限 5 ⇒ 至少第 6 次（下标 5）才硬停，实际下标 %s" % hard[0]


def test_hard_stop_text_says_backend_still_running():
    """硬停文案必须说"后台仍在运行"，否则模型会宣布任务失败/文件没上传。"""
    mw = LoopDetectionMiddleware(
        warn_threshold=3, hard_limit=5, progress_exempt_max=1
    )
    payloads = [{"success": True, "status": "running", "pages_ocred": 3}] * 8
    results = _replay_resume_loop(mw, 8, payloads)
    hard = [r for r in results if r is not None and r.get("messages")]
    assert hard, "progress_exempt_max=1 时应很快硬停"
    text = hard[0]["messages"][0].content
    assert "后台任务仍在运行" in text, text[:200]
    assert "不要声称任务失败" in text, text[:200]


def test_progress_exempt_requires_same_fingerprint():
    """只有"上次同指纹调用"的结果参与豁免；换参重试不豁免。"""
    mw = LoopDetectionMiddleware()
    call_a = _call("ocr_pdf_to_text", dict(_OCR_ARGS, pages="all"))
    call_b = _call("ocr_pdf_to_text", dict(_OCR_ARGS, pages="1-5"))
    hash_a = _hash_tool_calls([call_a], mw._schema_fields_for)
    running = {"status": "running", "pages_ocred": 9}

    switched = [_ai(call_b), _tool_result(running), _ai(call_a)]
    assert mw._last_same_call_had_progress(switched, hash_a) is False, (
        "上一次调用指纹不同（换参数重试）⇒ 不构成续查，不得豁免"
    )

    resumed = [_ai(call_a), _tool_result(running), _ai(call_a)]
    assert mw._last_same_call_had_progress(resumed, hash_a) is True


def test_progress_payload_requires_structured_fields():
    """进度判据只认结构化字段，不做宽松子串匹配（防豁免变免死金牌）。"""
    from src.core.loop_detection import _is_progress_payload

    assert _is_progress_payload('{"status": "running"}') is True
    assert _is_progress_payload('{"pages_ocred": 3}') is True
    assert _is_progress_payload(
        '[{"type": "text", "text": "{\\"status\\": \\"completed\\"}"}]'
    ) is True
    assert _is_progress_payload('{"success": false, "error": "x"}') is False
    assert _is_progress_payload("status: running") is False
    assert _is_progress_payload("") is False


def test_progress_payload_status_is_single_source_of_truth():
    """`progress_payload_status` 与 `_is_progress_payload` 必须同源、不可各写一套。

    为什么需要（2026-10-20 add0229b / 84433a68 残留）：
    「什么算推进」有两个消费者——middleware 的 Layer1/2/2c，和 chat.py 的 C
    （交付物停滞）。此前只有 middleware 那份，C 只认 /reports/ 交付物 ⇒
    middleware 已经豁免成功，C 仍在第 15 次注入「不需要文件就直接给结论」，
    把 262 页任务劝停。判据一旦各写一套，就必然出现这种"一半修好"的裂缝。
    """
    from src.core.loop_detection import _is_progress_payload, progress_payload_status

    cases = [
        ('{"status": "running", "pages_ocred": 0}', "running"),
        ('{"status": "completed", "pages_ocred": 3}', "completed"),
        ('[{"type": "text", "text": "{\\"status\\": \\"running\\"}"}]', "running"),
        ('{"pages_ocred": 3}', "running"),                     # 有计数、无 status
        ('{"status": "error", "error": "x"}', None),           # error 不算推进
        ('{"success": false, "step": "read"}', None),          # OCR 失败返回
        ("status: running", None),                             # 非 JSON，禁子串匹配
        ("", None),
    ]
    for text, expected in cases:
        got = progress_payload_status(text)
        assert got == expected, "progress_payload_status(%r) = %r，期望 %r" % (text, got, expected)
        # 两个消费者口径必须一致：布尔判据只能是它的投影
        assert _is_progress_payload(text) is (expected is not None), (
            "两处判据漂移：text=%r status=%r 但 _is_progress_payload=%r"
            % (text, got, _is_progress_payload(text))
        )


# ────────── 跨越「探查调用」的续查豁免（P2，2026-10-20 add0229b 实测序列）──────────
# 生产真事（15:54:58 / 15:55:02）：C 判据在同一毫秒正确豁免后，P0 抛 NoProgressAbort
# 取消了在飞的 tool 结果 ⇒ 模型丢掉 `pages_ocred=24/30` 的 JSON，转去 execute 自己
# 看目录、看 err.log（各一次），再发起第 6 次同参续查。middleware 此时因为
# 「紧邻上一条带 tool_calls 的 AI 是 execute」而 return False ⇒ count=6 ≥ hard=5
# 硬停。同一轮内天然 A/B：count=5（无插入）豁免成功、count=6（插 2 次）被硬停。
# 结论：豁免判据不能要求"紧邻"，必须允许跨越**不同工具**的探查调用。

_EXEC_LS = _call("execute", {"command": "ls -la /home/user/_ocr"})
_EXEC_TAIL = _call("execute", {"command": "tail -5 /home/user/_ocr/err.log"})


def test_progress_exempt_crosses_exploration_calls():
    """OCR(running) → execute 探查 ×2 → OCR(同参) 仍算"同一任务在推进"。"""
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    ocr = _call("ocr_pdf_to_text", dict(_OCR_ARGS))
    hash_ocr = _hash_tool_calls([ocr], mw._schema_fields_for)

    msgs = [
        _ai(ocr),
        _tool_result({"status": "running", "pages_ocred": 24}),
        _ai(_EXEC_LS),
        _tool_result({"output": "err.log result.json done.flag=0"}),
        _ai(_EXEC_TAIL),
        _tool_result({"output": "[ocr_run] page 24/30"}),
        _ai(ocr),
    ]
    assert mw._last_same_call_had_progress(
        msgs, hash_ocr, {"ocr_pdf_to_text"}
    ) is True, "插了 execute 自查就失去豁免 ⇒ 第 6 次续查必被硬停（add0229b 原样复现）"


def test_progress_exempt_crossing_same_tool_retry_is_rejected():
    """跨越的调用里出现**本次同工具**（换参数换策略）⇒ 不豁免。

    这是与"探查调用"的关键分界：execute 看目录是同一个任务的旁证，
    而 OCR 换 pages 区间是换策略重试，不是续查。
    """
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    ocr = _call("ocr_pdf_to_text", dict(_OCR_ARGS))
    hash_ocr = _hash_tool_calls([ocr], mw._schema_fields_for)

    msgs = [
        _ai(_call("ocr_pdf_to_text", dict(_OCR_ARGS, pages="1-5"))),
        _tool_result({"status": "running", "pages_ocred": 3}),
        _ai(ocr),
    ]
    assert mw._last_same_call_had_progress(
        msgs, hash_ocr, {"ocr_pdf_to_text"}
    ) is False, "同工具换参数被当作续查 ⇒ 换策略试错也能无限免死"


def test_progress_exempt_explore_hops_are_bounded():
    """探查跨越必须有跳数上限（防"在无关任务之间来回横跳"也算续查）。"""
    from src.core.loop_detection import _MAX_EXPLORE_HOPS

    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    ocr = _call("ocr_pdf_to_text", dict(_OCR_ARGS))
    hash_ocr = _hash_tool_calls([ocr], mw._schema_fields_for)

    def _chain(hops):
        msgs = [_ai(ocr), _tool_result({"status": "running", "pages_ocred": 9})]
        for i in range(hops):
            msgs.append(_ai(_call(f"explore_{i}", {"step": i})))
            msgs.append(_tool_result({"output": "ok"}))
        msgs.append(_ai(ocr))
        return msgs

    assert mw._last_same_call_had_progress(
        _chain(_MAX_EXPLORE_HOPS), hash_ocr, {"ocr_pdf_to_text"}
    ) is True, "上限内的探查应允许跨越（_MAX_EXPLORE_HOPS=%d）" % _MAX_EXPLORE_HOPS
    assert mw._last_same_call_had_progress(
        _chain(_MAX_EXPLORE_HOPS + 1), hash_ocr, {"ocr_pdf_to_text"}
    ) is False, "超过上限必须放弃豁免，否则跨任务横跳永远免死"


def test_replay_resumes_with_exploration_never_intervened():
    """端到端重放：8 次同参续查 + 中间插 2 次 execute 自查，全程不得介入。

    插入位置对齐生产实测（15:54:58 / 15:55:02，即第 5 次续查被 P0 打掉之后）：
    旧逻辑必须在 **count=6** 硬停 —— 与生产日志
    `Loop hard limit reached ... count=6` 逐字吻合，重放器才算可信
    （实测旧代码：`count=5` / `count=6` 两次硬停，介入轮次 [5, 6, 7]）。
    """
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    ocr = _call("ocr_pdf_to_text", dict(_OCR_ARGS))
    msgs: list = []
    results: list = []
    for i in range(8):
        msgs.append(_ai(ocr))
        results.append(mw._apply(_state_with(list(msgs)), _RUNTIME))
        msgs.append(_tool_result({"status": "running", "pages_ocred": i + 1}))
        if i in (4, 5):
            msgs.append(_ai(_EXEC_LS if i == 4 else _EXEC_TAIL))
            msgs.append(_tool_result({"output": "err.log result.json"}))
    intervened = [i for i, r in enumerate(results) if r is not None]
    assert not intervened, "有进展的续查即便插了 execute 自查也不得被介入，被介入轮次：%s" % intervened
