"""P1-b：SSE 帧编码 + run 端点接线契约。

背景：
    P1-b 把「run 生命周期」从「SSE 连接生命周期」里解耦出去（方案
    ``P0prime-P1-run实体方案.html``）。解耦之后，**线上帧格式**成了前后端之间唯一
    的契约面 —— 而 P1-b 上线时前端（geesun_agent_web）还没改（P1-c 才改）。所以
    本文件守的是"逐字兼容"这件事本身：

      · 结构性事件仍是 ``data: {"type": <事件名>, ...}``；
      · 正文/推理仍是 ``data: {"type": "token"|"reasoning", "content": ...}``；
      · 保活仍是 SSE 注释行 ``": ping\\n\\n"``；
      · 结束仍是 ``data: [DONE]`` —— 前端把它当作"正常结束"的**唯一**判据
        （lib/sse-client.ts：EOF 而无该哨兵即判定传输被切断并报错）。

    为什么这批断言值得单独写：``type`` 字段一旦写错（例如把 ``token`` 写成
    ``delta``），老前端不会报错，只会**静默不渲染**正文 —— 用户看到的是"回答了但
    界面空白"。这类故障在静态 AST 断言下完全隐形，必须真跑一遍映射。

为什么不直接 ``import src.api.endpoints.chat``：
    该模块顶层拖着 psycopg / e2b / deepagents / pydantic-settings 一整套运行期依赖
    与配置（本机测试环境装不齐，CI 也没有）。而这里要测的两个函数（``_sse_frame`` /
    ``_bridge_item_to_frame``）是**纯映射、零依赖**。所以按 ``ast`` 把它们的源码段
    抽出来，exec 在一个只注入 ``json`` + 桥类型的命名空间里 —— 既保住真实行为断言，
    又不必为了 30 行代码去背整个服务栈。

运行（需要 src 包可导入，故 cwd 必须在仓根）:
    python -m pytest tests/api/test_run_endpoints.py -q
"""

from __future__ import annotations

import ast
import asyncio
import json
from contextlib import aclosing
from pathlib import Path

import pytest

