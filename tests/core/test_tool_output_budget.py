"""ToolOutputBudgetMiddleware 单元测试 + 接线契约。

覆盖四类：

1. **判定边界**：阈值以下不动；超阈值必处置（两个阈值任一）。
2. **两条处置路径**（核心）：
   - 转存成功 → 指针 + 头尾预览，**内容长度必须 ≤ 内联硬上界**；
   - 转存失败 → **fail-closed 截断**（这正是 deer-flow 的取向：*so the model
     context is never blown by a single large tool return*）。
3. **不变式**：任意输入长度下，处置结果长度 ≤ ``fallback_max_chars``；闸门自身
   出错绝不把成功的工具调用变成失败。
4. **接线契约**（AST）：中间件已注册、插入位置满足三条论证过的约束、
   ``/large_tool_results/`` 既在白名单、路由也**不再是 StateBackend()**。

--- spike-then-verify：核心逻辑先单测再合入 agent.py ---
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from src.core.tool_output_budget import (
    LARGE_TOOL_RESULTS_PREFIX,
    TRANSFORM_KEY,
    ToolOutputBudgetMiddleware,
    _build_body,
    _DEFAULT_EXEMPT_TOOLS,
    _estimate_text_len,
    _extract_text,
    _rebuild_content,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT_PY = PROJECT_ROOT / "src" / "services" / "agent.py"


# ─── 测试替身 ────────────────────────────────────────────


class _WriteResult:
    def __init__(self, path: str | None = None, error: str | None = None) -> None:
        self.path = path
        self.error = error


class _FakeBackend:
    """记录写入的最小后端替身。``fail_with`` 模拟各类写失败原因。"""

    def __init__(self, fail_with: str | None = None) -> None:
        self.writes: list[tuple[str, int]] = []
        self.fail_with = fail_with

    def write(self, file_path: str, content: str) -> _WriteResult:
        self.writes.append((file_path, len(content)))
        if self.fail_with is not None:
            return _WriteResult(error=self.fail_with)
        return _WriteResult(path=file_path)

    async def awrite(self, file_path: str, content: str) -> _WriteResult:
        return self.write(file_path, content)


class _FakeRequest:
    """最小 ModelRequest 替身：只需要 messages + override。"""

    def __init__(self, messages: list) -> None:
        self.messages = messages
        self.overridden_with: list | None = None

    def override(self, **kwargs):
        new = _FakeRequest(kwargs.get("messages", self.messages))
        new.overridden_with = kwargs.get("messages")
        return new


def _mw(backend=None, **kwargs) -> ToolOutputBudgetMiddleware:
    backend = backend if backend is not None else _FakeBackend()
    kwargs.setdefault("externalize_min_chars", 40_000)
    kwargs.setdefault("fallback_max_chars", 60_000)
    return ToolOutputBudgetMiddleware(backend, **kwargs)


def _tool_msg(content, name="execute", call_id="chatcmpl-tool-abc123"):
    return ToolMessage(content=content, tool_call_id=call_id, name=name)


# ─── 1. 判定边界 ─────────────────────────────────────────


@pytest.mark.parametrize("size,expect_change", [(0, False), (1_000, False), (39_999, False),
                                                (40_000, False), (40_001, True)])
def test_threshold_boundary(size, expect_change):
    """阈值语义是"超过"（strictly greater），等于阈值不动。"""
    mw = _mw()
    msg = _tool_msg("x" * size)
    out = mw._process_sync(msg)
    assert (out is not msg) is expect_change


def test_below_both_thresholds_is_untouched():
    """全库实测最大工具结果 39758 字符 —— 正常结果不应被本闸门碰到。"""
    mw = _mw()
    msg = _tool_msg("正常结果" * 3_000)  # 12000 字符
    assert mw._process_sync(msg) is msg


def test_over_fallback_only_also_triggers():
    """只把 externalize 阈值关掉，fallback 阈值仍必须独立生效。"""
    mw = _mw(externalize_min_chars=0, fallback_max_chars=1_000)
    out = mw._process_sync(_tool_msg("y" * 5_000))
    assert len(out.content) <= 1_000


# ─── 2. 转存路径 ─────────────────────────────────────────


def test_externalize_replaces_with_pointer_and_preview():
    backend = _FakeBackend()
    mw = _mw(backend)
    text = "HEAD-" + "m" * 100_000 + "-TAIL"
    out = mw._process_sync(_tool_msg(text))

    assert out is not None and len(out.content) <= mw.fallback_max_chars
    assert LARGE_TOOL_RESULTS_PREFIX in out.content          # 指针在正文里
    assert "read_file" in out.content                        # 取回指引在正文里
    assert str(len(text)) in out.content                     # 原始体积自报
    assert "HEAD-" in out.content                            # 头部保留
    assert "-TAIL" in out.content                            # 尾部保留
    # 落盘内容必须是**全文**（不是截断后的）
    assert backend.writes == [(out.additional_kwargs[TRANSFORM_KEY]["path"], len(text))]
    assert out.additional_kwargs[TRANSFORM_KEY]["transform"] == "externalized"


def test_externalized_path_is_under_large_tool_results_and_content_addressed():
    backend = _FakeBackend()
    mw = _mw(backend)
    mw._process_sync(_tool_msg("z" * 50_000, call_id="call/with.dots"))
    path, _ = backend.writes[0]
    assert path.startswith(LARGE_TOOL_RESULTS_PREFIX + "/")
    assert "/" not in path[len(LARGE_TOOL_RESULTS_PREFIX) + 1:]  # tool_call_id 已净化
    assert "." not in path[len(LARGE_TOOL_RESULTS_PREFIX) + 1:].rsplit("-", 1)[0]


def test_same_content_same_path_different_content_different_path():
    """内容寻址：同内容→同路径（幂等），不同内容→不同路径（防指向陈旧内容）。"""
    mw = _mw()
    m1 = _tool_msg("A" * 50_000)
    m2 = _tool_msg("A" * 50_000)
    m3 = _tool_msg("B" * 50_000)
    assert mw._externalize_path(m1, "A" * 50_000) == mw._externalize_path(m2, "A" * 50_000)
    assert mw._externalize_path(m1, "A" * 50_000) != mw._externalize_path(m3, "B" * 50_000)


def test_existing_file_is_treated_as_success():
    """``FilesystemBackend.write`` 拒绝覆盖已存在文件；内容寻址下"已存在"=同内容在盘上。"""
    backend = _FakeBackend(
        fail_with="Cannot write to /large_tool_results/x.txt because it already exists."
    )
    mw = _mw(backend)
    out = mw._process_sync(_tool_msg("q" * 50_000))
    assert out.additional_kwargs[TRANSFORM_KEY]["transform"] == "externalized"
    assert "不可取回" not in out.content


def test_reported_limit_matches_governing_number():
    """两条路径报的上限必须各自正确。

    转存成功报"转存阈值"（触发本次处置的数）；转存失败报"内联硬上界"（真正决定模型
    看到多长的数）。报错数字会让排障者按错的量级判断（曾写错，此处钉死）。
    """
    out_ok = _mw(_FakeBackend())._process_sync(_tool_msg("x" * 50_000))
    assert "转存阈值 40000 字符" in out_ok.content

    out_fail = _mw(
        _FakeBackend(fail_with="拒绝写入: 只能写入到以下路径: /reports/")
    )._process_sync(_tool_msg("x" * 50_000))
    assert "内联硬上界 60000 字符" in out_fail.content


# ─── 3. fail-closed 兜底 ─────────────────────────────────


@pytest.mark.parametrize(
    "fail_reason",
    [
        # 事故现场的原文（ValidatedCompositeBackend 白名单拒绝）
        "拒绝写入: 只能写入到以下路径: /reports/, /tmp/",
        "Error writing file '/large_tool_results/x.txt': [Errno 28] No space left on device",
        "Error writing file '/large_tool_results/x.txt': [Errno 13] Permission denied",
    ],
)
def test_externalize_failure_falls_back_to_truncation(fail_reason):
    """转存失败必须截断（fail-closed），且必须**自报**完整内容不可取回。"""
    backend = _FakeBackend(fail_with=fail_reason)
    mw = _mw(backend)
    text = "H" * 50_000 + "T" * 50_000
    out = mw._process_sync(_tool_msg(text))

    assert len(out.content) <= mw.fallback_max_chars
    assert out.additional_kwargs[TRANSFORM_KEY]["transform"] == "truncated"
    assert out.additional_kwargs[TRANSFORM_KEY]["path"] is None
    assert "不可取回" in out.content          # 不许让模型以为还能读回来
    assert "100000" in out.content            # 原始体积要自报


def test_backend_raising_is_swallowed_into_truncation():
    """闸门自身抛异常时：绝不把成功的工具调用变成失败，退化到截断。"""

    class _Boom:
        def write(self, file_path, content):
            raise OSError("boom")

        async def awrite(self, file_path, content):
            raise OSError("boom")

    mw = _mw(_Boom())
    out = mw._process_sync(_tool_msg("k" * 50_000))
    assert len(out.content) <= mw.fallback_max_chars
    assert "不可取回" in out.content


def test_any_input_length_stays_within_ceiling():
    """不变式：任意长度输入，处置结果都 ≤ 内联硬上界。"""
    mw = _mw()
    for size in (40_001, 60_000, 60_001, 200_000, 3_000_000):
        out = mw._process_sync(_tool_msg("a" * size))
        assert len(out.content) <= mw.fallback_max_chars, size


# ─── 4. 豁免 / 多模态 / 形态 ─────────────────────────────


def test_exempt_tool_is_never_touched():
    """read_file 必须豁免 —— 否则 read → 转存 → 再 read 会死循环。"""
    mw = _mw()
    huge = "r" * 500_000
    for name in ("read_file", "ls", "glob", "grep", "write_file", "edit_file"):
        msg = _tool_msg(huge, name=name)
        assert mw._process_sync(msg) is msg


def test_exempt_set_matches_deepagents_eviction_exclusions():
    """依赖契约：豁免集合必须与 deepagents 的驱逐豁免一致，否则两层行为静默分叉。"""
    from deepagents.middleware.filesystem import TOOLS_EXCLUDED_FROM_EVICTION

    assert set(_DEFAULT_EXEMPT_TOOLS) == set(TOOLS_EXCLUDED_FROM_EVICTION)


def test_non_text_blocks_are_preserved():
    """多模态内容被处置后，图片块必须保留（deer-flow 在此处会丢块，刻意不照搬）。"""
    mw = _mw()
    image_block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    msg = ToolMessage(
        content=[{"type": "text", "text": "p" * 50_000}, image_block],
        tool_call_id="c1",
        name="screenshot",
    )
    out = mw._process_sync(msg)
    assert isinstance(out.content, list)
    assert image_block in out.content
    assert sum(len(b.get("text", "")) for b in out.content if isinstance(b, dict)) <= 60_000


def test_image_only_content_is_skipped():
    """纯图片结果没有可预算的文本 → 跳过（不误伤视觉内容）。"""
    mw = _mw()
    msg = ToolMessage(
        content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 90_000}}],
        tool_call_id="c2",
        name="screenshot",
    )
    assert mw._process_sync(msg) is msg


def test_command_results_are_patched():
    """工具返回 Command（内嵌 messages）时也要处置。"""
    from langgraph.types import Command

    mw = _mw()
    big = _tool_msg("c" * 50_000)
    result = Command(update={"messages": [big, "not-a-message"]})
    out = mw._patch_result_sync(result)
    assert len(out.update["messages"][0].content) <= mw.fallback_max_chars
    assert out.update["messages"][1] == "not-a-message"


# ─── 5. 钩子端到端（经 handler 的真实调用形态）──────────


async def _ret(value):
    return value


def test_awrap_tool_call_end_to_end():
    backend = _FakeBackend()
    mw = _mw(backend)
    msg = _tool_msg("e" * 100_000)

    async def handler(_request):
        return msg

    out = asyncio.run(mw.awrap_tool_call(SimpleNamespace(), handler))
    assert len(out.content) <= mw.fallback_max_chars
    assert backend.writes and backend.writes[0][1] == 100_000


def test_wrap_tool_call_sync_end_to_end():
    """同步路径也必须过闸门（本工程实际走异步，但不能留下静默缺口）。"""
    backend = _FakeBackend()
    mw = _mw(backend)
    msg = _tool_msg("s" * 100_000)
    out = mw.wrap_tool_call(SimpleNamespace(), lambda _r: msg)
    assert len(out.content) <= mw.fallback_max_chars
    assert backend.writes and backend.writes[0][1] == 100_000


def test_disabled_flag_is_a_true_bypass():
    backend = _FakeBackend()
    mw = _mw(backend, enabled=False)
    msg = _tool_msg("d" * 500_000)
    assert asyncio.run(mw.awrap_tool_call(SimpleNamespace(), lambda r: _ret(msg))) is msg
    assert backend.writes == []


def test_wrap_model_call_sweeps_historical_messages():
    """历史里已经躺着的大消息（如 4863afff 的 45.8MB pending write）必须被兜底。"""
    backend = _FakeBackend()
    mw = _mw(backend)
    huge = _tool_msg("h" * 200_000, call_id="legacy")
    small = _tool_msg("ok", call_id="small")
    request = _FakeRequest([small, huge])

    seen = {}

    async def handler(req):
        seen["messages"] = req.messages
        return "resp"

    asyncio.run(mw.awrap_model_call(request, handler))
    assert len(seen["messages"][1].content) <= mw.fallback_max_chars
    assert seen["messages"][0] is small              # 小消息不重建
    assert backend.writes and backend.writes[0][1] == 200_000


def test_wrap_model_call_fast_path_does_not_rebuild():
    """没有超限消息时必须走快路径：不调用 override（避免每次模型调用重建消息列表）。"""
    mw = _mw()
    request = _FakeRequest([_tool_msg("small"), _tool_msg("also small")])
    seen = {}

    def handler(req):
        seen["req"] = req
        return "resp"

    mw.wrap_model_call(request, handler)
    assert seen["req"] is request
    assert request.overridden_with is None


# ─── 6. 纯函数 ───────────────────────────────────────────


def test_extract_and_estimate_agree_on_str():
    assert _extract_text("abc") == "abc"
    assert _estimate_text_len("abc") == 3
    assert _extract_text(123) is None
    assert _estimate_text_len(123) == 0


def test_rebuild_content_keeps_media_and_replaces_text():
    assert _rebuild_content("old", "new") == "new"
    blocks = [{"type": "text", "text": "old"}, {"type": "image_url", "image_url": {}}]
    out = _rebuild_content(blocks, "new")
    assert out[0] == {"type": "text", "text": "new"}
    assert out[1] == {"type": "image_url", "image_url": {}}


def test_build_body_reports_omitted_count_and_respects_budget():
    text = "A" * 1_000 + "B" * 1_000
    body = _build_body(text, 1_000, 200, 200)
    assert len(body) <= 1_000
    assert "已省略" in body
    assert body.startswith("A")
    assert body.endswith("B")
    assert _build_body(text, 0, 200, 200) == ""


def test_build_body_survives_absurdly_small_budget():
    """预算被配到比标记还小时，长度保证仍成立（不变式优先于通知完整性）。"""
    text = "A" * 1_000 + "B" * 1_000
    for budget in (1, 5, 12, 50):
        assert len(_build_body(text, budget, 200, 200)) <= budget


# ─── 7. 接线契约（AST）────────────────────────────────────


def _agent_tree():
    return ast.parse(AGENT_PY.read_text(encoding="utf-8"))


def _middleware_order(tree) -> list[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "create_deep_agent":
            for kw in node.keywords:
                if kw.arg != "middleware":
                    continue
                order = []
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Name):
                        order.append(elt.id)
                    elif isinstance(elt, ast.Call):
                        f = elt.func
                        order.append(getattr(f, "id", None) or getattr(f, "attr", None) or "")
                return order
    raise AssertionError("create_deep_agent(middleware=[...]) 未找到")


def test_middleware_is_registered_in_expected_position():
    order = _middleware_order(_agent_tree())
    assert "ToolOutputBudgetMiddleware" in order, "中间件未注册 —— 等于没修"

    idx = order.index("ToolOutputBudgetMiddleware")
    assert order.index("file_to_image") < idx, (
        "必须在 file_to_image 之后：先让它把图片 file block 转成 image_url，"
        "再做瘦身，否则待转的 base64 文本会被当超限结果处置"
    )
    assert idx < order.index("summarization_mw"), (
        "必须在 summarization_mw 之前（wrap_* 链 first=outermost）："
        "Summarization 按 request.messages 的体量算触发，先瘦身它才算得准"
    )
    assert idx < order.index("model_call_guard"), (
        "必须在 model_call_guard 之前：它按总字符估 prompt_tokens 并动态收紧 max_tokens"
    )
    assert idx < order.index("LoopDetectionMiddleware") < order.index("TerminalResponseMiddleware"), (
        "TerminalResponseMiddleware 的正确性前提是注册在末尾（见其源码长注释），"
        "本中间件不得插到它后面"
    )


def _route_value(tree, route_key: str):
    """在任意 Dict 字面量里找 route_key，返回其 value 节点。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == route_key:
                    return v
    return None