from src.core.stream_bridge import (
    DELTA_REASONING,
    DELTA_TOKEN,
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    MemoryStreamBridge,
    StreamDelta,
    StreamEvent,
    StreamGap,
    StreamSnapshot,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CHAT_PY = PROJECT_ROOT / "src" / "api" / "endpoints" / "chat.py"
CHAT_SRC = CHAT_PY.read_text(encoding="utf-8")

#: 跨层测试用的 run_id（与 tests/core/test_stream_bridge.py 同惯例）
RUN = "run-1"

#: 抽出来的纯函数会引用到的、需要注入执行命名空间的名字
_BRIDGE_NAMES = {
    "DELTA_REASONING": DELTA_REASONING,
    "DELTA_TOKEN": DELTA_TOKEN,
    "END_SENTINEL": END_SENTINEL,
    "HEARTBEAT_SENTINEL": HEARTBEAT_SENTINEL,
    "StreamDelta": StreamDelta,
    "StreamEvent": StreamEvent,
    "StreamGap": StreamGap,
    "StreamSnapshot": StreamSnapshot,
}


def _load_pure_functions(
    *names: str, patch: tuple[str, str] | None = None
) -> dict[str, object]:
    """把 chat.py 顶层的指定函数抽出源码并 exec，返回其命名空间。

    ``patch`` 是 ``(旧串, 新串)``：先对整份源码做一次字符串替换再抽取。存在的意义是
    **负向对照** —— 证明下面的行为断言真的在检查映射结果，而不是"函数能跑就算过"。
    """
    src = CHAT_SRC if patch is None else CHAT_SRC.replace(*patch)
    tree = ast.parse(src, filename=str(CHAT_PY))

    segments: list[str] = []
    for node in tree.body:  # 只看模块顶层，避免抽到同名嵌套闭包
        if isinstance(node, ast.FunctionDef) and node.name in names:
            segment = ast.get_source_segment(src, node)
            assert segment, f"ast.get_source_segment 取不到 {node.name} 的源码段"
            segments.append(segment)

    found = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    missing = set(names) - found
    assert not missing, (
        f"chat.py 顶层找不到这些函数：{sorted(missing)}。结构已变，本文件需同步更新。"
    )

    ns: dict[str, object] = {"json": json, **_BRIDGE_NAMES}
    exec(compile("\n\n".join(segments), "<chat-pure-functions>", "exec"), ns)  # noqa: S102
    return ns


@pytest.fixture(scope="module")
def frames():
    """返回 ``_bridge_item_to_frame`` 与 ``_sse_frame`` 两个已加载的纯函数。"""
    ns = _load_pure_functions("_sse_frame", "_bridge_item_to_frame")
    return ns["_bridge_item_to_frame"], ns["_sse_frame"]


def _data_payload(frame: str) -> dict:
    """按**前端的解析方式**取出帧里的 JSON：只看 ``data: `` 开头的行，其余（``id:``、
    注释行）一律跳过。

    刻意不写成"按 \\n 切第二段"：无 ``id:`` 行的帧第二段是空行，那种写法会取到空串 ——
    而带 ``id:`` 行的帧恰好掩盖这个错误（结构性事件全绿、逐增量帧全红）。用与前端
    同构的取法，顺带把"``id:`` 行不影响前端"这条兼容性也钉住了。
    """
    line = next(
        (ln for ln in frame.splitlines() if ln.startswith("data: ")),
        None,
    )
    assert line is not None, f"帧里没有 data: 行：{frame!r}"
    return json.loads(line[len("data: "):])


# ─── ① 保活／结束：两个哨兵帧的字节形态 ─────────────────────────────────────


def test_heartbeat_is_bare_sse_comment(frames):
    """心跳必须是**注释行**（`: ping`），不是 data 帧。

    理由：前端解析器只看 ``data: `` 开头的行，注释行天然被忽略；若改成 data 帧，
    老前端会收到一个它不认识的 type（最坏情况是弹错或重置状态）。
    """
    to_frame, _ = frames
    assert to_frame(HEARTBEAT_SENTINEL) == ": ping\n\n"


def test_end_is_done_data_line(frames):
    """结束帧必须**逐字**是 ``data: [DONE]``。

    这是前端判定"正常结束"的唯一判据（lib/sse-client.ts）：改成别的字符串，前端会
    把一次正常完成当成传输被切断，弹"回复不完整"。
    """
    to_frame, _ = frames
    assert to_frame(END_SENTINEL) == "data: [DONE]\n\n"


# ─── ② 结构性事件：id 行 + type 字段 ───────────────────────────────────────


def test_structural_event_carries_event_id_and_type(frames):
    """结构性事件帧 = ``id:`` 行 + ``data: {"type": <事件名>, ...载荷}``。"""
    to_frame, _ = frames
    event = StreamEvent(
        id="1730000000123-7",
        event="tool_call",
        data={"tool_name": "write_file", "args": {"path": "/reports/a.md"}},
    )

    frame = to_frame(event)

    assert frame.startswith("id: 1730000000123-7\n"), "缺少 id: 行 —— 续订时前端无法回传游标"
    assert frame.endswith("\n\n"), "SSE 帧必须以空行结束"
    payload = _data_payload(frame)
    assert payload == {
        "type": "tool_call",
        "tool_name": "write_file",
        "args": {"path": "/reports/a.md"},
    }


def test_structural_event_wraps_non_dict_payload(frames):
    """载荷不是 dict 时包成 ``{"data": ...}``，而不是让 ``**`` 展开炸掉。"""
    to_frame, _ = frames
    frame = to_frame(StreamEvent(id="1-0", event="agent_status", data="thinking"))
    payload = _data_payload(frame)
    assert payload == {"type": "agent_status", "data": "thinking"}


def test_structural_event_payload_keeps_type_key_of_its_own(frames):
    """载荷里**不该**出现 type —— 事件名是权威，载荷不得覆盖它。

    实现是 ``{"type": item.event, **body}``，若 body 里带 ``type`` 就会覆盖事件名。
    此处固化"调用方不传 type"这条约定：一旦有人往载荷里塞 type，本断言会立刻暴露。
    """
    to_frame, _ = frames
    event = StreamEvent(id="1-0", event="ai_message_start", data={"index": 0})
    payload = _data_payload(to_frame(event))
    assert payload["type"] == "ai_message_start"


# ─── ③ 逐增量：token / reasoning 走 content 字段 ───────────────────────────


@pytest.mark.parametrize("kind", [DELTA_TOKEN, DELTA_REASONING])
def test_delta_maps_kind_to_type_and_text_to_content(frames, kind):
    """``{"type": <kind>, "content": <text>}`` —— 老前端按这两个键渲染正文。

    kind 写成别的（如 "delta"）前端不会报错，只会静默不渲染：用户看到"回答了但
    界面空白"。路径参数化覆盖 token 与 reasoning 两条。
    """
    to_frame, _ = frames
    frame = to_frame(StreamDelta(kind=kind, text="你好"))
    assert _data_payload(frame) == {"type": kind, "content": "你好"}


def test_delta_frame_has_no_id_line(frames):
    """逐增量帧不带 ``id:`` 行 —— 它不进有界日志，没有可回传的游标。"""
    to_frame, _ = frames
    assert to_frame(StreamDelta(kind=DELTA_TOKEN, text="x")).startswith("data: ")


# ─── ④ 快照：空的不下发，有的带权威正文 + 截断计数 ─────────────────────────


def test_empty_snapshot_is_suppressed(frames):
    """首订几乎恒为空快照 → 下发给老前端只会塞一个未知 type。返回 None 表示不下发。"""
    to_frame, _ = frames
    assert to_frame(StreamSnapshot(content="", reasoning="")) is None


def test_snapshot_carries_authoritative_text_and_dropped_counts(frames):
    """重连时快照是正文的**权威**来源（替换语义），并如实带上被裁掉的头字符数。"""
    to_frame, _ = frames
    frame = to_frame(
        StreamSnapshot(content="完整正文", reasoning="推理", content_dropped_chars=1200)
    )
    payload = _data_payload(frame)
    assert payload == {
        "type": "snapshot",
        "content": "完整正文",
        "reasoning": "推理",
        "content_dropped_chars": 1200,
        "reasoning_dropped_chars": 0,
    }


def test_reasoning_only_snapshot_is_emitted(frames):
    """只有推理、还没有正文时也要下发 —— 否则重连后看不到"正在思考"。"""
    to_frame, _ = frames
    frame = to_frame(StreamSnapshot(content="", reasoning="在想"))
    assert frame is not None
    payload = _data_payload(frame)
    assert payload["reasoning"] == "在想"


def test_new_subscriber_snapshot_degrades_to_append_frames(frames):
    """新订阅者的快照必须降级成 ``reasoning`` + ``token`` 两帧。

    为什么必须有这条：桥的首帧恒为快照（替换语义）。P1-b 上线时前端还没升级，
    它不认识 ``snapshot`` 这个 type —— 会**静默忽略整帧**。若不降级，首订时已经
    生成的正文（worker 抢在订阅生效前 publish 的那段）就永远看不到，用户看到的是
    "回答从中间开始"。
    顺序与实时流一致：先推理、后正文。
    """
    to_frame, _ = frames
    text = to_frame(
        StreamSnapshot(content="正文", reasoning="推理"), append_text_snapshot=True
    )
    assert text is not None
    assert _payloads(text) == [
        {"type": "reasoning", "content": "推理"},
        {"type": "token", "content": "正文"},
    ]


def test_new_subscriber_snapshot_without_reasoning_emits_only_token(frames):
    """没有推理时只发一帧 ``token``（不发空 content 帧 —— 那只会让前端多渲染一次空追加）。"""
    to_frame, _ = frames
    text = to_frame(StreamSnapshot(content="只有正文", reasoning=""), append_text_snapshot=True)
    assert _payloads(text) == [{"type": "token", "content": "只有正文"}]


def test_empty_snapshot_is_dropped_for_new_subscriber_too(frames):
    """空快照对新订阅者同样不下发：没有正文可补，发出去只是噪音。

    这正是"worker 刚起、还没吐 token 时首订"的常态 —— 改造前它就是**空**的一轮，
    线上不该多出任何字节。
    """
    to_frame, _ = frames
    assert to_frame(StreamSnapshot(content="", reasoning=""), append_text_snapshot=True) is None


def test_renewing_subscriber_keeps_snapshot_replacement_semantics(frames):
    """默认（续订者）仍是 ``snapshot``：它本地已有半截正文，必须被权威全文**替换**。

    若这里也降级成追加，续订者会把全文**接在**已有的半截后面 → 正文重复。
    """
    to_frame, _ = frames
    payload = _data_payload(to_frame(StreamSnapshot(content="正文", reasoning="推理")))
    assert payload["type"] == "snapshot"
    assert payload["content"] == "正文"


# ─── ⑤ 游标越界：gap → resync 帧 ───────────────────────────────────────────


def test_gap_becomes_resync_frame(frames):
    """游标被裁剪 ⇒ 下发 ``resync``，把三个边界 id 如实给前端。

    前端据此知道"手里的结构性历史缺了一段"，应回权威存储重取（P1-c）——
    而不是把缺头的事件序列误当成完整状态。
    """
    to_frame, _ = frames
    frame = to_frame(
        StreamGap(
            requested_event_id="1730000000001-0",
            earliest_available_event_id="1730000000009-8",
            latest_available_event_id="1730000000120-119",
        )
    )
    payload = _data_payload(frame)
    assert payload == {
        "type": "resync",
        "requested_event_id": "1730000000001-0",
        "earliest_available_event_id": "1730000000009-8",
        "latest_available_event_id": "1730000000120-119",
    }


# ─── ⑥ 帧格式不变量 ────────────────────────────────────────────────────────


def test_unknown_item_is_ignored(frames):
    """桥里出现本层不认识的东西（如内部的 RESYNC_MARKER）时静默跳过，不炸连接。"""
    to_frame, _ = frames
    assert to_frame(object()) is None


def test_every_emitted_frame_ends_with_blank_line(frames):
    """所有会下发的帧都必须以空行收尾 —— SSE 靠空行切分帧，少一个就整段错位。"""
    to_frame, _ = frames
    items = [
        HEARTBEAT_SENTINEL,
        END_SENTINEL,
        StreamEvent(id="1-0", event="error", data={"message": "x"}),
        StreamDelta(kind=DELTA_TOKEN, text="a"),
        StreamSnapshot(content="b", reasoning="c"),
        StreamGap(requested_event_id=None, earliest_available_event_id="",
                  latest_available_event_id=""),
    ]
    for item in items:
        frame = to_frame(item)
        assert frame is not None and frame.endswith("\n\n"), f"帧未以空行收尾: {frame!r}"


def test_sse_frame_omits_id_line_without_event_id(frames):
    """``_sse_frame`` 无 event_id 时不写 ``id:`` 行（不留空 ``id:`` 行）。"""
    _, sse_frame = frames
    assert sse_frame({"type": "x"}) == 'data: {"type": "x"}\n\n'


def test_sse_frame_writes_id_line_when_given(frames):
    _, sse_frame = frames
    frame = sse_frame({"type": "x"}, event_id="9-1")
    assert frame == 'id: 9-1\ndata: {"type": "x"}\n\n'


def test_sse_frame_keeps_chinese_unencoded(frames):
    """``ensure_ascii=False``：中文按 UTF-8 原样下发（而不是 \\uXXXX 转义）。

    不为省字节，而是为了让线上流量用肉眼/curl 就能读 —— 排查 SSE 问题时这一点很关键。
    """
    _, sse_frame = frames
    assert "中文" in sse_frame({"type": "token", "content": "中文"})


# ─── ⑦ 负向对照：证明上面测的是映射结果，不是"跑通就算过" ──────────────────


def test_end_mapping_detector_is_sensitive(frames):
    """把 END 的映射换成心跳字节后，行为断言必须失败。

    意义：证明 ``test_end_is_done_data_line`` 检查的是"END 映射成 [DONE]"这个**具体
    事实**。若检测逻辑退化成"函数能返回字符串就算过"，这里仍是绿的 —— 那就等于空测。
    """
    to_frame, _ = frames
    assert to_frame(END_SENTINEL) == "data: [DONE]\n\n"

    patched = _load_pure_functions(
        "_bridge_item_to_frame",
        patch=('return "data: [DONE]\\n\\n"', 'return ": ping\\n\\n"'),
    )["_bridge_item_to_frame"]
    assert patched(END_SENTINEL) != "data: [DONE]\n\n", (
        "把 END 的映射改成心跳字节后仍然返回 [DONE] —— 说明负向对照没有真的改到源码"
    )


def test_token_kind_detector_is_sensitive(frames):
    """把 token 的 type 改成 "delta" 后，正文断言必须失败。"""
    to_frame, _ = frames
    assert _data_payload(to_frame(StreamDelta(kind=DELTA_TOKEN, text="a")))["type"] == "token"

    patched = _load_pure_functions(
        "_sse_frame",
        "_bridge_item_to_frame",
        patch=('return _sse_frame({"type": item.kind, "content": item.text})',
               'return _sse_frame({"type": "delta", "content": item.text})'),
    )["_bridge_item_to_frame"]
    payload = _data_payload(patched(StreamDelta(kind=DELTA_TOKEN, text="a")))
    assert payload["type"] == "delta", "负向对照没有改到源码，断言无意义"


# ─── ⑧ 端点接线（AST 静态断言）────────────────────────────────────────────


def _routes(tree: ast.Module) -> set[tuple[str, str]]:
    """收集 ``@router.<method>("<path>")`` 装饰器。"""
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and isinstance(dec.func.value, ast.Name)
                and dec.func.value.id == "router"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                found.add((dec.func.attr, dec.args[0].value))
    return found


def test_three_new_endpoints_are_registered():
    """方案 §5.1 的三个新端点必须都注册上（少一个，P1-c 就没有落点）。"""
    chat_tree = ast.parse(CHAT_SRC, filename=str(CHAT_PY))
    chat_routes = _routes(chat_tree)
    assert ("post", "/chat") in chat_routes
    assert ("get", "/chat/runs/{run_id}/stream") in chat_routes, "缺少续订端点"
    assert ("post", "/chat/runs/{run_id}/cancel") in chat_routes, "缺少终止端点"

    sessions_py = PROJECT_ROOT / "src" / "api" / "endpoints" / "sessions.py"
    sessions_routes = _routes(ast.parse(sessions_py.read_text(encoding="utf-8")))
    assert ("get", "/sessions/{session_id}/active-run") in sessions_routes, (
        "缺少 active-run 端点 —— 刷新后前端没有任何服务端判据可用"
    )


def test_resume_endpoint_accepts_last_event_id_header():
    """续订端点必须能接 ``Last-Event-ID`` 头（原生 EventSource 重连时自动带）。"""
    tree = ast.parse(CHAT_SRC, filename=str(CHAT_PY))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "resume_run_stream":
            target = node
    assert target is not None, "找不到 resume_run_stream（结构已变，请同步）"

    aliases: set[str] = set()
    for d in list(target.args.defaults) + list(target.args.kw_defaults):
        if not isinstance(d, ast.Call):
            continue
        for kw in d.keywords:
            if kw.arg == "alias" and isinstance(kw.value, ast.Constant):
                aliases.add(kw.value.value)
    assert "Last-Event-ID" in aliases, (
        f"续订端点未声明 Header(alias='Last-Event-ID')；实际 alias={sorted(aliases)}"
    )
    assert "last_event_id" in aliases, "缺少 query 兜底入口（手工重放用）"


def test_worker_is_started_detached_from_request_task():
    """worker 必须经 ``asyncio.create_task`` 启动，**不得**在响应生成器里 await。

    这是"刷新不再杀任务"的落点：Starlette 在客户端断连时取消的是**请求 task**，
    而 create_task 出来的任务不在它的 task group 里，取消不传染。若被重构改回
    await，整次 P1-b 立即失效 —— 而功能测试很难发现（只有真断连才暴露）。
    """
    tree = ast.parse(CHAT_SRC, filename=str(CHAT_PY))
    started = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create_task"
        and any(
            isinstance(a, ast.Call)
            and isinstance(a.func, ast.Name)
            and a.func.id == "_run_worker"
            for a in n.args
        )
    ]
    assert started, "找不到 asyncio.create_task(_run_worker(...)) —— run 又会被请求取消带走"
    # 且必须是 attach：进程内登记 task 句柄，"终止任务"端点才有可取消的目标
    assert "attach_task" in CHAT_SRC, "run_registry.attach_task 未被调用，cancel 端点将无可取消目标"


def test_sse_response_closes_subscription():
    """``_sse_response`` 必须用 ``aclosing`` 关闭订阅生成器。

    ``async for`` 里 break/return **不会**自动关异步生成器（CPython 只在 aclose 或
    GC 时跑 finally）。不关就留下"幽灵订阅者"：桥会一直往它的队列投递，白占内存 ——
    这是桥模块 docstring 明写的调用契约。
    """
    tree = ast.parse(CHAT_SRC, filename=str(CHAT_PY))
    func = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_sse_response"),
        None,
    )
    assert func is not None, "找不到 _sse_response"
    assert any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "aclosing"
        for n in ast.walk(func)
    ), "_sse_response 未使用 aclosing —— 断连后会留下幽灵订阅者"


# ─── ⑨ 跨层联合：桥 → 帧编码的真实字节流 ────────────────────────────────────
#
# 上面 ①~⑦ 测的是"帧编码函数对某个 item 给出什么"，⑥ 之外的桥行为在
# tests/core/test_stream_bridge.py（P1-a，23 例）。两侧都绿**仍可能**漏掉一类问题：
# 桥吐出来的 item 序列，经帧编码后拼成的**线上字节**是否还是老前端认得的形状。
# 这一节就钉这个 —— 它是"P1-b 上线而前端未改仍可工作"的直接证据。