def _is_state_backend(value_node) -> bool:
    return (
        isinstance(value_node, ast.Call)
        and getattr(value_node.func, "id", None) == "StateBackend"
    )


def test_large_tool_results_routes_to_real_disk_not_state():
    tree = _agent_tree()
    value = _route_value(tree, LARGE_TOOL_RESULTS_PREFIX + "/")
    assert value is not None, f"{LARGE_TOOL_RESULTS_PREFIX}/ 路由缺失"
    assert not _is_state_backend(value), (
        "路由仍是 StateBackend() —— 转存内容会留在 LangGraph state 里，"
        "每次 checkpoint 仍全量序列化落 Postgres（4863afff 的 45.8MB 就是这么来的）"
    )
    assert isinstance(value, ast.Call) and getattr(value.func, "id", None) == "FilesystemBackend"


def test_route_check_detects_the_old_statebackend_value():
    """负向对照：确认上面的检测器真的能识别"路由写回 StateBackend"这种回归。"""
    snippet = 'routes = {"/large_tool_results/": StateBackend()}'
    tree = ast.parse(snippet)
    value = _route_value(tree, "/large_tool_results/")
    assert value is not None and _is_state_backend(value)


def test_large_tool_results_is_whitelisted_for_write():
    """白名单与路由表必须一致 —— 二者脱节正是 4863afff 的成因。"""
    tree = _agent_tree()
    prefixes = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ValidatedCompositeBackend":
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "ALLOWED_WRITE_PREFIXES"
                    for t in stmt.targets
                ):
                    prefixes = {
                        elt.value for elt in stmt.value.elts if isinstance(elt, ast.Constant)
                    }
    assert prefixes is not None, "ALLOWED_WRITE_PREFIXES 未找到"
    assert LARGE_TOOL_RESULTS_PREFIX + "/" in prefixes, (
        "白名单缺 " + LARGE_TOOL_RESULTS_PREFIX + "/ —— 转存会被拒写，"
        "fail-closed 截断会变成常态（内容虽不再撑爆上下文，但失去了取回能力）"
    )