def _collect_frames(
    bridge, run_id: str, *, last_event_id: str | None = None
) -> str:
    """订阅并把桥的每个 item 逐帧编码成字节串。

    ⚠ 刻意**逐行复刻** ``_sse_response._gen`` 的循环，包括"只对首帧降级快照"这条
    规则 —— 本节的断言测的就是那段代码编出来的线上字节。复刻一旦与生产漂移，测试会
    给出假绿，所以本节末尾另有一条 AST 断言把生产侧的传参钉住（§⑩）。

    ``aclosing`` 不能省 —— 与生产代码同因：``async for`` 里 break 不会自动关闭异步
    生成器，不关就留下幽灵订阅者。
    """
    to_frame = _load_pure_functions(
        "_sse_frame", "_bridge_item_to_frame"
    )["_bridge_item_to_frame"]
    append_first_snapshot = last_event_id is None

    async def _go() -> str:
        out: list[str] = []
        first = True
        async with aclosing(
            bridge.subscribe(
                run_id, last_event_id=last_event_id, heartbeat_interval=0.05
            )
        ) as stream:
            async for item in stream:
                frame = to_frame(
                    item, append_text_snapshot=first and append_first_snapshot
                )
                first = False
                if frame is not None:
                    out.append(frame)
                if item is END_SENTINEL:
                    break
        return "".join(out)

    return asyncio.run(_go())


def _payloads(text: str) -> list[dict]:
    """按前端的方式取出全部 data 帧（跳过注释行与 ``[DONE]`` 哨兵）。"""
    return [
        json.loads(line[len("data: "):])
        for line in text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def _event_ids(text: str) -> list[str]:
    """取出帧里的全部 ``id:`` 值（= 前端的续订游标）。"""
    return [line[len("id: "):] for line in text.splitlines() if line.startswith("id: ")]


def test_online_bytes_are_recognisable_by_pre_p1b_frontend():
    """一轮完整产出经"桥 → 帧编码"后，必须是老前端认得的字节形状。

    老前端（P1-c 之前）只有三条判据：
      · ``data: {"type": <事件名>, ...}`` —— switch 到已知分支；
      · ``data: {"type": "token"|"reasoning", "content": ...}`` —— 追加正文；
      · ``data: [DONE]`` —— 唯一的"正常结束"信号。

    本断言把这三条同时钉住，并额外要求结构性事件带 ``id:``（否则 P1-c 的续订没有
    游标可用）。

    这里所有 ``publish`` 都发生在订阅**之前** ⇒ 本轮的正文只存在于桥的累积器里，
    订阅者拿到它的唯一途径就是首帧快照。所以这条用例真正在证的是：
    **首订时已生成的前缀正文，必须以老前端认得的追加型帧下发**（而不是它不认识的
    ``snapshot`` —— 那会被静默丢弃，用户看到"回答从中间开始"）。
    """
    async def _produce():
        bridge = MemoryStreamBridge()
        await bridge.publish(RUN, "ai_message_start", {"index": 0})
        await bridge.publish_delta(RUN, DELTA_TOKEN, "你")
        await bridge.publish_delta(RUN, DELTA_TOKEN, "好")
        await bridge.publish(RUN, "tool_call", {"tool_name": "write_file"})
        await bridge.publish_delta(RUN, DELTA_REASONING, "想")
        await bridge.publish_end(RUN)
        return bridge

    text = _collect_frames(asyncio.run(_produce()), RUN)

    assert text.endswith("data: [DONE]\n\n"), (
        "线上字节没有以 data: [DONE] 收尾 —— 老前端会把一次正常完成当成传输被切断"
    )
    assert _event_ids(text), "结构性事件没有 id: 行 —— P1-c 的续订拿不到游标"

    payloads = _payloads(text)
    types = [p["type"] for p in payloads]
    assert "snapshot" not in types, (
        "新订阅者收到了 snapshot 帧 —— 未升级的前端会静默丢弃整帧，"
        "首订时已生成的前缀正文永远看不到"
    )
    # 前缀正文以追加型帧补发，且顺序与实时流一致：先推理、后正文
    assert types[:2] == ["reasoning", "token"], f"正文补发帧的顺序不对: {types}"
    assert {"type": "reasoning", "content": "想"} in payloads
    assert {"type": "token", "content": "你好"} in payloads
    # 结构性事件原样（老前端靠 type 分支处理）
    assert {"type": "ai_message_start", "index": 0} in payloads
    assert {"type": "tool_call", "tool_name": "write_file"} in payloads
    assert all("type" in p for p in payloads), f"存在没有 type 的帧：{payloads}"


def test_resume_sends_only_newer_events_and_pads_text_with_snapshot():
    """断线重连：带 ``Last-Event-ID`` 只收到游标**之后**的事件，缺的正文由快照补齐。

    这是"刷新不再丢内容"的协议面证据，两半缺一不可：
      · 结构性事件按游标续传（不重复、不漏）—— 否则重复的 `file_generated` 会多出
        卡片、漏掉的 `completion_blocked` 会让用户不知道本轮为何被拦；
      · 逐 token 不进日志、只能靠首帧快照给**权威**正文 —— 断线期间生成的正文全在
        这里，重连代价因此与已生成文本长度解耦。
    """
    async def _produce() -> MemoryStreamBridge:
        bridge = MemoryStreamBridge()
        await bridge.publish(RUN, "ai_message_start", {"index": 0})
        await bridge.publish_delta(RUN, DELTA_TOKEN, "断线前写的")
        await bridge.publish(RUN, "tool_call", {"tool_name": "write_file"})
        await bridge.publish_delta(RUN, DELTA_TOKEN, "断线后写的")
        await bridge.publish_end(RUN)
        return bridge

    bridge = asyncio.run(_produce())

    # 第一次订阅 ≈ 断线前的那次连接（没带游标）：拿到两条结构性事件，记下第一条游标
    first = _collect_frames(bridge, RUN)
    ids = _event_ids(first)
    assert len(ids) == 2, f"预期两条结构性事件，实际游标={ids}"
    first_types = [p["type"] for p in _payloads(first)]
    assert [t for t in first_types if t in ("ai_message_start", "tool_call")] == [
        "ai_message_start", "tool_call",
    ], f"首订的结构性事件顺序/内容不对: {first_types}"
    assert "snapshot" not in first_types, (
        "无游标的首订收到了 snapshot —— 老前端不认识它，会静默丢弃"
    )

    # 重连：从第一条之后续订
    resumed = _collect_frames(bridge, RUN, last_event_id=ids[0])
    payloads = _payloads(resumed)
    types = [p["type"] for p in payloads]

    assert types[0] == "snapshot", f"重连首帧不是快照（实际={types}）"
    assert payloads[0]["content"] == "断线前写的断线后写的", (
        "快照不是全文 —— 断线期间生成的正文没有被补齐"
    )
    assert "ai_message_start" not in types, "续订重复下发了游标之前的事件"
    assert "tool_call" in types, "续订漏掉了游标之后的结构性事件"
    assert resumed.endswith("data: [DONE]\n\n"), "重连到已结束的 run 没有拿到 [DONE]"


# ─── ⑩ 防漂移：把"首帧降级"这条规则钉在生产代码里 ───────────────────────────
#
# §⑨ 的 `_collect_frames` 是**逐行复刻** `_sse_response._gen` 才成立的。复刻与被
# 复刻者一旦漂移（比如生产侧某次重构把 append_text_snapshot 参数去掉），§⑨ 会继续
# 全绿却与线上无关 —— 正是 wiring-contract-verify 要治的那类"测试自嗨"。


def _function(tree: ast.Module, name: str):
    return next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ),
        None,
    )