# ─── 8. 真实后端闭环（不是替身）──────────────────────────


def test_real_backend_large_tool_results_round_trip(tmp_path, monkeypatch):
    """真实 ``ValidatedCompositeBackend`` 上的写-读闭环。

    证据链：白名单放行 → 路由到 FilesystemBackend → **落真实磁盘** → ``read`` 能取回
    （模型 read_file 走的就是这条）。必须用真实后端而非替身：4863afff 的成因恰恰是
    "routes 表配了、白名单没放行"这类**跨两处配置的脱节**，替身测不出来。
    """
    from src.core.config import settings
    from src.services.agent import build_backend

    monkeypatch.setattr(settings, "agent_workspace", str(tmp_path))
    backend = build_backend("u1", "s1", store=None, sandbox=None)

    res = backend.write("/large_tool_results/probe.txt", "body-123")
    assert getattr(res, "error", None) is None, getattr(res, "error", None)
    disk = tmp_path / "tool_results" / "u1" / "s1" / "probe.txt"
    assert disk.is_file(), "转存没有落到真实磁盘（路由可能又指回 StateBackend）"
    assert disk.read_text(encoding="utf-8") == "body-123"

    read_result = backend.read("/large_tool_results/probe.txt")
    assert getattr(read_result, "error", None) is None
    assert read_result.file_data["content"] == "body-123"