def test_sse_response_wires_append_snapshot_for_new_subscribers():
    """``_sse_response`` 必须把 ``append_text_snapshot`` 真的传给帧编码函数。"""
    func = _function(ast.parse(CHAT_SRC, filename=str(CHAT_PY)), "_sse_response")
    assert func is not None, "找不到 `_sse_response`"

    wired = [
        n.lineno
        for n in ast.walk(func)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_bridge_item_to_frame"
        and any(kw.arg == "append_text_snapshot" for kw in n.keywords)
    ]
    assert wired, (
        "`_sse_response` 没有向 `_bridge_item_to_frame` 传 append_text_snapshot —— "
        "新订阅者会收到 snapshot 帧并被未升级的前端静默丢弃（首订丢前缀正文）"
    )


def test_append_snapshot_is_limited_to_first_frame():
    """降级只能作用于**首帧**。

    之后的快照来自订阅者队列溢出后的重同步（RESYNC → `_catch_up`）—— 那时本地已经
    由实时增量累积了正文，再追加一次全文会让正文重复。生产侧靠 `_first` 标记表达这条
    约束，这里把它钉住。
    """
    func = _function(ast.parse(CHAT_SRC, filename=str(CHAT_PY)), "_sse_response")
    assert func is not None

    # 传参表达式里必须出现对 "_first" 的引用（`_first and _append_first_snapshot`）
    guarded = any(
        kw.arg == "append_text_snapshot"
        and any(isinstance(n, ast.Name) and n.id == "_first" for n in ast.walk(kw.value))
        for n in ast.walk(func)
        if isinstance(n, ast.Call)
        for kw in n.keywords
    )
    assert guarded, (
        "`append_text_snapshot` 没有被 `_first` 约束 —— 重同步快照也会被降级成追加，"
        "导致正文重复"
    )