def test_real_backend_async_write_and_disk_landing(tmp_path, monkeypatch):
    """异步路径（生产实际走这条）同样落真实磁盘。"""
    from src.core.config import settings
    from src.services.agent import build_backend

    monkeypatch.setattr(settings, "agent_workspace", str(tmp_path))
    backend = build_backend("u1", "s1", store=None, sandbox=None)

    res = asyncio.run(backend.awrite("/large_tool_results/async.txt", "async-body"))
    assert getattr(res, "error", None) is None
    assert (tmp_path / "tool_results" / "u1" / "s1" / "async.txt").read_text(
        encoding="utf-8"
    ) == "async-body"


def test_real_backend_existing_file_error_is_recognised_as_success(tmp_path, monkeypatch):
    """内容寻址依赖的语义：``FilesystemBackend`` 拒绝覆盖已存在文件。

    ``_write_ok`` 必须把这条**唯一**的"已存在"错误判为成功（同内容已在盘上、
    指针有效）；其余错误（白名单拒绝等）一律失败并走 fail-closed。
    """
    from src.core.config import settings
    from src.services.agent import build_backend

    monkeypatch.setattr(settings, "agent_workspace", str(tmp_path))
    backend = build_backend("u1", "s1", store=None, sandbox=None)

    assert getattr(backend.write("/large_tool_results/dup.txt", "same"), "error", None) is None
    again = backend.write("/large_tool_results/dup.txt", "same")
    assert "already exists" in str(getattr(again, "error", ""))
    assert ToolOutputBudgetMiddleware._write_ok(again) is True

    rejected = backend.write("/etc/evil.txt", "x")
    assert ToolOutputBudgetMiddleware._write_ok(rejected) is False, (
        "白名单拒绝绝不能被当作成功 —— 否则截断兜底会被绕过"
    )


def test_real_backend_rejects_paths_outside_whitelist(tmp_path, monkeypatch):
    """对照：这批改动没有放松任何权限（新增一个白名单项 ≠ 放开闸门）。"""
    from src.core.config import settings
    from src.services.agent import build_backend

    monkeypatch.setattr(settings, "agent_workspace", str(tmp_path))
    backend = build_backend("u1", "s1", store=None, sandbox=None)

    for path in ("/etc/passwd", "/root/.ssh/id_rsa", "/uploads/u1/s1/x.txt"):
        assert getattr(backend.write(path, "x"), "error", None), f"{path} 不该被放行"
