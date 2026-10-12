import asyncio
import json
import os
import re
import time
import uuid
import logging
import psycopg
from contextlib import aclosing
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Query, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from src.infra.database import message_key
from src.infra.sandbox import create_sandbox, get_env_snapshot
from src.infra.reports import snapshot_report_files
from src.services.agent import create_agent
from src.core import execution_guard, run_registry, turn_registry
from src.core.loop_detection import LOOP_FORCED_STOP_KEY
from src.core.mcp import get_mcp_tools
from src.core.terminal_response import fallback_notice, pop_fallback_signal, quick_mode_notice
from src.core.stream_bridge import (
    DELTA_REASONING,
    DELTA_TOKEN,
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    StreamDelta,
    StreamEvent,
    StreamGap,
    StreamSnapshot,
)
from src.api.deps import (
    get_store,
    get_checkpointer,
    get_current_user,
    get_stream_bridge,
)
from src.core.config import settings

logger = logging.getLogger(__name__)


# ─── 后台保存任务：脱离 SSE 取消作用域（2026-10-10）───────────────────────────
# 事故：会话 4863afff 断连后 store 里 **0 行**，服务端既无"保存完成"也无"保存失败"
#   （14:32:05.602977 打出 `[DIAG] SSE 流中断` 后一条日志都没有）。
# 根因：Starlette 的 StreamingResponse 在客户端断连时取消请求 task，断连 finally 里的
#   `await _persist_session(...)` 在**第一个 await** 就抛 CancelledError；而
#   `except Exception` 抓不到它（Py3.8+ CancelledError 继承 BaseException）→ 两条
#   日志全被跳过 → 静默 0 落库。这个 bug 2026-08-11、2026-09-30 各抓到过一次。
# 实测证据（tests/spikes/sse_disconnect_repro.py：真 uvicorn + 真 TCP RST 断开）：
#   direct  ：finally ENTER → enter → CancelledError → **无 WROTE**（复现事故）
#   detached：spawned → 156ms 后 **WROTE 42 msgs**（修复有效）
#   shield  ：外层 await 仍抛 CancelledError，内层虽跑完但**结果无法回传** → 不采用
# 结论：用 create_task 把保存协程交给事件循环独立承载 —— 新 task 不在任何取消
#   作用域内，原 task 被取消不会传染它。
_DETACHED_SAVES: set[asyncio.Task] = set()

#: worker 侧周期 beat 间隔（秒）。取 30 是为了远小于 turn_registry.STALE_MS（300s）——
#: 即使某一轮 beat 因为事件循环被同步 IO 短暂占住而迟到，也不会越过陈旧阈值。
#: ⚠ 为什么需要它：beat 原先挂在**每个 SSE chunk** 上，P1-b 之后 run 不再随连接存活，
#: 没有订阅者时那条驱动源就消失了（订阅侧的心跳跑在消费者里，无订阅者时不动）。
BEAT_INTERVAL_SECONDS = 30.0

#: run 终态后回收进程内态的宽限期（秒）。取值只需覆盖"前端重连的重试节奏"
#: （秒级~十秒级，含用户切走再切回前台的间隔）；留 90s 是为了不去猜前端的具体实现。
#: 更晚到来的订阅请求本就该走 GET /sessions/{sid}/active-run → 拿 null →
#: 回权威存储重取（P1-c），而不是指望进程内窗口还在。
RUN_REAP_DELAY_SECONDS = 90.0


def _on_detached_save_done(task: asyncio.Task) -> None:
    """后台保存的结果侧日志：成功打写入条数，失败打完整堆栈。

    存在的意义就是"不许静默"：原先失败与取消都不见天日，用户只看到历史凭空消失。
    """
    try:
        if task.cancelled():
            logger.error(
                "[PERSIST] 后台保存任务被取消（不应发生，本轮消息可能丢失）: %s",
                task.get_name(),
            )
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "[PERSIST] 后台保存失败（关键错误，本轮消息可能丢失）: %s",
                exc, exc_info=exc,
            )
            return
        logger.warning(
            "[PERSIST] 后台保存完成（断连兜底）: %s → 写入 %s 条",
            task.get_name(), task.result(),
        )
    except BaseException:
        # 回调自身异常绝不能向上冒（会打进 asyncio 的 unhandled 日志里，噪音且难查）
        logger.exception("[PERSIST] 后台保存结果回调自身异常")


def _spawn_detached_save(coro, *, label: str) -> asyncio.Task:
    """把保存协程剥离出当前取消作用域执行。

    ⚠ 调用方**不要** await 返回的 task —— await 会把它重新拉回取消作用域，
    等同没剥离。结果与失败一律由 `_on_detached_save_done` 回报。
    """
    task = asyncio.get_running_loop().create_task(coro, name=label)
    _DETACHED_SAVES.add(task)
    task.add_done_callback(_DETACHED_SAVES.discard)
    task.add_done_callback(_on_detached_save_done)
    return task


# ─── P1-b：run 与连接解耦后的线上协议 ─────────────────────────────────────────
# 桥（src/core/stream_bridge.py）只负责"把事件分发给订阅者"，**线上帧格式**由这里定。
#
# 硬约束：P1-b 上线时前端（geesun_agent_web）还没改（P1-c 才改），所以帧格式必须与
# 改造前**逐字兼容**：
#   · 结构性事件仍是 `data: {"type": ..., ...}`；正文/推理仍是
#     `data: {"type": "token"|"reasoning", "content": ...}`；
#   · 保活仍是 `": ping\n\n"`（SSE 注释行）；结束仍是 `data: [DONE]` ——
#     前端拿它当"正常结束"的**唯一**判据（lib/sse-client.ts 的
#     STREAM_TRUNCATED_MESSAGE：EOF 而无哨兵即判定传输被切断）。
# 新增两类帧（`snapshot` / `resync`）对现有前端安全：它的解析器只处理 `data: ` 开头的
# 行（其余一律 skip），且 reducer 的 `default:` 分支直接 `return slice`。


def _sse_frame(payload: dict, *, event_id: str | None = None) -> str:
    """编码一个 SSE 帧。`event_id` 写进 `id:` 行，客户端续订时作为 Last-Event-ID 回传。"""
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _bridge_item_to_frame(item, *, append_text_snapshot: bool = False) -> str | None:
    """桥的一帧 → SSE 帧。返回 None 表示"这一帧不必下发"。

    `append_text_snapshot`：**新订阅者**（本地没有任何正文）专用，见下。
    """
    if isinstance(item, StreamEvent):
        if item is HEARTBEAT_SENTINEL:
            # SSE 注释行：字节照传（保持 TCP 不被 proxy_read_timeout 掐断），前端忽略
            return ": ping\n\n"
        if item is END_SENTINEL:
            return "data: [DONE]\n\n"
        body = item.data if isinstance(item.data, dict) else {"data": item.data}
        return _sse_frame({"type": item.event, **body}, event_id=item.id)
    if isinstance(item, StreamDelta):
        return _sse_frame({"type": item.kind, "content": item.text})
    if isinstance(item, StreamSnapshot):
        if append_text_snapshot:
            # ─── 新订阅者：把"替换型"快照降级成两帧"追加型"文本 ───────────────
            # 桥的首帧恒为快照（替换语义）。对**续订者**替换是对的：它本地已有半截
            # 正文，需要被权威全文覆盖。但对**新订阅者**，P1-b 上线时的前端还不认识
            # `snapshot` 这个 type（P1-c 才加），会**静默忽略整帧** ⇒ 首订时已经生成
            # 的那段前缀正文永远看不到，用户看到的是"回答从中间开始"。
            #
            # 所以新订阅者改发老的追加型帧：字节形状与改造前逐字一致，任何版本的
            # 前端都能渲染；语义上也等价 —— 本地为空，"追加全文"就等于"全文"。
            #
            # 触发窗口为什么真实存在：worker 由 create_task 启动后，`bridge.subscribe`
            # 挂队列本身要 await condition 才生效 —— 这段时间里 worker 完全可能先
            # publish 出正文。靠时序碰巧不丢，不是保证。
            parts: list[str] = []
            # 顺序与实时流一致：先推理、后正文
            if item.reasoning:
                parts.append(
                    _sse_frame({"type": DELTA_REASONING, "content": item.reasoning})
                )
            if item.content:
                parts.append(_sse_frame({"type": DELTA_TOKEN, "content": item.content}))
            return "".join(parts) or None
        if not item.content and not item.reasoning:
            # 空快照没有信息量。首订时几乎恒为空（worker 刚起、还没有 token），此时
            # 下发只会给现有前端塞一个它不认识的 type；只有真带内容（= 订晚了 /
            # 断线重连）才下发。
            return None
        return _sse_frame({
            "type": "snapshot",
            "content": item.content,
            "reasoning": item.reasoning,
            "content_dropped_chars": item.content_dropped_chars,
            "reasoning_dropped_chars": item.reasoning_dropped_chars,
        })
    if isinstance(item, StreamGap):
        # 游标已被裁剪：正文由紧随其后的快照补齐，但结构性历史缺了一段。前端应据此
        # 回权威存储重取（P1-c），否则会把缺头的事件序列当成完整状态。
        return _sse_frame({
            "type": "resync",
            "requested_event_id": item.requested_event_id,
            "earliest_available_event_id": item.earliest_available_event_id,
            "latest_available_event_id": item.latest_available_event_id,
        })
    return None


def _sse_response(
    run_id: str,
    bridge,
    *,
    last_event_id: str | None = None,
    heartbeat_interval: float = 15.0,
) -> StreamingResponse:
    """把桥的订阅流包成 SSE 响应（P1-b 之后**唯一**的下发通道）。

    ⚠ `aclosing` 不能省：`async for` 里 break/return **不会**关闭异步生成器（CPython
    只在 aclose 或 GC 时跑 finally）。不关就留下"幽灵订阅者"——桥会一直往它的队列
    投递，白占内存。这是桥模块 docstring 明写的调用契约。
    """
    # 新订阅者（没带游标）本地一条正文都没有 ⇒ 首帧快照降级成老前端认得的追加型帧
    # （见 _bridge_item_to_frame 的 append_text_snapshot）。
    # 只对**首帧**生效：此后若再出现快照，那是订阅者队列溢出后的重同步（RESYNC →
    # _catch_up）—— 此时本地已由实时增量累积了正文，必须用替换语义，追加会让正文重复。
    _append_first_snapshot = last_event_id is None

    async def _gen():
        _first = True
        async with aclosing(
            bridge.subscribe(
                run_id,
                last_event_id=last_event_id,
                heartbeat_interval=heartbeat_interval,
            )
        ) as stream:
            async for item in stream:
                frame = _bridge_item_to_frame(
                    item, append_text_snapshot=_first and _append_first_snapshot
                )
                _first = False
                if frame is not None:
                    yield frame
                if item is END_SENTINEL:
                    return

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 禁用 nginx/openresty 缓冲，保证 SSE 实时逐字节下发（配合心跳保活）
            "X-Accel-Buffering": "no",
            # 本次 run 的 id（P1-c）。前端的「停止查看 / 终止任务 / 继续查看」都需要它，
            # 而 `GET /active-run` 读的是 store 行 —— `run_registry.persist` 刻意吞掉
            # 写失败（见其 docstring），那种情况下 active-run 返回 null，前端就再也
            # 定位不到这个 run。响应头是"我刚起的这一轮"的直接来源，不受写库影响。
            # ⚠ 跨源（开发态前端直连 :8009）需要 server.py 的 CORS expose_headers。
            "X-Run-Id": run_id,
        },
    )


def _log_worker_done(task: asyncio.Task) -> None:
    """worker 的结果侧日志：不许静默（否则"这一轮为什么没结果"完全不可见）。"""
    try:
        if task.cancelled():
            logger.warning("[RUN] worker 被取消: %s", task.get_name())
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "[RUN] worker 异常退出（本轮可能没有结果）: %s", exc, exc_info=exc
            )
    except BaseException:
        logger.exception("[RUN] worker 结果回调自身异常")


async def _reap_run_later(run_id: str, bridge, delay: float) -> None:
    """宽限期后回收该 run 的**进程内**态：桥的窗口 + run 登记表记录。

    为什么必须回收：桥的正文累积器上限是 200k 字符 × 2 类（token / reasoning），
    即单个 run 最多常驻约 400KB，且**每轮一份**。不回收就是随轮数线性增长的内存
    泄漏（run 记录本身很小，但同理没有任何理由让它常驻）。
    为什么必须**延迟**：前端断线重连是秒级节奏，"刚结束就重连"属正常路径（用户切
    走又切回前台）。就地回收会让这次重连拿到 404 / 空流，而它本来能靠桥的重放
    窗口拿到完整尾部 + `[DONE]`。
    两步之间没有 await 让出点 ⇒ 外部只会观察到"两条都在"或"两条都不在"，不会撞见
    "记录没了但桥窗口还在"的半回收态。
    """
    await asyncio.sleep(delay)
    run_registry.drop(run_id)
    await bridge.cleanup(run_id)


def _log_reap_done(task: asyncio.Task) -> None:
    """回收任务的结果侧日志：失败必须可见（静默 = 垃圾没扫掉却无人知晓，即泄漏）。"""
    try:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "[RUN] 终态回收失败（进程内态未释放，构成内存泄漏）: %s",
                exc, exc_info=exc,
            )
    except BaseException:
        logger.exception("[RUN] 回收结果回调自身异常")


def _clip(text: str, limit: int) -> str:
    """按配置上限裁剪文本；limit <= 0 表示不限制（2026-09-12 新增）。

    store 回放副本与 SSE tool_result 的长度上限统一走此函数，
    替代此前散落在 _persist_session / 事件流里的 [:2000] 硬编码。
    """
    if limit and limit > 0 and len(text) > limit:
        return text[:limit]
    return text


def _strip_injected_path_hint(content: str) -> str:
    """去掉 user 消息里注入的 `path_hint` 前缀（回放副本专用）。

    原先是内联在 `_persist_session` 的 message 循环里（`if role == "user" and
    "\\n\\n" in content`）。P1-c 的 setup 预写（见 `_write_setup_user_message`）也要做
    同一件事，而**两处必须逐字一致**：预写进去的条目会被流尾的水印视为"已归档"、
    永不再被改写，任何偏差都会直接变成用户看到的正文差异。

    判据是"按最后一个空行切、取后半段"（历史沿革，未改语义）：`path_hint` 与
    `body.message` 之间正是用 `\\n\\n` 分隔的，所以切完剩下的就是用户真正输入的内容。
    注意该判据对"用户自己在正文里打了空行"的消息同样生效（只保留最后一段）——
    这是既有行为，本次只做提取、不改判据；要修需单独一轮并带迁移。
    """
    if "\n\n" not in content:
        return content
    parts = content.rsplit("\n\n", 1)
    return parts[-1].strip() if len(parts) > 1 else parts[0].strip()


def _turn_start_index(history: list[dict]) -> int:
    """本轮区间的起点下标（= 最后一条 user 消息的下标 + 1）。

    找不到 user 消息时（history 被摘要压缩截断、或系统续跑没有用户消息）返回 0 ——
    即退化为「整份 history」= 旧口径。此处刻意保留该退化：无法判定轮次边界时沿用
    已在生产跑过的旧口径，不会比它更差。

    纯函数，便于 spike 用 ast 提取后直接断言。
    """
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user":
            return i + 1
    return 0


def _last_ai_index_for_turn(history: list[dict]) -> int | None:
    """「本轮」最后一条 AI 消息在 history 里的下标；本轮没有 AI 产出则返回 None。

    「本轮」= **最后一条 user 消息之后**（不含该 user）的那一段（见 `_turn_start_index`）。

    为什么不能对整份 history 直接倒序找 AI（2026-10-12 修）：`history` 是
    `_persist_session` 从 checkpoint **重建的全量快照**，不止本轮。改前写法是
    `for i in range(len(history) - 1, -1, -1)` 找第一条 `role == "ai"`，于是本轮
    没有 AI 产出时（模型未产出即断连 / 被取消，走 interrupted 保存路径）会命中
    **上一轮**的 AI，把本轮耗时写到上一轮那条上——实测：上一轮自己的 4500ms
    被改写成本轮的 1000ms（`tests/spikes/turn_duration_per_turn.py` S3）。

    纯函数，便于 spike 用 ast 提取后直接断言。
    """
    start = _turn_start_index(history)
    for i in range(len(history) - 1, -1, -1):
        if i >= start and history[i].get("role") == "ai":
            return i
    return None


def _mark_turn_last_ai(history: list[dict], fields: dict) -> int | None:
    """把 `fields` 写到**本轮**最后一条 AI 条目上，返回其下标；本轮无 AI 产出则返回 None。

    三处「本轮事实」共用这一个落点：`turn_duration_ms`、`completion`
    （`blocked_no_output` / `model_output_truncated`）、`completion` 配套的 `stop_reason`。

    为什么要收敛到一处（2026-10-12）：这三处原先各自内联「在整份 history 里倒序找
    第一条 AI」。那份 history 是**全量快照**（见 `_last_ai_index_for_turn` 文档），
    于是本轮无 AI 产出时三者都会打到**上一轮**的 AI 上。只修其中一处（如只修耗时）
    会留下「耗时对了、徽标还错位」的半修状态——同一个缺陷面被修了一半，比不修更
    容易骗过复盘。故统一走本函数，判据单一来源。

    返回 None 时**什么都不写**：宁可这一轮没有耗时值 / 没有徽标，也不给错的归属。
    调用方应据此留 WARNING 便于观测（`completion` 那样的事实缺失是复盘信号，不该静默）。
    """
    idx = _last_ai_index_for_turn(history)
    if idx is None:
        return None
    history[idx].update(fields)
    return idx


def _iso_from_ms(ms: int) -> str:
    """毫秒时间戳 → 带时区的 ISO 字符串（与 entry 其余时间字段同一格式）。"""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _interrupted_turn_fields(
    turn_started_at_ms: int | None,
    turn_ended_at_ms: int | None,
) -> dict:
    """P0′（2026-10-12）：断连/取消路径写进**本轮**最后一条 AI 的"本轮事实"。

    为什么不能复用正常路径的 `turn_duration_ms`：断连时刻不是任务终点。
    `turn_ended_at_ms` 在断连路径上取的是"持久化时刻"（`chat.py` 内层 finally），
    于是 `duration = 断连时刻 − 流建立时刻` 实际度量的是 **"用户刷新得有多快"** ——
    会话 `GY24428:5f65f8df` 落下的 `turn_duration_ms=664703`（11m 4.7s）就是这么来的：
    前端据此渲染"已完成 · 11m 5s"，而那一轮**一个交付物都没有**。数据层没有"中断"
    这个状态可落，就只能落成"完成"。

    故中断路径改为：
      · `turn_status="interrupted"` —— 数据层第一次能表达"这轮没跑完"；
      · `turn_started_at` —— 保留起点，前端可显示"已进行 X 分钟"（而非"耗时 X"）；
      · `turn_ended_at` —— 连接断开时刻，语义是"观察窗结束"，不是任务结束。

    纯函数，便于 spike 用 ast 提取后直接断言（与 `_mark_turn_last_ai` 同族）。
    """
    fields: dict = {"turn_status": "interrupted"}
    if turn_started_at_ms is not None:
        fields["turn_started_at"] = _iso_from_ms(turn_started_at_ms)
    if turn_ended_at_ms is not None:
        fields["turn_ended_at"] = _iso_from_ms(turn_ended_at_ms)
    return fields


def _reasoning_duration_fields(
    *,
    interrupted: bool,
    reasoning_started_at_ms: int | None,
    reasoning_ended_at_ms: int | None,
) -> dict:
    """本条 AI 消息的推理时长/起点字段（纯函数，便于 spike 直接断言）。

    正常路径：两个时间戳都有值才写 `reasoning_duration_ms` + `reasoning_started_at`。
    中断路径（P0′）：`reasoning_ended_at_ms` 未必是"推理真的结束"，也可能是断连兜底
    时刻 —— 拿它算时长会把"用户刷新得多快"当成"推理秒数"（与 `turn_duration_ms`
    同源缺陷）。故中断时**只写起点、不写时长**：宁可这一段没有确数，也不给错数。
    前端 ReasoningBlock 拿 `reasoning_started_at` 仍能稳定锚定与自行计时。
    """
    if reasoning_started_at_ms is None:
        return {}
    if interrupted:
        return {"reasoning_started_at": _iso_from_ms(reasoning_started_at_ms)}
    if reasoning_ended_at_ms is None:
        return {}
    return {
        "reasoning_duration_ms": max(0, reasoning_ended_at_ms - reasoning_started_at_ms),
        "reasoning_started_at": _iso_from_ms(reasoning_started_at_ms),
    }


def _attach_generated_files(
    history: list[dict], generated_files: list[dict]
) -> list[dict]:
    """把本轮产出关联到**发起该工具调用**的那条 AI 上；返回实际写入的条目列表。

    归属优先级（2026-10-12 重写）：

      1. **`tool_call_id` 精确匹配**——本轮区间内某条 AI 的 `tool_calls[].id` 命中，
         它就是产出该文件的工具调用的发起者。这与流式阶段逐字一致：`file_generated`
         紧跟 tool_call 之后到达，前端 `attachGeneratedFile` 挂到"当时最后一条 AI"，
         那条正是发起者（id 同源：ToolMessage.tool_call_id ↔ AIMessage.tool_calls[].id，
         落盘第一遍建 `tool_results` 映射用的就是同一个键）。
      2. 无 id / id 匹配不上 → 本轮**最后一条带 tool_calls** 的 AI。
      3. 本轮完全没有带 tool_calls 的 AI → 本轮最后一条 AI。
      4. 本轮**没有任何 AI** → 什么都不写（返回空列表），绝不挂到上一轮。

    为什么不用「第一条带 tool_calls 的 AI」（改前实现）：那份 history 是全量快照，
    「第一条」跨轮会命中**最早那轮**；即使只看本轮，多段工具调用（write_file →
    download_from_sandbox → …）时它也会把所有文件堆到**第一段**上，而流式阶段是按段
    分散显示的——刷新后卡片"重排到一条气泡下"就是这么来的（`tests/spikes/
    turn_duration_per_turn.py` S6 有该场景）。改前的注释声明的意图其实正是
    「发起 tool_call 的那条」，但实现没做到；本函数是把该意图按 id 精确实现。

    `tool_call_id` 只是**本函数内部**的归属锚，写进 entry 前会剥离，不进入对前端的
    数据契约（`GeneratedFile` 字段不变）。

    纯函数，便于 spike 用 ast 提取后直接断言。
    """
    if not generated_files:
        return []

    start = _turn_start_index(history)
    turn_ai_idxs = [
        i for i in range(start, len(history)) if history[i].get("role") == "ai"
    ]
    if not turn_ai_idxs:
        return []

    # tool_call_id → 本轮 AI 下标（首次出现者，同一 id 不会跨 AI 重复）
    by_tool_call: dict[str, int] = {}
    for i in turn_ai_idxs:
        for tc in history[i].get("tool_calls") or []:
            tcid = tc.get("id") if isinstance(tc, dict) else None
            if tcid:
                by_tool_call.setdefault(str(tcid), i)

    fallback_with_tc = next(
        (i for i in reversed(turn_ai_idxs) if history[i].get("tool_calls")), None
    )
    fallback_any = turn_ai_idxs[-1]

    pending: dict[int, list[dict]] = {}
    for f in generated_files:
        tcid = f.get("tool_call_id")
        idx = by_tool_call.get(str(tcid)) if tcid else None
        if idx is None:
            idx = fallback_with_tc if fallback_with_tc is not None else fallback_any
        entry = {k: v for k, v in f.items() if k != "tool_call_id"}
        pending.setdefault(idx, []).append(entry)

    for idx, items in pending.items():
        history[idx].setdefault("generated_files", [])
        history[idx]["generated_files"].extend(items)
    return [e for items in pending.values() for e in items]


def _plan_message_append(
    history: list[dict],
    prev_count: int,
    last_stored_id: str | None,
) -> tuple[int, list[dict], str]:
    """算出增量追加的起始序号与待写条目（纯函数，便于 spike 直接断言）。

    返回 `(next_seq, new_items, mode)`：
      next_seq   本条消息的起始 store 序号（= 已存最大序号 + 1）
      new_items  本次要写的新条目（可能为空）
      mode       定位方式，仅用于日志与断言：`by_id` / `by_length` / `none`

    ## 为什么需要"按 id 定位"而不是"按长度"

    增量追加的朴素前提是"history 只在尾部增长"。但 `SummarizationMiddleware` 触发时
    返回 `[RemoveMessage(REMOVE_ALL_MESSAGES), 摘要, *保留的尾部]`——**history 会变短**。
    此时若仍按 `len(history) > prev_count` 判断，压缩后 len 反而更小 → 判为"无新增"，
    当轮之后的新消息**永远写不进去**；反过来若按 history 下标算 key，压缩后 history
    里第 10 条其实是 store 里第 250 条 → **覆盖已归档条目**。

    所以用 store 里最大序号那条的 `id`，在 history 里反查它的位置，从其后开始追加；
    序号继续沿用 **store 的**序号（不是 history 下标），两头都不会重叠。
    摘要消息本身**不写入 store**：store 是给人看的回放副本，已保留完整原文，
    摘要只是"喂给模型的面"的产物，写进来反而会把原文挤掉（正是旧 bug 的形态）。

    ## 退化路径

    `last_stored_id` 为 None（老数据 / 无 id 构造路径）或 id 在 history 里找不到
    （历史被改写、条目无 id）→ 退回按长度判断。此时若 history 没有更长，
    本次什么都不写并交由调用方打 warn——**宁可少写一轮，不可改写已归档内容**。
    """
    next_seq = prev_count + 1

    start: int | None = None
    mode = "none"
    if last_stored_id:
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("id") == last_stored_id:
                start = i + 1
                mode = "by_id"
                break

    if start is None:
        if len(history) > prev_count:
            start = prev_count
            mode = "by_length"
        else:
            start = len(history)
            mode = "none"

    return next_seq, history[start:], mode


def _warn_oversized_items(items: list[dict], *, prefix: str) -> None:
    """单条 entry 体积观测闸——**只打日志，不截断、不丢弃**（2026-09-12 方案 B）。

    持久化层截断 = 不可逆删除（5df629e 的教训），所以这里只做信号。
    依据：生产实测单条最大 278 KB（AI 正文 272833 字符）→ 阈值 1 MB 留 3.6× 余量。
    """
    limit = settings.persist_max_item_bytes
    if limit <= 0:
        return
    for entry in items:
        try:
            size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as e:
            logger.warning("[DIAG] 单条 entry 无法序列化度量: %s (prefix=%s)", e, prefix)
            continue
        if size > limit:
            logger.warning(
                "[DIAG] 单条消息体积 %d 字节超过观测阈值 %d（未截断，仅告警）: "
                "prefix=%s role=%s id=%s",
                size, limit, prefix, entry.get("role"), entry.get("id"),
            )


async def _write_setup_user_message(
    store,
    *,
    user_id: str,
    session_id: str,
    message_id: str,
    content: str,
) -> bool:
    """setup 阶段把本轮 user 消息先写进 store，返回是否真的写入（P1-c D5）。

    ## 为什么必须提前写

    `_persist_session` 只在**流结束**时落盘。所以在此之前，"第 2 轮跑到一半刷新"
    会撞上两个同源现象：

    · `GET /sessions/{sid}/messages` 返回的是**上一轮**的消息 —— 用户刚问的问题凭空消失；
    · 续订时桥给新订阅者的首帧是追加型 `token`/`reasoning` 帧，前端会把正文追加到
      最后一条 AI 消息上，也就是**上一轮的 AI** ⇒ 正文串轮。

    两者的根因都是"本轮 user 消息还不存在"。补上它，续订才有正确的落点。

    ## 为什么能安全重入（不会写重复、也不会被改写）

    `message_id` 是我们生成的 uuid，并作为 `HumanMessage(id=...)` 显式喂进图。
    实测（`.workbuddy/_probe_msg_id.py`，三组对照）：

    · 显式 id 会被 LangGraph **原样保留**在 checkpoint 里；
    · dict 形式（改造前的写法）则由框架生成 uuid，控制不了；
    · 同 id 重发在 `add_messages` 下是**替换**而非追加。

    于是流尾的 `aget_message_watermark` 拿到的 `last_stored_id` 正是它，
    `_plan_message_append` 从它**之后**开始追加 ⇒ 只补写 AI 消息；本条既不会被
    重复写，也不会被流尾用另一份 content 改写。

    ⚠ 这条 id **不是可选装饰**。预写会把水位的 `last_stored_id` 从"上一轮的 AI 消息"
    换成"我们刚写的这一条"，所以这一条必须能被 `_plan_message_append` 在 history 里
    认出来 —— 否则水印会**永久退化成 by_length**，等于亲手关掉了那条专门对抗
    `SummarizationMiddleware` 压缩的 by_id 路径。压缩后 `len(history)` 小于
    `prev_count`，by_length 判为"无新增"，本轮 AI 回答**一条都写不进去**：没有异常、
    没有日志，只有用户刷新后看不到回答。负向对照见
    `tests/api/test_setup_user_message.py::test_missing_id_loses_turn_when_history_compressed`。

    若 setup 这次没写成（DB 瞬时故障 / 存量仍是旧格式），流尾会退化成按 id 定位到
    上一轮的条目，把本轮 user + AI 一起补上 —— 与改造前一致，不丢数据。

    ## 调用方必须先登记 turn_registry

    删会话端点的护栏查的是 `turn_registry.is_active`。若等 worker 起来才登记，
    删除请求可以抢在本次预写之前完成 → 我们随后把消息写进一个**已被删除**的会话，
    留下孤儿行（`session_existed_at_start` 守卫只能拦住"流尾再写"，拦不住这一次）。
    所以 chat 端点在本函数**之前**就 acquire，见那里的注释。
    """
    namespace = ("messages", user_id, session_id)
    prefix = ".".join(namespace)
    try:
        prev_count, _last_id, legacy_rows = await store.aget_message_watermark(prefix)
    except Exception:
        logger.exception("[SETUP] 读取消息水位失败，跳过 setup 预写: session=%s", session_id)
        return False

    if legacy_rows:
        # 与 _persist_session 同一道部署顺序防线：存量还是旧格式时绝不写入 ——
        # 新旧格式并存会让迁移脚本的前置守卫拒绝执行，留下需要人工对账的中间态。
        logger.warning(
            "[SETUP] 会话 %s 存量仍为旧格式（%d 条），跳过 setup 预写",
            session_id, legacy_rows,
        )
        return False

    entry = {
        "id": message_id,
        "role": "user",
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _warn_oversized_items([entry], prefix=prefix)
    try:
        await store.awrite_messages(
            namespace, [(message_key(prev_count + 1), entry)]
        )
    except Exception:
        # 预写失败不阻断本轮：消息最终由流尾的 _persist_session 兜底落盘。
        logger.exception(
            "[SETUP] 预写本轮 user 消息失败（不阻断，流尾会兜底）: session=%s", session_id
        )
        return False

    logger.warning(
        "[SETUP] 已预写本轮 user 消息: session=%s key=%d id=%s content=%d 字符",
        session_id, prev_count + 1, message_id, len(content),
    )
    return True


async def _aget_state_with_retry(agent, thread_id: str, max_attempts: int = 3):
    """带重试地读取 agent 最终状态（防御性）。

    断连兜底路径（reason="interrupted"）下，agent 图可能仍在写 checkpoint。
    原单连接模式会在此撞 "another command is already in progress" 而静默丢消息
    （server.log 16:30:09 checkpointer.aget_tuple 失败）。底层已换连接池后该碰撞
    不再发生，此处仍加短暂重试作为纵深防御，避免瞬时争用直接丢本轮消息。
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await agent.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
        except (
            psycopg.OperationalError,
            psycopg.InterfaceError,
            psycopg.ProgrammingError,
        ) as e:
            last_exc = e
            logger.warning(
                "[DIAG] aget_state 第 %d/%d 次失败（%s），重试...",
                attempt, max_attempts, e,
            )
            await asyncio.sleep(0.2 * attempt)
    logger.error(
        "aget_state 重试 %d 次仍失败，放弃读取最终状态: %s",
        max_attempts, last_exc,
    )
    raise last_exc


def _tool_intent_sig(tool_name: str, result_str: str) -> str:
    """提取工具调用意图指纹：工具名 + 结果内容的关键特征。

    用于 P0 无进展检测——判断模型是否在重复执行同一件事。
    - execute：取结果前 60 字符（命令相同 → 结果通常相同，指纹稳定）
    - 其他工具：取结果前 80 字符
    结果为空时退化为纯工具名（同工具重复也算重复意图）。
    """
    s = (result_str or "").strip()
    if tool_name == "execute":
        return f"{tool_name}:{s[:60]}"
    return f"{tool_name}:{s[:80]}" if s else tool_name


def _missing_skill_artifacts(
    report_root: str,
    user_id: str,
    session_id: str,
    stage1_done: bool,
    stage3_done: bool,
) -> list[str]:
    """确定性校验 skill 工作流关键产物（M3 扩展，2026-08-19）。

    本轮跑过 run_pdf_diff_stage1/stage3（成功）后，reports 必须出现对应产物
    （agent 用 download_from_sandbox 拉回）。缺失即视为"流程未走完"，
    由 M3 完成门注入收敛提示继续，不信任模型自陈"做完了"。

    Returns: 缺失项描述列表（空 = 齐）。
    """
    missing: list[str] = []
    rp = os.path.join(report_root, user_id, session_id)
    if stage1_done and not os.path.isfile(os.path.join(rp, "diff_pages.json")):
        missing.append(
            "diff_pages.json（run_pdf_diff_stage1 产物，需 download_from_sandbox 拉回再读）"
        )
    if stage3_done:
        try:
            has_report = any(
                n.startswith("技术协议差异对比报告_")
                and n.endswith((".md", ".html"))
                for n in os.listdir(rp)
            ) if os.path.isdir(rp) else False
        except OSError:
            has_report = False
        if not has_report:
            missing.append(
                "技术协议差异对比报告_*（run_pdf_diff_stage3 产物，需 download_from_sandbox 拉回）"
            )
    return missing


class ToolLoopAbortError(Exception):
    """工具连续失败超限，提前终止本轮流式生成。

    与 GraphRecursionError 的区别：后者要烧满 recursion_limit（100 步）才抛，
    期间模型会反复空转；本异常在连续失败达到阈值时立刻抛出，
    由 _drain_astream 的 except 分支捕获后转为 SSE error 事件，秒级止损。
    """


class NoProgressAbortError(Exception):
    """工具"成功但无进展"循环检测，中断当前轮并触发收敛注入。

    与 ToolLoopAbortError 的区别：那个管"工具连续失败"（错误循环），
    本异常管"工具全部成功但模型反复执行同一意图、零新交付物"的空转
    （2026-08-17/08-18 两次 Recursion limit 死循环实证：execute 全 OK、
    报告已生成，但模型不断"重新构建 diff.json"永不收敛）。
    由外层捕获后注入收敛 SystemMessage，让模型收敛交付而非烧满 200 步。
    """


# 与前端 lib/types.ts inferFileType 保持一致
# 用于保存 generated_files 时填充 file_type 字段
_FILE_TYPE_BY_EXT = {
    "md": "text", "txt": "text", "log": "text",
    "py": "code", "js": "code", "ts": "code", "tsx": "code",
    "css": "code", "html": "code", "json": "code",
    "yaml": "code", "yml": "code", "sh": "code",
    "java": "code", "go": "code", "rs": "code",
    "c": "code", "cpp": "code", "h": "code",
    "png": "image", "jpg": "image", "jpeg": "image",
    "gif": "image", "svg": "image", "webp": "image",
    "bmp": "image", "ico": "image",
    "pdf": "pdf",
    "xlsx": "spreadsheet", "xls": "spreadsheet", "csv": "spreadsheet",
    "zip": "archive", "tar": "archive", "gz": "archive",
    "7z": "archive", "rar": "archive",
}


def _infer_file_type(file_name: str) -> str:
    ext = file_name.split(".")[-1].lower() if "." in file_name else ""
    return _FILE_TYPE_BY_EXT.get(ext, "other")


def _tool_result_is_error(
    tool_name: str, content_str: str, status: str | None = None
) -> bool:
    """判定一条工具结果是否失败（决定 SSE 的 success 字段与文件解析准入）。

    优先级：**框架的 `ToolMessage.status` > 内容启发式**。

    为什么必须让 status 优先（2026-09-29 根因修复）：
    execute 通道护栏（`agent.py` 的 `ValidatedCompositeBackend.execute`）拒绝越界命令时
    抛 `ValueError` → deepagents 工具层回
    `ToolMessage(status="error", content="Error: Invalid parameter. {拒绝文案}")`。
    这段内容**既不含** "command failed with exit code"、**也不以** "Execution error:"
    开头 → 旧的 execute 关键词分支判 `is_error=False` → 拒绝结果被当**成功**结果送进
    文件解析 → 护栏文案里的 `/reports/<uid>/<sid>/<文件名>` 被当路径提取 → 产出一张
    `file_size=0` 的幽灵卡片（2026-09-29 活体 e2e 实测，正是本次要消灭的那类病）。
    status 是框架权威字段，有它就不该再猜。

    关键词兜底仍保留（status 缺失/老版本 SDK 时用），并保持原语义：
    - 结构化 JSON：只看 `success` 字段，命中即返回，**不落关键词**（历史上
      `"error": null` 撞上宽泛关键词把成功标成失败）；
    - `read_file` 是内容型工具，成功返回的正文里可能含 "No such file"（如 SKILL.md 里
      的示例文本）→ 只认 deepagents 失败时必带的 "Error: " 前缀。
    """
    if status == "error":
        return True
    if not content_str:
        return False
    try:
        parsed = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        pass
    else:
        # 解析成 JSON 了：只认 success 字段（非 dict / 无该字段 → 视为无失败信号）
        return isinstance(parsed, dict) and "success" in parsed and not parsed["success"]
    lower = content_str.lower()
    if tool_name == "execute":
        return (
            "command failed with exit code" in lower
            or content_str.startswith("Execution error:")
        )
    if tool_name == "read_file":
        return content_str.startswith(("Error: ", "error: "))
    return any(
        marker in lower
        for marker in (
            "exception", "traceback", "failed", "failure",
            "timeout", "permission denied", "no such file",
        )
    )


# 从工具结果文本里抠 /reports/ 路径（write_file 之外的工具：execute / download_from_sandbox
# 的 JSON 或 shell 回显）。字符类比 \S+ 保守：到引号/空白/逗号/花括号/方括号为止，
# 否则会把 JSON 尾巴（`","size":15758`）或 shell 报错的 `':` 一起吞进路径 → 前端 404。
# 中文不在 \w 里，故用显式字符类而非 \w。
_REPORTS_PATH_RE = re.compile(r'/reports/[^"\s,}\]]+')


def _sanitize_generated_path(file_path: str) -> str:
    """清洗工具返回的文件路径（对称前端 deriveGeneratedFiles 清洗，2026-08-24 根治脏路径）。

    上游（模型/技能工具）返回的 /reports/ 路径可能尾随单引号/空白等脏字符——
    2026-08-24 实测 tech-spec-pdf-diff 返回 `.../diff.json'`（尾随单引号）：
    磁盘上根本不存在该文件 → 后端 emit file_generated → 前端 HEAD 探测 404
    → 红框"文件不可用"卡，连累整组报告卡片的预览/下载观感。
    前端 deriveGeneratedFiles 只过滤 `file_path.endsWith("/")`，漏掉尾随引号，
    故在后端 emit 前统一清洗（去首尾引号/空白）。清洗后再走磁盘查找/去重/emit，
    `diff.json'` 自动归正为 `diff.json`（磁盘存在 → 正常预览下载，且与真实条目去重）。

    2026-09-29 扩展（B① 前置条件）：追加剥离 `:` `;` `,` `` ` `` —— shell 报错回显里的
    路径常被"引号 + 冒号"包住，形如
        cp: cannot create regular file '/reports/u/s/x.pptx': No such file or directory
    而提取路径的正则 `[^"\\s,}\\]]+` 不把 `'` 当分隔符，会把尾部的 `':` 一起吞进路径
    （实测得到 `/reports/u/s/x.pptx':`）。不清掉这一步，后面的沙箱候选路径（
    /home/user/x.pptx）永远对不上 → B① 的"从沙箱拉回"必然失败。
    """
    if not file_path:
        return file_path
    return file_path.strip().strip("'\"` \t:,;")


async def _pull_report_from_sandbox(
    sandbox, file_path_virtual: str, user_id: str, session_id: str, filename: str
) -> int:
    """沙箱产物拉回宿主持久卷（2026-09-29 方案A：file_generated 兜底持久化）。

    背景（GY35377/60c64c4f 项目团队任命书.pptx 实测）：模型在沙箱内 execute
    生成的产物（python-pptx 等）落在**沙箱文件系统**，未走 download_from_sandbox
    拉回宿主；沙箱 5min 空闲 TTL 被 lifecycle-manager 回收后文件随之消失，
    但 file_generated 事件已按模型声称的 /reports/ 路径推送 → 前端 HEAD 404
    → 红框"文件不可用"。write_file 写 /reports/ 走 FilesystemBackend 直写
    持久卷本无此问题，漏网的只是"沙箱内 execute 产物"。

    策略：emit 前发现磁盘缺失（file_size==0）时，按候选路径从沙箱读回字节
    并落盘到 settings.report_root/{user}/{session}/——生成瞬间即持久化，
    之后沙箱回收无所谓，也不再依赖模型自觉调 download_from_sandbox。

    候选沙箱路径（按命中率排序）：
      1. 模型声称的虚拟路径本身（沙箱内 mkdir -p /reports/... 后写同一绝对路径）
      2. /home/user 下的相对写法（execute CWD 多为 /home/user）
      3. /home/user/{basename}、/tmp/{basename}（脚本就近输出）

    返回落盘后的文件大小；任何失败返回 0（不阻断 SSE 流，退化为现状 404，
    不会比修复前更差）。沙箱已回收时 download_files 全 file_not_found → 0。

    ⚠️ 必须 await + to_thread（2026-09-29 加固，随 1.0.20 发布）：
    `sandbox.download_files` 是**同步阻塞**方法（内部走网络读沙箱文件），直接调在
    async 生成器里会占住事件循环——沙箱已回收时（每条候选都要等到超时才返回）
    整个 worker 会僵住，**其他用户正在跑的 SSE 流一起卡**，不只影响当前会话。
    本仓既有约定见 `src/infra/trash.py:96`：「同步阻塞函数，调用方须用
    asyncio.to_thread 包住」。
    同时改为**逐条候选 + 命中即短路**：库内 `download_files` 会遍历传入的全部路径，
    原来一次传 4 条 = 无条件读 4 次；现在首条命中即停，省往返也缩短最坏路径。
    """
    if sandbox is None or not filename:
        return 0
    base_name = filename.rsplit("/", 1)[-1]
    # dict.fromkeys 去重且保序：候选 1 与候选 2 常常是同一条路径（模型照抄虚拟路径）
    candidates = list(
        dict.fromkeys(
            [
                file_path_virtual,
                f"/home/user/reports/{user_id}/{session_id}/{filename}",
                f"/home/user/{base_name}",
                f"/tmp/{base_name}",
            ]
        )
    )
    content = None
    tried: list[str] = []
    for cand in candidates:
        tried.append(cand)
        try:
            # to_thread：同步阻塞方法，绝不能在事件循环里直调（见 docstring）
            responses = await asyncio.to_thread(sandbox.download_files, [cand])
        except Exception as e:  # noqa: BLE001 — 断连/回收：换下一条候选，不重试同一路径
            logger.warning("[FILE_GEN] 沙箱拉回异常: path=%s err=%s", cand, e)
            continue
        for resp in responses or []:
            c = getattr(resp, "content", None)
            if c:
                content = c
                break
        if content:
            break
    if not content:
        logger.warning(
            "[FILE_GEN] 沙箱内未找到产物（可能沙箱已回收）: tried=%s", tried
        )
        return 0
    # 路径穿越防御（与 files.py 同款）：落盘必须限制在 report_root/uid/sid 内
    root = os.path.normpath(os.path.join(settings.report_root, user_id, session_id))
    disk_path = os.path.normpath(os.path.join(root, filename))
    if disk_path != root and not disk_path.startswith(root + os.sep):
        logger.warning("[FILE_GEN] 拉回路径越界拦截: %s", disk_path)
        return 0
    try:
        os.makedirs(os.path.dirname(disk_path), exist_ok=True)
        with open(disk_path, "wb") as f:
            f.write(content)
    except Exception as e:  # noqa: BLE001
        logger.warning("[FILE_GEN] 拉回写盘失败: %s err=%s", disk_path, e)
        return 0
    logger.info(
        "[FILE_GEN] 沙箱产物已拉回宿主: %s (%d bytes)", disk_path, len(content)
    )
    return len(content)


# ─── 沙箱交付物兜底回收（2026-09-29 方案 B②）───
# 触发场景：模型用 execute 在沙箱内生成交付物（python-pptx / pandas.to_excel 等），
# 却从未调用 download_from_sandbox 拉回宿主 → 宿主报告目录零产出，沙箱 5min TTL
# 回收后文件永失（GY35377/60c64c4f 项目团队任命书.pptx 实测）。
#   B① 按"工具结果里出现的路径"救（要求模型说得出路径）；
#   B② 按"沙箱文件系统差集"救（模型连路径都没说出来也能救）——本节即 B②。
#
# 只回收"像交付物"的扩展名：脚本/日志/缓存/依赖不该变成用户面前的报告卡片
# （模型常写 generate_ppt.py / output.log，那不是交付物）。
_SALVAGE_EXTS = (
    "md", "txt", "pdf", "docx", "doc", "xlsx", "xls", "csv", "pptx", "ppt",
    "html", "htm", "json", "xml", "yaml", "yml", "png", "jpg", "jpeg", "svg", "zip",
)
# 沙箱内产物高发目录：execute 的 CWD 默认 /home/user；脚本常写 /tmp；
# 模型被误导后还会自己 mkdir -p /reports/...（**沙箱内的同名目录**，不是宿主卷）
_SALVAGE_DIRS = ("/home/user", "/tmp", "/reports")
# 单轮兜底最多回收几个：避免一次性把沙箱垃圾全搬进来刷屏
_SALVAGE_MAX_FILES = 5
# B①：是否也解析"失败的工具结果"里的交付物路径。
# 报错文本里的 /reports/... 也可能指向真实产物（cp 因目标目录不存在而失败，源文件仍在），
# 跳过解析等于连"试着拉回"的机会都没有。安全性由 emit 护栏承担：
# **报错来源的路径只有真正拉回成功（file_size > 0）才允许 emit**（见下方 B① 护栏）。
_PARSE_ERROR_TOOL_RESULTS = True


def _sandbox_artifact_listing(sandbox) -> dict[str, int]:
    """列沙箱候选目录下的交付物候选 {绝对路径: 字节数}（**同步阻塞**，调用方须 to_thread）。

    `sandbox.execute` 内部走 e2b `commands.run`（同步网络调用），在事件循环里直调
    会占住整个 worker（同 `_pull_report_from_sandbox` docstring 的教训）。

    为什么要大小：前后快照做差集时，"覆盖写同名文件"（同路径、内容变了）也必须算
    本轮新产物；只比路径会漏。
    为什么不用时间戳：沙箱 MicroVM 与 agent 容器的时钟可能漂移，`-newermt` 会漏或
    多算；差集在本机算，无时钟依赖。
    枚举失败（沙箱回收/命令错误）返回 {} —— 调用方据此放弃兜底，不抛异常。
    注意这里是**fail-closed**：拿不到可信清单就不搬文件，宁可不救也不能把无关文件
    塞进用户报告目录（部分结果比空结果更危险）。
    """
    if sandbox is None:
        return {}
    name_clause = " -o ".join("-name '*.%s'" % e for e in _SALVAGE_EXTS)
    # ⚠️ 必须先过滤掉**不存在的**候选目录再交给 find（2026-09-29 真沙箱实测）：
    # 新沙箱里 `/reports` 不存在（它要模型自己 mkdir 才有），而 GNU find 遇到不存在的
    # 起始路径会以 **exit 1** 结束 → 整条命令被判失败 → 本函数返回 {} → 兜底**永久静默
    # 失效**（每个还没在沙箱内建过 /reports 的会话都中招，恰好是绝大多数）。
    # 实测对照：`find /home/user /tmp /reports ...` exit=1 输出空；
    #           `find /home/user /tmp ...`（仅存在目录）exit=0 且正确列出产物。
    # 目录全都不存在时 `if` 不执行、整体退出码为 0 → 返回 {}（无候选），语义正确。
    dirs_clause = " ".join(_SALVAGE_DIRS)
    cmd = (
        "D=''; for d in %s; do [ -d \"$d\" ] && D=\"$D $d\"; done; "
        "if [ -n \"$D\" ]; then find $D -maxdepth 4 -type f \\( %s \\) "
        "-not -path '*/__pycache__/*' -not -path '*/site-packages/*' "
        "-not -path '*/node_modules/*' -not -path '*/.cache/*' -not -path '*/.git/*' "
        "-printf '%%p\\t%%s\\n' 2>/dev/null; fi"
        % (dirs_clause, name_clause)
    )
    try:
        resp = sandbox.execute(cmd, timeout=30)
    except Exception as e:  # noqa: BLE001
        logger.warning("[FILE_GEN] 沙箱产物枚举异常: %s", e)
        return {}
    if getattr(resp, "exit_code", 0) != 0:
        logger.warning(
            "[FILE_GEN] 沙箱产物枚举 exit=%s: %s",
            getattr(resp, "exit_code", None),
            str(getattr(resp, "output", ""))[:200],
        )
        return {}
    listed: dict[str, int] = {}
    for line in (getattr(resp, "output", "") or "").splitlines():
        path, sep, size = line.rpartition("\t")
        if not sep or not path.startswith("/"):
            continue
        try:
            listed[path] = int(size)
        except ValueError:
            continue
    return listed


async def _salvage_sandbox_artifacts(
    sandbox, user_id: str, session_id: str, before: dict[str, int] | None
) -> list[dict]:
    """收尾兜底：把沙箱里**本轮新增/改写**的交付物拉回宿主持久卷。

    仅在「交付型任务 + 宿主报告目录本轮零新文件 + B① 也没救回」时调用一次
    （调用点有 `_salvage_done` 单次门）。返回已落盘条目（字段与 file_generated
    事件同构），无物可回收返回 []。

    `before` 为 None 表示**基线不可用**（开轮枚举失败）→ 直接放弃兜底：没有基线就
    分不清"本轮新产物"和"上一轮遗留"，硬拉会把旧文件算到本轮头上。
    注意 `{}` 与 None 语义不同：`{}` 是"沙箱当时确实没有候选文件"，可以正常兜底。

    与 `_pull_report_from_sandbox` 的分工：那个是"知道确切文件名，逐条试候选路径"；
    本函数是"不知道文件名，枚举沙箱前后差集"。两者都不抛异常——收尾兜底不能反过来
    把当轮搞挂；沙箱已回收时返回 []，退化为修复前行为，不会更差。
    """
    if sandbox is None or before is None:
        return []
    try:
        after = await asyncio.to_thread(_sandbox_artifact_listing, sandbox)
    except Exception as e:  # noqa: BLE001
        logger.warning("[FILE_GEN] 收尾兜底枚举失败: %s", e)
        return []
    new_paths = [p for p, size in after.items() if before.get(p) != size]
    if not new_paths:
        logger.info("[FILE_GEN] 收尾兜底：沙箱内无本轮新增交付物")
        return []
    # 大文件优先：正式交付物（报告/表格）通常远比零散小文件更可能是用户要的东西
    new_paths.sort(key=lambda p: (-after[p], p))
    root = os.path.normpath(os.path.join(settings.report_root, user_id, session_id))
    salvaged: list[dict] = []
    for src in new_paths[:_SALVAGE_MAX_FILES]:
        try:
            responses = await asyncio.to_thread(sandbox.download_files, [src])
        except Exception as e:  # noqa: BLE001
            logger.warning("[FILE_GEN] 收尾兜底拉回异常: src=%s err=%s", src, e)
            continue
        content = None
        for resp in responses or []:
            c = getattr(resp, "content", None)
            if c:
                content = c
                break
        if not content:
            continue
        base_name = src.rsplit("/", 1)[-1]
        # 路径穿越防御（与 _pull_report_from_sandbox 同款）：落盘限制在 report_root/uid/sid 内
        disk_path = os.path.normpath(os.path.join(root, base_name))
        if disk_path != root and not disk_path.startswith(root + os.sep):
            logger.warning("[FILE_GEN] 收尾兜底路径越界拦截: %s", disk_path)
            continue
        try:
            os.makedirs(os.path.dirname(disk_path), exist_ok=True)
            with open(disk_path, "wb") as f:
                f.write(content)
        except Exception as e:  # noqa: BLE001
            logger.warning("[FILE_GEN] 收尾兜底写盘失败: %s err=%s", disk_path, e)
            continue
        salvaged.append({
            "file_name": base_name,
            "file_path": "/reports/%s/%s/%s" % (user_id, session_id, base_name),
            "file_size": len(content),
            "file_type": _infer_file_type(base_name),
        })
        logger.warning(
            "[FILE_GEN] 收尾兜底：沙箱产物已拉回宿主 %s ← %s (%d bytes)",
            disk_path, src, len(content),
        )
    return salvaged


def _merge_disk_diff_into_generated(
    generated_files: list,
    disk_files: frozenset[str],
    report_root: str,
    user_id: str,
    session_id: str,
) -> list:
    """磁盘差集补全 generated_files（2026-08-21 根治）。

    背景：_generated_files 来自模型/工具返回的 file_path 解析，可能脏——模型把
    file_path 传成目录（漏文件名）时无法产出正确 file_name；而 snapshot_report_files
    的磁盘差集（_new_files）是 report_root 下的真实文件名，是唯一可靠来源。
    chat.py 旧实现算出差集却只用于 M3 完成门，保存消息时只用脏源 → 350e5f80 会话
    全卡片 (未知文件)。此处按 file_path 去重，缺的用磁盘真实文件补齐。
    """
    result = list(generated_files)
    base = os.path.join(report_root, user_id, session_id)
    for rel in disk_files:
        disk_full = os.path.join(base, rel)
        if not os.path.isfile(disk_full):
            continue  # snapshot 含子目录名，只补文件
        fp = f"/reports/{user_id}/{session_id}/{rel}"
        if any(g.get("file_path") == fp for g in result):
            continue  # 已有（工具解析正确）保留原项，不重复
        try:
            size = os.path.getsize(disk_full)
        except OSError:
            size = 0
        file_name = rel.rsplit("/", 1)[-1]
        result.append(
            {
                "file_name": file_name,
                "file_path": fp,
                "file_size": size,
                "file_type": _infer_file_type(file_name),
            }
        )
    return result


# ─── M3 完成门任务类型感知（2026-08-26 方案 A）───
# 完成门为"必须有文件交付物"的任务设计（报告/对比/导出等），对内容型任务
# （写散文/问答/解释，交付物=对话回复本身）零产出是正常结果，不应拦截。
# 判断基于用户原始请求 body.message（不能用拼了 path_hint 的 user_message——
# 里面含"报告输出:/reports/..."会误导命中报告关键词）。
_DELIVERABLE_STRONG = (
    "报告", "报表", "导出", "保存为", "保存到", "写入文件", "生成文件",
    "输出文件", "创建文档", "生成文档", "制作文档", "整理成文档",
    "工作簿", "交付物", "产物", "附件",
    "xlsx", "excel", "word", "pdf", "csv", "pptx", "zip", "docx",
)
# ⚠ 不含裸"输出"（2026-09-16 移除）：它在指令型/规则型提示里几乎都是"模型应当输出
#   （文字）"的意思，是**内容生成**的动词而非**文件交付**的动词。文件交付义已由强信号
#   「输出文件」「生成文件」「导出」覆盖。实测误伤见 _is_deliverable_task docstring。
_DELIVERABLE_VERBS = ("对比", "差异", "diff", "分析", "总结", "汇总", "整理", "生成", "制作", "创建")
# 组合式名词仅保留明确「产出物」语义的词；纪要/清单/大纲等常是输入材料或口头回复，
# 命中会把口头总结误判为交付任务（2026-08-26 实测"总结一下会议纪要"误伤）
_DELIVERABLE_NOUNS = ("报告", "报表", "表格", "文档", "文件", "工作簿")
# 子句切分：动词与名词必须落在**同一子句**才算组合命中（2026-09-16 新增）。
# 长提示/规范文档里动词与名词天然大量散落，跨句拼凑会稳定误命中（见 docstring）。
_CLAUSE_SPLIT = re.compile(r"[。！？；;\n]")


def _is_deliverable_task(text: str) -> bool:
    """判断用户请求是否要求文件交付物（决定是否启用 M3 零产出拦截）。

    启发式：强信号词（出现即判定）或「产出类动词 × 文件类名词」**同子句**共现。
    组合式避免"分析一下这个方案"这类口头任务误判——它命中动词但无数词。

    2026-09-16 两次收紧（会话 GY24428:de18ad37 生产实证）：
      1. 移除裸"输出"：该会话用户消息里的「严禁输出缺失位置的速度/位置设定」是**规则
         约束**，却让启发式命中；结果是纯视觉标注任务被当成文件交付任务，完成门在
         模型因输出超限截断后误报「未产出交付物 / 请检查是否遗漏 download_from_sandbox」。
      2. 改为同子句共现：原文里"输出"与"表格"（【表格/矩阵布局专属规则】）分处不同段落，
         跨句拼凑是这类长规范提示的稳定误命中源。收紧后真实会话判定 False，
         而"对比两份协议生成报告""整理成表格"等真实交付请求仍为 True。

    ⚠ 刻意保留的方向性偏差：收紧后可能漏判（如"帮我写个文档说明这个模块"——"写"不在
    动词表内）。漏判 = 完成门不介入 = 退回旧行为（零产出时只是不提示），而误判 =
    给用户一条归因错误的指控。两害相权取其轻（2026-09-16 用户拍板 A+C）。
    """
    t = (text or "").lower()
    if any(k in t for k in _DELIVERABLE_STRONG):
        return True
    return any(
        any(v in clause for v in _DELIVERABLE_VERBS)
        and any(n in clause for n in _DELIVERABLE_NOUNS)
        for clause in _CLAUSE_SPLIT.split(t)
    )


class ChatRequest(BaseModel):
    session_id: str = "default-session"
    message: str = ""
    model_override: dict | None = (
        None  # 可选，动态切换模型：{"model_name": "...", "base_url": "...", "api_key": "..."}
    )
    files: list[str] | None = None  # 可选，本轮上传的文件虚拟路径列表
    continue_from_state: bool = (
        False  # 为 true 时不新增用户消息，直接从当前 checkpoint 继续生成
    )
    mcp_servers: list[str] | None = (
        None  # 可选，本轮临时启用的 MCP 服务名列表（缺省 = 全部 enabled）
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 观测：让 LangGraph chain span 回到"真根"（2026-09-29 重做版）
# ═══════════════════════════════════════════════════════════════════════════════
# 背景：`_OpenInferenceOnlySpanProcessor`（src/core/tracing.py:71）按属性
# `openinference.span.kind` 丢弃 HTTP/ASGI span —— 这是**设计意图**（POST
# /api/v1/chat 这类基础设施 span 不该进 LLM trace）。但它只"丢父节点"、不"缝合树"：
# LangGraph 等业务 span 的 parent_span_id 仍指向那个被丢弃的 FastAPI server span
# ⇒ 到达后端的 span 里**没有任何根**。而两个后端在 trace 层都依赖根：
#
#   · Langfuse：trace 级 `name`/`input`/`output` 的回退源**只有「根观测」**
#     （未显式给 `langfuse.trace.*` 时取根 span 的 name / observation input），
#     无根 ⇒ 三列恒空。实测 138 条 trace 中 input/output 各仅 10 条有值，
#     且恰好是唯一那条有根的老 trace。
#   · Phoenix：`traces` 表根本没有 name/input 列，GraphQL `Trace.rootSpan` 在无根
#     trace 上实测返回 null ⇒ 树形/详情页退化。
#
# 退化时间线（trace 数据 × git 双向对齐，详见 .workbuddy/memory/2026-09-29.md）：
#   · 09-04 16:24 起，`5b476e3`（新增 FastAPIInstrumentor）第一次构建成镜像上线
#     ⇒ HTTP server span 成了 LLM trace 的根，name 退化成 "POST /api/v1/chat"、
#     input/output 变空；
#   · 09-11 起，`f3fd075`（引入本过滤器，它当时确实占了观测后端 98%）把 HTTP span
#     丢掉 ⇒ 根彻底消失，三列全空（现状）。
#   9.2/9.3 的"正常"是巧合而非设计：那时链路里压根没有 HTTP span，`LangGraph`
#   天然就是根，三列取的是它自己的值。
#
# 修复姿势：**不新建 span、不写任何 name/input/output**，只在 event_stream 期间把
# 当前 OTel Context 换成一个空 Context —— `get_current_span()` 随即是 INVALID_SPAN，
# 于是 LangChain 插桩创建的 `LangGraph` span 的 parent=None，天然成为真根；它自带
# `openinference.span.kind=CHAIN`，能过本过滤器；它的 name/input/output 正是
# 9.2/9.3 基线里 Langfuse 三列的取值来源 ⇒ 三列是**回退**出来的、与基线逐字一致。
#
# 为什么不沿用上一版的 `start_as_current_span(..., context=Context())`（已废弃）：
# 那样等于**另造一个根覆盖掉本来正确的回退源**，实测 name 退化成 "agent-run"、
# input 退化成纯文本；而且要自己复刻 LangChain 的消息序列化格式（messages_to_dict
# 那种 `{"type": "human", "data": {...}}`），框架一升级就失真。
#
# 为什么用 attach/detach 而非 `with use_span(...)`：event_stream 是 async generator，
# 作用域要跨 yield 边界；attach 拿 token、try/finally 显式 detach 语义最直白，也不会
# 把 Context 泄漏到 StreamingResponse 之后的调用栈。
#
# 实证依据（spike 均已跑通，可复现）：
#   .workbuddy/spikes/lf_root_cut.py       —— use_span(Context()) / attach(Context())
#                                             两种姿势均能切断与 HTTP span 的父子关系
#   .workbuddy/spikes/lf_async_ctx2.py     —— 跨 async generator + 客户端断连场景下
#                                             attach/detach 配对正确，子 span 落到新根下
#   .workbuddy/spikes/lf_baseline_902b.py  —— 9.2/9.3 基线：根观测 = LangGraph、
#                                             parent=null，三列取的就是它的值
# ═══════════════════════════════════════════════════════════════════════════════

try:
    from opentelemetry import context as _otel_context_api
    from opentelemetry.context import Context as _OtelContext
except ImportError:  # pragma: no cover — 观测依赖缺失时整体降级为"不干预"
    _otel_context_api = None  # type: ignore[assignment]
    _OtelContext = None  # type: ignore[assignment]


router = APIRouter()


@router.post("/chat")
async def chat(
    request: Request,
    body: ChatRequest,  # ← FastAPI 自动解析 JSON
    store=Depends(get_store),
    checkpointer=Depends(get_checkpointer),
    current_user: dict = Depends(get_current_user),  # 从 JWT 取当前用户
    bridge=Depends(get_stream_bridge),
):
    user_id = current_user["user_id"]
    session_id = body.session_id
    thread_id = f"{user_id}:{session_id}"

    # ─── D2 护栏：同一会话已有活跃 run → 409，不并存 ─────────────────────────
    # 为什么必须拒绝而不是顶掉旧 run（interrupt）：刷新后前端可能重发一次，而旧 run
    # 仍在后台跑。"顶掉"会丢掉用户已经等待的那一轮进度；"并存"会让两条流往同一个
    # thread 的 checkpoint 里交替写，历史必然错乱。对齐 deer-flow 的
    # `multitask_strategy` 默认值 reject。
    # 与 turn_registry（删除会话护栏）的分工：那条管"会话能不能删"，这条管
    # "这一轮能不能起"。两者都要，不能互相替代。
    _active_run = run_registry.active_for_thread(thread_id)
    if _active_run is not None:
        logger.warning(
            "[RUN] 拒绝重复提交（该会话已有在跑的 run）: user=%s session=%s run=%s",
            user_id, session_id, _active_run.run_id,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "run_already_active",
                "message": "该会话已有任务在运行，请等待它结束或先终止它",
                "run_id": _active_run.run_id,
            },
        )

    # worker 是否已把收尾交给后台任务（见 _run_worker 的 finally 与 _on_worker_done）。
    # 用可变容器而不是 bool 变量：闭包里的赋值不会让外层读到新绑定，容器语义更直白。
    _worker_finalize_started = {"started": False}

    def _on_worker_done(task: asyncio.Task) -> None:
        """worker 退出时的日志 + **兜底收尾**。

        为什么需要兜底：收尾机制（`_run_worker` 里的 try/except/finally）生效之前还有
        一个开轮 prologue，里面唯一的 await 是沙箱产物基线枚举。若正好在那里被取消或
        抛异常，worker 会直接退出、连 finally 都进不去 —— run 行就留在活跃态，而它的
        `boot_id` 与本进程相同、惰性孤儿校正**兜不住**，前端会一直显示「任务进行中」。

        兜底只做**非持久化**部分（release + 落终态 + publish_end）：那个窗口里 agent
        一行都还没跑，本轮没有任何新消息需要落库。
        """
        _log_worker_done(task)
        if _worker_finalize_started["started"]:
            return  # 收尾已由 worker 的 finally 接手，不重复做
        status = (
            run_registry.STATUS_INTERRUPTED
            if task.cancelled()
            else run_registry.STATUS_FAILED
        )
        logger.warning(
            "[RUN] worker 未进收尾机制，执行兜底收尾（本轮尚无产出需落库）: run=%s status=%s",
            run.run_id, status,
        )
        asyncio.create_task(
            _finish_run(run.run_id, status), name=f"finalize-fallback:{run.run_id}"
        )

    # ─── 并发护栏快照（2026-09-12）：会话在本轮开跑前是否存在？───
    # 用途见 _persist_session 开头的防复活守卫：若用户在轮次进行中删除会话，
    # 轮次结束时不得把刚删的 sessions 行 / messages 台账写回来（复活成孤儿）。
    # 快照=False（直连 /chat 且无 POST /sessions 的新会话）则保留"不存在则创建"
    # 的兼容路径。单行 store 读（store_pkey btree），每轮一次，成本可忽略。
    session_existed_at_start = (
        await store.aget(("sessions", user_id), session_id) is not None
    )

    # 按本轮透传的 mcp_servers 过滤 MCP 工具（缺省 = 全部 enabled）
    tools = await get_mcp_tools(body.mcp_servers)

    # ⚠️ 必须 await + to_thread（2026-10-10 加固）：create_sandbox / get_env_snapshot
    # 内部是**同步阻塞**调用（CubeSandbox.get_or_create 走网络建沙箱、sandbox.execute
    # 跑环境探测，单次最长 30s 超时），裸调在 async 入口里会占住事件循环 —— 沙箱冷启
    # 或网络慢时整个 worker 僵住，**同进程所有会话的 SSE 流一起卡**，连 /healthz 都
    # 不响应，swarm healthcheck 判 unhealthy 后 SIGKILL。
    # 实证（会话 GY24428:a2719d7b）：08:41-09:23 agent 连续被杀 ≥7 次，每次死亡前
    # 约 3 分 19 秒完全静默（连 healthz 都不打）；同期 agent 内存峰值仅 350MiB/2GiB，
    # 非 OOM —— 是事件循环被冻住，不是资源不足。
    # 本仓既有约定见 src/infra/trash.py:96 与 _ensure_file_on_disk（本文件 :345）。
    sandbox = await asyncio.to_thread(create_sandbox, thread_id)
    logger.info("[DIAG] create_sandbox(thread_id=%s) → sandbox=%s", thread_id, type(sandbox).__name__ if sandbox else "None")

    # ─── M1 环境预检：快照注入 + 磁盘硬阈值拒绝（设计文档 M1）───
    # 快照缓存按 thread_id 60s 复用；无沙箱（本地模式）时 env_snapshot 为 None，跳过
    env_snapshot = await asyncio.to_thread(get_env_snapshot, sandbox, thread_id)
    if env_snapshot is not None and env_snapshot.ok:
        disk = env_snapshot.disk_avail_mb
        if disk is not None and disk < settings.sandbox_disk_hard_mb:
            logger.warning(
                "[M1] 沙箱磁盘不足拒绝启动: user=%s, session=%s, avail=%sMB",
                user_id, session_id, disk,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    f"沙箱磁盘可用空间不足（{disk}MB < {settings.sandbox_disk_hard_mb}MB），"
                    "请清理沙箱后重试"
                ),
            )
        if disk is not None and disk < settings.sandbox_disk_warn_mb:
            logger.warning("[M1] 沙箱磁盘偏低: user=%s, session=%s, avail=%sMB", user_id, session_id, disk)
    # 构建 skill sources：系统 + Agent 自创（启动时加载）+ 当前用户的共享 skill
    base_skills = getattr(request.app.state, "skills", [])
    user_skill_path = f"/skills/__user_{user_id}__/"
    skills = list(base_skills) + [user_skill_path]
    agent = await create_agent(
        user_id=user_id,
        session_id=body.session_id,
        thread_id=thread_id,
        store=store,
        sandbox=sandbox,
        checkpointer=checkpointer,
        tools=tools,
        skills=skills,
    )

    sandbox_id = ""
    if sandbox is not None:
        try:
            sandbox_id = sandbox.sandbox_id
            logger.info("[DIAG] sandbox_id 提取成功: %s", sandbox_id)
        except Exception as e:
            logger.warning("[DIAG] sandbox_id 提取失败: %s", e)
    else:
        logger.warning("[DIAG] sandbox 为 None（create_sandbox 返回空），sandbox_id 未设置")
    
    if not sandbox_id:
        logger.warning("[DIAG] sandbox_id 最终为空，upload_to_sandbox 等工具将无法使用")

    path_hint = (
        f"沙箱 ID：{sandbox_id}\n"
        f"当前用户：{current_user.get('display_name', user_id)}（{current_user.get('role', 'user')}）\n"
        f"\n"
        f"【当前会话路径】\n"
        f"输入文件：/uploads/{user_id}/{session_id}/\n"
        f"报告输出：/reports/{user_id}/{session_id}/\n"
        # ─── 交付物写入通道（2026-09-29 方案 A）───
        # 背景：GY35377/60c64c4f 实测——模型用 execute 在沙箱内生成 pptx，再在**沙箱内**
        # `mkdir -p /reports/... && cp ...`（exit 0）就宣称"已保存、可直接下载"，实际宿主
        # 报告目录一个文件都没有，用户点下载必然 404。
        # 根因是**同一段路径字符串在两套后端下含义不同**：write_file 走 FilesystemBackend
        # 直写宿主报告卷；execute 走 sandbox 默认后端（agent.py:690 注释"execution is not
        # path-routable"），而沙箱**未挂载任何宿主目录**（sandbox.create_sandbox 无 mount）。
        # 明说通道边界，从源头掐掉这类幻觉（不替代 B 的兜底，二者互补）。
        f"⚠️ 交付物写入通道（务必遵守，弄错用户会拿到不存在的文件）：\n"
        f"  · 写报告/文档/表格等交付物 → **只能用 write_file/write 工具**写 "
        f"/reports/{user_id}/{session_id}/...；该虚拟路径直通宿主报告目录，写完即可下载。\n"
        f"  · **禁止用 execute/shell 往 /reports/... 或 /uploads/... 写文件**：这两个目录是"
        f"虚拟路径，沙箱里**没有挂载**它们（execute 走沙箱文件系统，不参与路径路由）。"
        f"shell 里的 mkdir/cp 到 /reports/... 只落在沙箱内部，宿主报告目录不会有任何文件，"
        f"而且沙箱空闲约 5 分钟即被回收，文件随即消失。\n"
        f"  · 若确实要用 shell 生成文件（如 python-pptx / pandas.to_excel）：产物先落在沙箱"
        f"（如 /home/user/xxx.pptx），**必须再用 download_from_sandbox 拉回** "
        f"/reports/{user_id}/{session_id}/ —— 这是 shell 产物唯一的交付通道。\n"
        f"  · 同理，沙箱里也看不到 /uploads/... 的用户文件：读它们用 read_file/ls，"
        f"或先 upload_to_sandbox 把文件送进沙箱再处理。\n"
        f"  · **未真正看到 download_from_sandbox 或 write_file 的成功回执前，"
        f"禁止声称文件\"已保存/已生成/可直接下载\"**；不确定就如实说明缺少哪一步。\n"
    )

    # M1：环境快照注入（系统自动探测，模型不应自行重装/探测环境）
    if env_snapshot is not None:
        path_hint += (
            f"\n【沙箱环境】（系统自动探测，仅需直接使用，勿重复安装或探测）\n"
            f"{env_snapshot.to_hint()}\n"
        )

    # 本轮文件提示（多轮对话时 Agent 只处理本轮上传的文件）
    file_hint = ""
    if body.files:
        file_list = "\n".join(f"- {f}" for f in body.files)
        file_hint = f"\n用户为本轮对话上传了以下文件（路径已映射到虚拟文件系统，请精确处理这些文件）：\n{file_list}"

    user_message = f"{path_hint}\n{body.message}{file_hint}"

    # ─── P1-c D5：本轮 user 消息的"预写坐标" ──────────────────────────────────
    # ① id 由我们生成并显式喂给 LangGraph（下面的 `HumanMessage(id=...)`）。这是 D5
    #    的承重点：预写之后，流尾水位的 last_stored_id 就变成了"我们刚写的这一条"，
    #    它必须能在 checkpoint 的 history 里被认出来，否则水印会永久退化成 by_length
    #    —— 那条专门对抗 SummarizationMiddleware 压缩的 by_id 路径被关掉后，压缩会
    #    让 len(history) 小于 prev_count，于是判为"无新增"，本轮 AI 回答一条都写不进去
    #    （无异常、无日志，只有刷新后看不到回答）。
    #    实测（.workbuddy/_probe_msg_id.py，三组对照）：显式 id 被 LangGraph 原样保留；
    #    dict 形式（改造前的写法）由框架生成 uuid，控制不了；同 id 重发在 add_messages
    #    下是替换而非追加。
    # ② content 走与流尾**完全相同**的清洗（裁剪 + 去 path_hint 前缀）：预写的条目
    #    之后不会再被流尾改写（水印只追加其后），内容必须一次对、且与改造前一致。
    # 编辑后重发（continue_from_state）不注入新 user 消息 → 不预写。
    setup_user_message_id: str | None = (
        None if body.continue_from_state else str(uuid.uuid4())
    )
    setup_user_message_content = (
        _strip_injected_path_hint(_clip(user_message, settings.persist_max_content_chars))
        if setup_user_message_id
        else ""
    )

    # 构造 graph 输入：
    graph_config = {"configurable": {"thread_id": thread_id}}

    # - 正常发送：新增用户消息
    # - 编辑后重发：从 PostgresStore 读取截断后的消息列表重建 graph 状态
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

    if body.continue_from_state:
        try:
            msg_namespace = ("messages", user_id, session_id)
            msg_prefix = ".".join(msg_namespace)
            # 取全（而非只取最新一页）：这里要把历史**整段**灌回图，截短即丢上下文。
            # 增量格式下每个条目就是一条消息（旧格式是单行 {"items": [...]}）。
            _rows = await store.alist_all_messages(msg_prefix)
            stored_items = [v for _k, v in _rows if isinstance(v, dict)]
            lc_msgs = []
            for m in stored_items:
                if m.get("role") == "user":
                    lc_msgs.append(HumanMessage(content=m.get("content", "")))
                elif m.get("role") == "ai":
                    stored_reasoning = m.get("reasoning", "")
                    ai_kwargs = {}
                    if stored_reasoning:
                        ai_kwargs["reasoning_content"] = stored_reasoning
                    lc_msgs.append(AIMessage(
                        content=m.get("content", ""),
                        additional_kwargs=ai_kwargs,
                    ))
            graph_input = {"messages": lc_msgs}
            logger.info("continue_from_state: 从 store 重建 %d 条消息", len(lc_msgs))
        except Exception as e:
            logger.warning(
                "continue_from_state: 读取 store 失败, fallback 到 checkpoint: %s", e
            )
            latest = await agent.aget_state({"configurable": {"thread_id": thread_id}})
            graph_input = (
                {"messages": latest.values.get("messages", [])} if latest else None
            )
    else:
        # 显式带 id（见上方 setup_user_message_id 注释）：这个 id 是 setup 预写条目
        # 与 checkpoint 之间唯一的连接点，丢了它流尾就定位不到水印。
        graph_input = {
            "messages": [
                HumanMessage(content=user_message, id=setup_user_message_id)
            ]
        }

    async def _persist_session(
        agent,
        thread_id: str,
        user_id: str,
        session_id: str,
        store,
        body,
        *,
        generated_files: list | None = None,
        disk_files: frozenset[str] | None = None,
        completion_blocked: bool = False,
        terminal_fallback: dict | None = None,
        reasoning_started_at_ms: int | None = None,
        reasoning_ended_at_ms: int | None = None,
        turn_started_at_ms: int | None = None,
        turn_ended_at_ms: int | None = None,
        reason: str = "normal",
    ) -> int:
        """把 agent checkpoint 中的消息保存到会话历史（store）。

        由 event_stream 两处调用：
        - 正常路径（reason="normal"）：SSE 流完整结束
        - 断连/取消路径（reason="interrupted"）：生成器被 GeneratorExit/CancelledError
          打断时在 finally 中强制保存，防止用户刷新/关闭页面后本轮消息丢失
          （2026-08-11 15:26 实测：断连后"写 skill"指令未保存，前端重拉即消失）。
        函数内部只有 await 没有 yield，可在 finally 块中安全调用。

        时间戳参数（reasoning_started/ended/turn_started/ended）来自 event_stream
        流式阶段埋点（2026-09-08 加）：首个 reasoning 事件 → 推理起始；首个 token
        事件 → 推理结束；while 循环 break → turn 结束。持久化到 AI 消息 entry 的
        reasoning_duration_ms / turn_duration_ms / reasoning_started_at 三个字段，
        让前端 ReasoningBlock 跨刷新稳定显示秒数（对齐 deer-flow 风格）。

        P0′（2026-10-12）中断记账：`reason="interrupted"` 时**不写任何时长字段**
        （reasoning_duration_ms / turn_duration_ms），改写 `turn_status="interrupted"`
        + `turn_started_at` + `turn_ended_at`。理由见下方两处 `interrupted` 分支。
        """
        generated_files = generated_files or []
        # P0′（2026-10-12）：断连/取消路径。此时 `turn_ended_at_ms` 只是"连接断开时刻"，
        # 不是本轮任务的终点 —— 见下方两处 `interrupted` 分支的说明。
        interrupted = reason == "interrupted"
        if disk_files:
            # 磁盘差集补全：工具解析的 generated_files 可能脏（模型把 file_path 传成
            # 目录），用 report_root 磁盘真实文件名补缺（2026-08-21 根治）
            generated_files = _merge_disk_diff_into_generated(
                generated_files, disk_files,
                settings.report_root, user_id, session_id,
            )
        # ─── 并发护栏：会话在本轮进行中被删除 → 本轮不持久化（2026-09-12）───
        # session_existed_at_start 是 chat 端点在 agent 开跑前的快照（闭包变量）：
        # 快照=True 且现在行没了 → 用户在轮次进行中删除了会话。此时写台账会把
        # 刚被原子删除的 sessions 行 + messages 复活成孤儿（删了又回来，且
        # checkpoint 里的旧上下文也被重建）。快照=False（直连 /chat 的新会话）
        # 则照常走下方"不存在则创建"的兼容路径，不受影响。
        if session_existed_at_start:
            if await store.aget(("sessions", user_id), session_id) is None:
                logger.info(
                    "会话 %s 在本轮进行中被删除，跳过持久化（防复活护栏）", session_id
                )
                return 0
        try:
            # 读取最终状态中的消息（断连兜底路径下 agent 图可能仍在写 checkpoint，
            # 用带重试的读取防御瞬时争用导致的 "another command is already in progress"）
            state = await _aget_state_with_retry(agent, thread_id)
            logging.warning(
                "[DIAG] %s: state=%s, has_values=%s",
                "SSE 结束" if reason == "normal" else "SSE 中断强制保存",
                type(state).__name__ if state else None,
                hasattr(state, "values") if state else False,
            )
            if state and hasattr(state, "values"):
                all_msgs = state.values.get("messages", [])
                # 第一遍：建立 tool_call_id -> ToolMessage content 的映射
                # 用于在保存 AIMessage 的 tool_calls 时填入 result 字段
                tool_results: dict[str, str] = {}
                for m in all_msgs:
                    if getattr(m, "type", None) == "tool":
                        tc_id = getattr(m, "tool_call_id", None)
                        if tc_id:
                            tool_results[tc_id] = str(m.content) if m.content else ""

                # 提取人类可读的消息（只保留 user / assistant / tool 角色的核心信息）
                history = []
                for msg in all_msgs:
                    role = getattr(msg, "type", "unknown")
                    if role == "human":
                        role = "user"
                    content = str(msg.content) if msg.content else ""
                    reasoning = ""

                    # 1. 从 additional_kwargs 提取推理内容（DeepSeek/Groq/Ollama/XAI 等）
                    if hasattr(msg, "additional_kwargs") and msg.additional_kwargs:
                        kw_reasoning = msg.additional_kwargs.get("reasoning_content") or ""
                        if kw_reasoning:
                            reasoning = kw_reasoning

                    # 2. Qwen 系：从 content 中移除 </think> 段
                    # 关键：每条 AI 消息独立切分（之前的 `if not reasoning` 会让后一条
                    # AI 消息的 <think> 段残留到 content 里）
                    if "</think>" in content:
                        think_end = content.find("</think>")
                        thinking_part = content[:think_end]
                        remaining = content[think_end + 8:]  # skip </think>
                        if remaining.startswith("\n"):
                            remaining = remaining[1:]
                        if thinking_part.strip():
                            # 保留 original reasoning（如有），附加本次 <think> 段
                            if reasoning:
                                reasoning = reasoning + "\n\n" + thinking_part
                            else:
                                reasoning = thinking_part
                            content = remaining

                    # 3. 长度上限（2026-09-12 修：原为 content[:2000]/reasoning[:2000]）
                    #    截断写在持久化层＝写入即不可逆删除，方向错了层（且引入提交
                    #    4a92447 未说明原因）。正文/推理是用户要看的交付物本体，默认不限制；
                    #    tool 消息正文属中间过程走中间上限。
                    #    ⚠ 本循环遍历**所有角色**，role=tool 的独立条目同样命中此处，
                    #    故必须按 role 分流，否则工具输出会被一并放开。
                    content = _clip(
                        content,
                        settings.persist_max_tool_result_chars
                        if role == "tool"
                        else settings.persist_max_content_chars,
                    )
                    reasoning = _clip(reasoning, settings.persist_max_content_chars)

                    # 4. 去掉 user 消息中的 path_hint 前缀
                    #    抽成纯函数与 setup 预写共用 —— 预写条目不会被流尾改写，
                    #    两处判据必须逐字一致，见 _strip_injected_path_hint 文档。
                    if role == "user":
                        content = _strip_injected_path_hint(content)

                    # 5. 不再跳过中间 AI 消息
                    #    之前跳过的初衷是避免"只有 thinking 折叠块 + 空正文"的奇怪气泡，
                    #    但这会丢失 tool_call 渲染（流式阶段用户能看到 write_file 的 tool_call
                    #    卡片 + 文件卡片，刷新后这部分消失，体验不一致）。
                    #    现在保留所有 AI 消息：中间 AI 消息显示 thinking + ToolCallCard +
                    #    GeneratedFileCard，最终 AI 消息显示 thinking + 真正的回复内容。

                    entry = {
                        "id": getattr(msg, "id", None),
                        "role": role,
                        "content": content,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    if reasoning:
                        entry["reasoning"] = reasoning
                    # AI 消息附带 tool_calls 信息
                    if role == "ai" and hasattr(msg, "tool_calls") and msg.tool_calls:
                        entry["tool_calls"] = [
                            {
                                "id": tc.get("id") or f"tc-{i}",
                                "tool": tc["name"],
                                "args": tc["args"],
                                "status": "success",
                                "result": _clip(
                                    tool_results.get(tc.get("id", "")) or "",
                                    settings.persist_max_tool_result_chars,
                                ),
                            }
                            for i, tc in enumerate(msg.tool_calls)
                        ]
                    # 持久化时长字段（2026-09-08 加，对齐 deer-flow）：
                    # reasoning_duration_ms：本条 AI 消息的推理时长（首个 reasoning 事件 →
                    #   首个 token 事件）。仅当 reasoning 存在 + 两个时间戳都有值时写入。
                    # reasoning_started_at：推理起始的 ISO 时间戳，供前端 ReasoningBlock
                    #   锚定（防组件挂载漂移），与 reasoning_duration_ms 配合使用。
                    # ⚠ P0′（2026-10-12）：中断路径的取舍（只写起点、不写时长）收敛到
                    #   纯函数 _reasoning_duration_fields，见其文档；改前这段是三条件
                    #   内联 if，无法被 spike 直接断言。
                    if role == "ai" and reasoning:
                        entry.update(_reasoning_duration_fields(
                            interrupted=interrupted,
                            reasoning_started_at_ms=reasoning_started_at_ms,
                            reasoning_ended_at_ms=reasoning_ended_at_ms,
                        ))
                    history.append(entry)

                # 持久化 turn_duration_ms（整轮耗时）：写到**本轮**最后一条 AI 消息上。
                # turn_start = event_stream 进入时刻；turn_end = while 循环 break / 兜底 now。
                # 仅当两个时间戳都有值时写入（断连路径兜底时刻已设，正常路径 break 前已设）。
                #
                # 归属判据 2026-10-12 修：改前是「在整份 history 里倒序找第一条 AI」——
                # 那会命中上一轮的 AI（详见 _last_ai_index_for_turn 文档）。
                # 现在统一走 _mark_turn_last_ai（按「最后一条 user 之后」划定本轮区间），
                # 区间内没有 AI 就不写：宁可这一轮没有耗时值（前端走内存计时兜底 / 不显示），
                # 也不给错的归属。
                if interrupted:
                    # ─── P0′（2026-10-12）：断连 ≠ 本轮完成 ───
                    # payload 由纯函数 _interrupted_turn_fields 构造（见其文档：
                    # 为什么不能写 turn_duration_ms、为什么保留起点）。
                    # 归属仍走 _mark_turn_last_ai：本轮无 AI 产出则不写（宁可无标记，
                    # 也不把"中断"打到上一轮的 AI 上）。
                    if _mark_turn_last_ai(
                        history,
                        _interrupted_turn_fields(turn_started_at_ms, turn_ended_at_ms),
                    ) is None:
                        logger.warning(
                            "[DIAG] 本轮无 AI 产出，跳过 turn_status=interrupted 打点"
                            "（避免写到上一轮的 AI 上）: session=%s, history=%d 条",
                            session_id, len(history),
                        )
                elif turn_started_at_ms is not None and turn_ended_at_ms is not None:
                    turn_duration_ms = max(0, turn_ended_at_ms - turn_started_at_ms)
                    if _mark_turn_last_ai(
                        history, {"turn_duration_ms": turn_duration_ms}
                    ) is None:
                        logger.warning(
                            "[DIAG] 本轮无 AI 产出，跳过 turn_duration_ms 打点"
                            "（避免写到上一轮的 AI 上）: session=%s, history=%d 条",
                            session_id, len(history),
                        )

                # 循环结束后，将 generated_files 关联到合适的 AI 消息。
                #
                # 归属判据 2026-10-12 重写（改前是「全文第一条带 tool_calls 的 AI」，
                # 跨轮会命中**最早那轮**、本轮多段工具调用时会全堆到第一段上——与
                # 流式阶段按段分散显示不一致）。现在按 tool_call_id 精确匹配到
                # **发起该工具调用**的那条 AI，回退链见 _attach_generated_files。
                if generated_files:
                    if not _attach_generated_files(history, generated_files):
                        logger.warning(
                            "[DIAG] 本轮无 AI 产出，跳过 generated_files 关联"
                            "（避免挂到上一轮的 AI 上）: session=%s, files=%d",
                            session_id, len(generated_files),
                        )

                # M3 完成门：零产出时在**本轮**最后一条 AI 消息上标记失败状态，
                # 前端可据此展示"未产出交付物"徽标，避免"状态不明"。
                #
                # 归属判据 2026-10-12 修：改前同样是「整份 history 倒序找第一条 AI」，
                # 本轮无 AI 产出时会把徽标打到**上一轮**的 AI 上（与耗时同源的错位，
                # 且徽标比耗时行更显眼）。现与耗时共用 _mark_turn_last_ai。
                # 注：本标记为真的常见场景（模型零工具调用纯复述 / 产物缺失耗尽重试）
                # 本身**有** AI 消息，故正常路径行为不变；只有本轮连 AI 消息都没有
                #（模型调用失败 / 断连且 checkpoint 无 AI）才不落盘——此时无载体可挂，
                # 事实由 SSE 事件与 WARNING 日志承载。
                if completion_blocked:
                    if _mark_turn_last_ai(
                        history, {"completion": "blocked_no_output"}
                    ) is None:
                        logger.warning(
                            "[M3] 本轮无 AI 产出，跳过 completion=blocked_no_output 打点"
                            "（避免写到上一轮的 AI 上）: session=%s, history=%d 条",
                            session_id, len(history),
                        )

                # 空响应降级（2026-09-16）：失败点在**模型层**（思考吃满输出上限被截断），
                # 不是"产物没拉回来"，故不与上面共用 blocked_no_output 标记——两者在
                # 复盘/取证时要能一眼分开（旧实现会把 de18ad37 这类截断也标成"零交付物"）。
                #
                # 归属判据同上（2026-10-12 修）：改前会打到上一轮的 AI 上。
                # 三个分支里 THINKING_TRUNCATED / THINKING_ONLY 本轮必然有 AI 消息
                #（带 reasoning 的截断/仅思考条目），FULLY_EMPTY 才可能没有——后者不落盘。
                if terminal_fallback:
                    if _mark_turn_last_ai(
                        history,
                        {
                            "completion": "model_output_truncated",
                            "stop_reason": terminal_fallback.get("branch"),
                        },
                    ) is None:
                        logger.warning(
                            "[DIAG] 本轮无 AI 产出，跳过 completion=model_output_truncated "
                            "打点（避免写到上一轮的 AI 上）: session=%s, branch=%s, history=%d 条",
                            session_id, terminal_fallback.get("branch"), len(history),
                        )

                # ─── 存入 store：增量追加（2026-09-12 方案 B，替代全量覆盖写）───
                # 改前：`aput(ns, "messages", {"items": history})` —— 每轮把整份 history
                # 当一个 value 覆盖写单行。三个后果（生产实测）：
                #   ① 爆炸半径=整个会话：最大会话 259 条 / 1.32 MB 挤一行，写失败即全丢；
                #   ② 写放大 130×：累计约 171 MB 只为存下 1.32 MB；
                #   ③ 摘要裁剪传导：checkpoint 被 RemoveMessage 裁短 → 下一轮快照缩水
                #      → 全量覆盖把缩水写进 store，早期对话在任何一层都找不回。
                # 改后：一条消息一行（key = 零填充序号），只写新增部分。
                #   `created_at` 顺带修掉一个隐性 bug：改前每轮给**所有**历史消息重写
                #   `created_at = now()`，前端看到的每条历史消息时间其实是"最后一次保存
                #   时刻"；增量下已归档条目不再被触碰。
                msg_namespace = ("messages", user_id, session_id)
                msg_prefix = ".".join(msg_namespace)
                # 第三个返回值是"旧格式残留条数"——部署顺序防线，见 aget_message_watermark
                prev_count, last_stored_id, legacy_rows = await store.aget_message_watermark(
                    msg_prefix
                )
                stored_message_count: int | None = None
                if legacy_rows:
                    # 存量还是旧格式（新版已上线、迁移脚本尚未执行）：**本轮不写**。
                    # 写了会让新旧格式并存，迁移脚本的前置守卫随即拒绝执行 → 需要人工对账。
                    # 代价仅限于"这一轮的回放副本"（checkpoint 不受影响，对话照常继续）。
                    logger.error(
                        "[DIAG] 会话 %s 存量仍为旧格式（%d 条非序号条目），本轮跳过 "
                        "store 写入；请先执行 migrate_messages_incremental.py 展开存量",
                        session_id, legacy_rows,
                    )
                else:
                    next_seq, new_items, append_mode = _plan_message_append(
                        history, prev_count, last_stored_id,
                    )
                    _warn_oversized_items(new_items, prefix=msg_prefix)

                    if new_items:
                        await store.awrite_messages(
                            msg_namespace,
                            [
                                (message_key(next_seq + offset), entry)
                                for offset, entry in enumerate(new_items)
                            ],
                        )
                    elif append_mode == "none" and prev_count > 0:
                        # 既没按 id 定位到，history 也不比已存的长 → 本轮无法安全追加。
                        # 不写是安全方向（宁可少写一轮，不可改写已归档内容），但要可见。
                        logger.warning(
                            "[DIAG] 增量追加跳过：无法定位水印且 history(%d) 未超过已存(%d)，"
                            "session=%s last_stored_id=%r",
                            len(history), prev_count, session_id, last_stored_id,
                        )

                    # 会话内实际可见条数 = 已存条数 + 本次新增（压缩后 history 会变短，
                    # 但 store 保留全部原文，元数据应与 store 实际条目一致而非 history 长度）
                    stored_message_count = prev_count + len(new_items)
                    logger.warning(
                        "[DIAG] %s: session=%s 增量追加 prev=%d new=%d mode=%s",
                        "会话保存完成" if reason == "normal" else "断连强制保存完成",
                        session_id, prev_count, len(new_items), append_mode,
                    )

                # 更新会话元数据（标题、消息数、时间）
                session_ns = ("sessions", user_id)
                item = await store.aget(session_ns, session_id)
                now_ts = datetime.now(timezone.utc).isoformat()

                # 用用户实际输入作为默认标题
                title = (
                    body.message[:50] + ("..." if len(body.message) >= 50 else "")
                    if body.message
                    else "新会话"
                )

                if item is not None:
                    data = item.value
                    # 旧格式残留时 stored_message_count 为 None → 保持原值不动
                    # （若写 0 会把会话列表的条数徽标清零，属于额外伤害）
                    if stored_message_count is not None:
                        data["message_count"] = stored_message_count
                    data["updated_at"] = now_ts
                    old_title = data.get("title", "")
                    # 覆盖旧的 path_hint 标题，或首次设置标题
                    if (
                        old_title.startswith("沙箱 ID")
                        or old_title in ("新会话", "新对话")
                        or not old_title
                    ):
                        data["title"] = title
                else:
                    # 会话不存在则创建（兼容直接调 /chat 而非 POST /sessions 的场景）
                    data = {
                        "title": title,
                        "created_at": now_ts,
                        "updated_at": now_ts,
                        "message_count": stored_message_count or 0,
                    }

                # 会话条目即唯一数据源：GET /sessions 按 namespace 前缀直接检索，
                # 不再需要维护 __index__ 手工索引（少一次非原子写，消除数据/索引分叉面）。
                await store.aput(session_ns, session_id, data)
                logger.warning(
                    "[DIAG] %s: user=%s, session=%s, msgs=%s(stored)",
                    "会话保存完成" if reason == "normal" else "断连强制保存完成",
                    user_id,
                    session_id,
                    stored_message_count,
                )
                return stored_message_count or 0
        except asyncio.CancelledError:
            # 2026-10-10：CancelledError 继承 BaseException，原来的 `except Exception`
            # 抓不到它，保存被取消时**完全静默**（事故现场两条日志都没有）。显式记录后
            # 必须重抛 —— 不能吞掉取消语义。
            logger.error(
                "保存会话消息被取消（关键错误，本轮消息可能丢失）: user=%s, session=%s",
                user_id, session_id, exc_info=True,
            )
            raise
        except Exception as e:
            logger.error(
                "保存会话消息失败（关键错误，本轮消息可能丢失）: %s", e, exc_info=True
            )
        return 0

    async def _finish_run(run_id: str, status: str) -> None:
        """收尾三元组：release → 落终态 → 通知订阅者。**三处的顺序都有语义**。

        · release 在持久化**之后**（2026-10-10 不变量）：否则用户此刻提交删除请求，
          可能趁持久化过程中插进来，把刚写的台账又删掉 / 留下孤儿行。
        · 落终态在 publish_end **之前**：进程若在两者之间死掉，store 里会留下一条
          "活跃"的行 —— 同一次 boot 内 boot_id 相同，惰性孤儿校正**兜不住**它，
          前端就会一直显示「任务进行中」。
        · publish_end（= 前端看到的 `[DONE]`）在持久化**之后**：前端收到 [DONE] 会
          立刻允许编辑，此时消息必须已经落盘，否则编辑用的 from_index 会越界
          （2026-08 实测；P1-b 前这条约束由 yield 顺序保证，现在由本函数保证）。
        """
        turn_registry.release(thread_id)
        try:
            await run_registry.mark(store, run_id, status)
        except Exception:  # 落态失败不该阻断 [DONE]
            logger.exception(
                "[RUN] 终态落盘失败（刷新后可能一直显示「进行中」）: run=%s status=%s",
                run_id, status,
            )
        await bridge.publish_end(run_id)

        # ─── 终态已达成 ⇒ 进程内态可以进入回收队列 ───────────────────────────────
        # 方案文档的清单里没有这一步，但不加它就是 P1-b 亲手引入的内存泄漏（见
        # _reap_run_later 的算式）。挂在这里而不是 finally：只有"终态 + [DONE] 都
        # 已落地"才算真的结束，否则回收会把还在收尾的 run 的窗口提前拔掉。
        # 刻意 create_task 而不是 await：宽限期是 sleep，await 会把收尾任务（可能是
        # 脱离取消作用域的后台任务）拴在这个必然要等 90s 的 sleep 上。
        _reap = asyncio.create_task(
            _reap_run_later(run_id, bridge, RUN_REAP_DELAY_SECONDS),
            name=f"reap:{run_id}",
        )
        _reap.add_done_callback(_log_reap_done)

    async def _persist_session_and_release(
        run_id: str, *, persisted: bool, status: str, **persist_kwargs
    ) -> int:
        """**中断收尾**专用打包：落盘（若还没落）→ release → 终态 → publish_end。

        为什么必须打包成一个后台任务：worker 被取消/断连时它的 finally **不能 await**
        —— 本 task 已在取消中，第一个 await 点即抛 CancelledError，整段收尾蒸发
        （4863afff 事故现场：打了「SSE 流中断」后一条日志都没有）。所以收尾动作只能
        交给这个脱离取消作用域的任务，而它们之间的顺序有语义（见 _finish_run）。

        `persisted=True` 表示正常路径已经把这一轮存过了，只是在落终态阶段被打断 ——
        此时不重复写库。`persisted=False` 时重跑一次是安全的：_persist_session 走
        水印式增量（aget_message_watermark → _plan_message_append 只写新增），
        第二次跑会因为水印推进而不再追加任何消息。
        """
        try:
            if persisted:
                return 0
            return await _persist_session(**persist_kwargs)
        finally:
            await _finish_run(run_id, status)

    async def _astream_with_heartbeat(stream, interval: float = 15.0):
        """把 agent.astream 包成带静默保活心跳的异步生成器。
    
        模型长思考不吐字节时（如 Qwen <think> 静默数分钟），TCP 连接会空闲超过代理
        proxy_read_timeout（openresty / AC gateway 常见 60s）→ 被掐断 → 前端"思考消失、
        无输出"。每 interval 秒无新 chunk 则 yield 心跳哨兵，消费方转成 SSE 注释行
        ": ping\\n\\n" 保活连接（字节照传、前端忽略）。
    
        参考 deer-flow StreamBridge.subscribe(heartbeat_interval=15) → sse_consumer 转
        ": heartbeat\\n\\n"。关键：用 asyncio.wait 竞速——token 先到则立即发（零延迟），
        满 interval 秒无字节才发心跳；只 cancel 静默 sleep task，**绝不 cancel 生成器
        task**，否则会破坏 agent.astream 这个异步生成器（asyncio.wait_for 超时则会
        cancel 进生成器内部，导致后续 __anext__ 不可用）。
        """
        _anext = asyncio.ensure_future(stream.__anext__())
        try:
            while True:
                _sleep = asyncio.ensure_future(asyncio.sleep(interval))
                _done, _ = await asyncio.wait(
                    {_anext, _sleep}, return_when=asyncio.FIRST_COMPLETED
                )
                if _anext not in _done:
                    # 满 interval 秒无 chunk：发心跳，保持同一个生成器继续等下一项
                    _sleep.cancel()
                    yield ("__heartbeat__", None)
                    continue
                # token 先到：取消静默 sleep，取出真实 chunk 正常下发
                _sleep.cancel()
                try:
                    item = _anext.result()
                except StopAsyncIteration:
                    break
                yield item
                _anext = asyncio.ensure_future(stream.__anext__())
        finally:
            _anext.cancel()

    async def _run_worker(run: "run_registry.RunRecord") -> None:
        """一轮对话的**生产者**：脱离 SSE 连接独立推进（P1-b）。

        与原 `_event_stream_inner` 的差别只有两处，其余逐行保留：
          1. 每个事件不再 `yield` 给连接，而是 `await` 桥的 publish —— 订阅者来去
             不再影响本函数。这是"刷新不再杀任务"的落点。
          2. OTel 空 Context 的 attach/detach 从连接层**搬进来**：它包围的是产生
             LLM span 的 agent 逻辑，留在端点会让 LangGraph span 重新挂到 FastAPI 的
             HTTP server span 之下 —— 注释块（本文件 :910 起）里记录的"Langfuse 三列
             全空 / Phoenix 无 rootSpan"会原样回归。方案文档未点名，属高危遗漏项。

        收尾顺序（都不能动）：持久化 → release → 落终态 → publish_end，理由见
        `_finish_run`。被打断时整套收尾交给脱离取消作用域的后台任务。

        调用方：`chat()` 用 `asyncio.create_task` 起它（不是 await）—— 必须脱离请求
        task 的取消作用域，否则客户端断连时 Starlette 会把本轮一起取消，就白改了。
        """
        run_id = run.run_id

        # ─── "在跑轮次"台账：acquire 从连接层搬到这里 ───
        # 轮次的"活着"由 worker 定义，不再由连接定义。
        turn_registry.acquire(thread_id)

        async def _beat_forever() -> None:
            """worker 自带的周期 beat（与有没有订阅者无关）。"""
            while True:
                await asyncio.sleep(BEAT_INTERVAL_SECONDS)
                turn_registry.beat(thread_id)

        _beat_task = asyncio.create_task(_beat_forever(), name=f"beat:{run_id}")

        # ─── OTel 空 Context：让 LangGraph span 成真根（原理见本文件 :910 注释块）───
        _ctx_token = None
        if _otel_context_api is not None and _OtelContext is not None:
            try:
                # 空 Context ⇒ get_current_span() 是 INVALID_SPAN，父子链路在此断开
                _ctx_token = _otel_context_api.attach(_OtelContext())
            except Exception:
                logger.debug("[TRACING] 切断 OTel 父 span 失败（已忽略）", exc_info=True)

        async def _pub(payload: dict) -> None:
            """发一个**结构性**事件（进桥的有界日志，可被 Last-Event-ID 重放）。"""
            etype = str(payload.get("type") or "message")
            await bridge.publish(
                run_id, etype, {k: v for k, v in payload.items() if k != "type"}
            )

        async def _pub_delta(kind: str, text: str) -> None:
            """发一段**逐增量**文本（token / reasoning），走桥的尾部累积器。"""
            await bridge.publish_delta(run_id, kind, text)

        # ─── 收尾状态位：必须在**第一个 await 之前**就位 ───
        # 理由：本函数靠 finally 兜底收尾，而它可能在任意 await 点被取消后触发。若那时
        # 某个状态位还没绑定，收尾自己会 NameError —— 就从"丢一轮消息"升级成"收尾也
        # 崩、run 行永远停在活跃态"。
        # 其余收尾要用的状态见上方 `_turn_started_at_ms` 那一片原始声明：它们同样是同步
        # 语句，且都排在本函数首个 await（下方沙箱产物基线枚举）之前，本来就满足约束。
        # `_persisted` / `_finalized` 是 P1-b 新增的，原本的对应物（单一 `_saved`）写在
        # try 之前、即首个 await **之后**，所以必须在这里提前 —— 不能留在原处。
        # 两者分开而不是一个布尔：取消可能正好落在"已落库但还没落终态 / 还没
        # publish_end"之间，那种情况不该重复写库，但**必须**补完终态与收尾。
        _persisted = False
        _finalized = False
        _status_on_failure = run_registry.STATUS_INTERRUPTED

        invoke_kwargs = {}
        # 如果传了 model_config，通过 runtime context 传给 switch_model middleware
        if body.model_override:
            invoke_kwargs["context"] = {"model_config": body.model_override}

        # 标记当前是否刚发出过 thinking 事件
        thinking_emitted = False
        # 标记当前是否刚发出过 generating 事件（答案 token 阶段，区别于 thinking/CoT）
        generating_emitted = False
        # ─── 本轮时长埋点（持久化到 AI 消息 metadata，跨刷新保留秒数显示）───
        # _turn_started_at_ms：event_stream 进入时刻（每个 SSE 连接新建一次）
        # _reasoning_started_at_ms：首个 reasoning 事件触发时刻（首字节推理到达）
        # _reasoning_ended_at_ms：首个 token 事件触发时刻（推理结束 / 进入答案生成）
        # _turn_ended_at_ms：while 循环 break 时由内层 finally 兜底设置
        _turn_started_at_ms = int(time.time() * 1000)
        _reasoning_started_at_ms: int | None = None
        _reasoning_ended_at_ms: int | None = None
        _turn_ended_at_ms: int | None = None
        # [DEBUG] 记录上一个 langgraph_step，避免逐 token 重复打印
        _last_debug_step = None
        # 记录本次流式生成过程中产生/下载的文件
        # 后续保存到 store 时关联到对应的 AI 消息 entry，确保刷新页面后还能看到文件卡片
        _generated_files: list[dict] = []
        # 已推送 file_generated 事件的 file_path 集合（同会话内同一文件只推一次，
        # 覆盖写/重复 write_file 不再触发重复卡片——2026-08-11 16:00 实测重复报告）
        _emitted_files: set[str] = set()
        # 工具连续失败计数（跨 astream 轮共享声明，实际每轮在 _drain_astream 内重置）
        _consecutive_tool_failures = 0
        # M3 完成门：astream 前快照 /reports/ 目录，结束后做差集判定本轮产出
        _before_files = snapshot_report_files(settings.report_root, user_id, session_id)
        # 每轮 after-before 差集（磁盘真实文件名）；断连路径（finally）也可能引用，
        # 故在循环前初始化，避免 GeneratorExit 早抛时 NameError（2026-08-21）
        _new_files: frozenset[str] = frozenset()
        _completion_blocked = False
        # ─── 循环硬停信号（②A，2026-10-10）───
        # LoopDetectionMiddleware 的硬停（剥空 tool_calls）只让**图**这一轮收尾，
        # 本层原本完全不知情：M3 完成门随后按「/reports 有无新产物」判定，零产出就
        # 注入自动继续轮 —— 等于把刚掐断的循环重新推起来（a2719d7b 的"硬停未止损"）。
        # 现在中间件把硬停写进 state（loop_forced_stop），本层从 updates 事件捕获后
        # **直接终止本轮**、不再走自动继续轮，并发一条提示告知用户。
        # 生命周期 = 一轮 SSE（局部变量），且中间件 before_agent 每个 run 开头会清零
        # state 侧标记，双保险防止跨轮误判。
        _loop_forced_stop: dict | None = None
        # TerminalResponseMiddleware 降级信号（2026-09-16）：本轮以「空响应降级」收尾时
        # 由中间件登记、这里取走。命中即说明**模型层没有产出**（而非下游漏了文件步骤），
        # 故跳过 M3 完成门的零产出拦截，改发 fallback_notice() 的诚实文案，避免归因错位。
        # 见 src/core/terminal_response.py 模块头「与 M3 完成门的分工」。
        #
        # 生命周期铁律：信号的有效期 = 一轮 SSE。开轮先清残留（上一轮若在守卫登记后
        # 立刻断连、没走到消费点，它不会自己消失——留着会被**下一轮**当成自己的信号，
        # 把一轮正常的回复误报成"模型未产出"）。断连兜底路径同样要取走，见下方 finally。
        pop_fallback_signal(thread_id)
        # execute 通道护栏信号（2026-09-29 A+B）：与上面降级信号**同因同位置**，故并排。
        # 为什么必须在这里清：该信号的消费点只在收尾兜底分支内，且它一旦存在就会让
        # `_before_sandbox_files is not None and (... or _exec_violation is not None)`
        # 无条件成立（见下方 B② 触发条件）。上一轮若在 backend 登记信号之后立刻断连
        # （GeneratorExit 直接跳出 while 循环），永远走不到消费点，信号不会自己消失
        # ——残留到下一轮会：① 让一轮毫无越界行为的回复白跑一次沙箱兜底并打出误导性
        # WARNING；② 把 `_salvage_done` 提前置 True，使该轮**真正**的越界信号因
        # `not _salvage_done` 不成立而拿不到救济（B 在该轮失效）。故开轮先清。
        execution_guard.clear_execute_path_violation(user_id, session_id)
        _terminal_fallback: dict | None = None
        # M3 任务类型感知（方案 A）：仅"要求文件交付物"的任务启用零产出拦截，
        # 内容型任务（散文/问答）交付物即回复本身，零产出直接放行。
        _is_deliverable = _is_deliverable_task(body.message)
        # ─── B② 收尾兜底基线：沙箱侧交付物快照（2026-09-29）───
        # 失败置 None = 基线不可用 → 禁止兜底：没有基线就分不清"本轮新产物"与
        # "上一轮遗留"，硬拉会把旧文件算到本轮头上，制造假卡片（比不救更糟）。
        #
        # 取值条件**不限于** _is_deliverable 轮次（2026-09-29 A+B 改为"有沙箱就取"）：
        # B 的触发源是**轮中**才出现的 execute 越界写拒绝，而基线必须在轮首取——
        # 词表漏判时（60c64c4f 那轮英文 "appointment PPT" 未命中 pptx 强信号，
        # _is_deliverable=False）事后无法回头补基线，B 会全程失效。
        # 代价是每个带沙箱的轮次多一次 find（亚秒级），换来"任意轮次都能救回沙箱产物"，
        # 也让 B② 兜底不再受关键词词表漏判影响。
        _before_sandbox_files: dict[str, int] | None = None
        _salvage_done = False
        if sandbox is not None:
            try:
                _before_sandbox_files = await asyncio.to_thread(
                    _sandbox_artifact_listing, sandbox
                )
                logger.debug(
                    "[FILE_GEN] 沙箱产物基线: %d 个候选文件", len(_before_sandbox_files)
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[FILE_GEN] 沙箱产物基线枚举失败（放弃收尾兜底）: %s", e)
                _before_sandbox_files = None
        # graph_input 是 chat() 外层变量；event_stream 内若要重新赋值（完成门自动
        # 继续轮注入 SystemMessage），必须用独立局部变量 _graph_input，否则
        # Python 会把 graph_input 判定为 event_stream 局部变量，首轮读取即
        # UnboundLocalError（2026-08-04 M3 重构回归，已修复）
        _graph_input = graph_input

        # ─── 流式生成（多轮共用：首轮 + 完成门自动继续轮）───
        # P0 无进展循环检测状态（跨轮共享，收敛注入后重置）：
        # _last_tool_sig / _repeat_count：连续同一工具意图计数；
        # _files_in_window：本轮重复窗口内是否有新交付物（有则不算空转）
        _last_tool_sig = None
        _repeat_count = 0
        _files_in_window = 0
        _no_progress_injections = 0
        async def _drain_astream(_input):
            """消费一轮 agent.astream，把事件 publish 进桥；内部吞掉异常不崩本轮。
            M3-v2 继续轮传入 {'messages': [SystemMessage(...)]} 追加系统消息，
            依赖 deepagents astream 继续模式（spike 验证，未通过时自动继续保持关闭）。
            """
            nonlocal _last_debug_step, thinking_emitted, generating_emitted, _generated_files, _consecutive_tool_failures, _reasoning_started_at_ms, _reasoning_ended_at_ms
            nonlocal _last_tool_sig, _repeat_count, _files_in_window, _no_progress_injections, _graph_input, _no_progress_triggered
            nonlocal _loop_forced_stop
            _consecutive_tool_failures = 0  # 每轮 astream 重新计数（完成门继续轮独立统计）
            # ─── 清洗：messages 里不应有 SystemMessage ───
            # 主 system 由 deepagents 的 system_message 字段承载（factory 最内层
            # 前置 + memory middleware append_to_system_message），messages 中出现
            # system 均为异常数据：历史 checkpoint 可能残留旧版 P0 收敛注入的
            # SystemMessage（2026-08-19 09:44 旧代码注入后被保存进 state，09:57
            # 重启恢复后第一轮 model 调用即 400 'System message must be at the
            # beginning'——vLLM 要求 system 连续位于开头）。统一过滤掉。
            _raw_msgs = _input.get("messages", [])
            _filtered_system = sum(
                1 for m in _raw_msgs if getattr(m, "type", "") == "system"
            )
            if _filtered_system:
                logger.warning(
                    "[DIAG] astream 输入已清洗 %d 条中间 SystemMessage（checkpoint 残留）",
                    _filtered_system,
                )
            _input = {
                **_input,
                "messages": [
                    m for m in _raw_msgs if getattr(m, "type", "") != "system"
                ],
            }
            # 使用 try/except 保护，防止 agent.astream 内部异常导致 SSE 流中断
            try:
                _stream = agent.astream(
                    _input,
                    config={
                        **graph_config,
                        "recursion_limit": 200,  # ← 最多 200 步（原 100）：大文档任务（80+页×2 PDF 逐章节提取）实测 100 步不够，报告都没来得及写；正常流程 25-35 步，200 是 5-8 倍余量
                    },
                    stream_mode=["messages", "updates"],
                    **invoke_kwargs,
                )
                # 包静默保活：模型长思考不吐字节时本该发 SSE 注释行保活 TCP，
                # 防止代理 proxy_read_timeout（60s）掐断。
                # ⚠ P1-b：保活帧**不再由这里产生** —— 对客户端的 `": ping"` 改由桥的
                #   subscribe(heartbeat_interval) 在**订阅侧**发（无消费者自然不用发）。
                #   这里保留这条哨兵的真正理由变成了**驱动 turn_registry.beat**：
                #   台账的刷新源不能依赖 SSE chunk（没有订阅者时就没有 chunk）。
                async for mode, data in _astream_with_heartbeat(_stream, 15.0):
                    if mode == "__heartbeat__":
                        turn_registry.beat(thread_id)
                        continue

                    if (
                        mode == "messages"
                    ):  # mode == "messages" 时，data 是 (AIMessageChunk, metadata) 的元组
                        token, metadata = data
                        # 用 langgraph_step 变化检测"新一次 LLM 调用"，替代不存在的 run_id
                        if metadata:
                            step = metadata.get("langgraph_step")
                            node = metadata.get("langgraph_node")
                            # 只在 model 节点发送 ai_message_start（避免 tool 节点空触发）
                            if step is not None and step != _last_debug_step and node == "model":
                                # 新 step 到来前先通知前端：上一条 AI 消息结束
                                # 这是关键：让前端能正确分割多条 AIMessage
                                if _last_debug_step is not None:
                                    await _pub({'type': 'ai_message_start'})
                                _last_debug_step = step
                                thinking_emitted = False  # 新 step → 重置 thinking 标记
                                generating_emitted = False  # 新 step → 重置 generating 标记

                        # ─── 流式 token 处理：分离推理内容与回复内容 ───
                        _raw_content = token.content if hasattr(token, "content") else ""
                        # vLLM 0.19+（启用 reasoning-parser / 多模态后）流式 delta.content
                        # 可能不是 str 而是 OpenAI content-parts 数组
                        # （[{"type": "text", "text": "..."}, ...]）。这里统一归一化为 str，
                        # 防 str += list 的 TypeError
                        # （2026-09-09 实测：upload_to_sandbox 后正文输出 chunk 崩）。
                        # None（tool_calls chunk）与未知类型一律置 ""，保持原 falsy 语义，
                        # 避免 str(None)="None" 之类污染流内容。
                        if _raw_content is None:
                            content = ""
                        elif isinstance(_raw_content, list):
                            _text_parts: list[str] = []
                            for _p in _raw_content:
                                if isinstance(_p, str):
                                    _text_parts.append(_p)
                                elif isinstance(_p, dict) and _p.get("type") == "text":
                                    _text_parts.append(_p.get("text", ""))
                            content = "".join(_text_parts)
                        elif isinstance(_raw_content, str):
                            content = _raw_content
                        else:
                            content = ""

                        # 1. 优先从 additional_kwargs 提取推理内容（DeepSeek/Groq/Ollama/XAI 等）
                        reasoning = ""
                        if hasattr(token, "additional_kwargs") and token.additional_kwargs:
                            reasoning = (
                                token.additional_kwargs.get("reasoning_content") or ""
                            )

                        if reasoning:
                            # 模型通过 API 字段返回推理内容（不经过 content）
                            if not thinking_emitted:
                                thinking_emitted = True
                                # 持久化埋点：首个 reasoning 事件 → 记录推理起始时刻
                                if _reasoning_started_at_ms is None:
                                    _reasoning_started_at_ms = int(time.time() * 1000)
                                await _pub({'type': 'agent_status', 'status': 'thinking'})
                            await _pub_delta(DELTA_REASONING, reasoning)
                        elif content:
                            # 2. 正常 token：这是"写答案"阶段，状态应为 generating（生成中），
                            #    而非 thinking（思考中）。之前误把首个答案 token 标成 thinking，
                            #    导致整段答案生成都显示"思考中…"。
                            #
                            # 注：此处原为「content 内 </think> 标签兜底切分」分支
                            # （2026-07-13 cf81106 引入，服务当时未启 reasoning-parser 的 Qwen）。
                            # 该分支锚点是**闭标签**且默认假设"未见 </think> 即仍在思考"；一旦
                            # vLLM 的 reasoning-parser 把 thinking 剥进 reasoning 字段（当前部署
                            # --reasoning-parser qwen3），它的正确路径永不命中，而普通正文 chunk
                            # 恰好满足其条件 → 正文被无限吞进思考区。实测现象：1+1=2 也显示
                            # "思考过程"、散文正文混入思考块。
                            # 2026-09-10 删除，对齐 deepseek-harness 与 deer-flow 后端：流式路径
                            # 零标签解析，reasoning 只认 additional_kwargs.reasoning_content；
                            # 无 parser 模型的标签兜底改由前端渲染派生层承担（开标签锚点 +
                            # 乐观默认正文，物理上不可能吞正文）。请勿在后端流式归类里重加标签猜测。
                            if not generating_emitted:
                                generating_emitted = True
                                # 持久化埋点：首个 token 事件 → 推理结束 / 进入答案生成
                                # （覆盖路径：无 reasoning 内容直接进 token 的场景，如某些非 CoT 模型）
                                if _reasoning_ended_at_ms is None:
                                    _reasoning_ended_at_ms = int(time.time() * 1000)
                                await _pub({'type': 'agent_status', 'status': 'generating'})
                            await _pub_delta(DELTA_TOKEN, content)

                    elif mode == "updates":
                        # 根据 node 名称和输出内容解析 Agent 行为
                        for node_name, node_output in data.items():
                            # ─── 循环硬停信号捕获（②A，2026-10-10）───
                            # 必须放在「跳过 middleware 节点」名单之前：这个信号正是
                            # 由中间件节点（LoopDetectionMiddleware.after_model）产出的，
                            # 放在后面会被名单逻辑挡掉（本中间件目前不在名单里，但不能
                            # 依赖这个巧合）。中间件 before_agent 的清零会写
                            # loop_forced_stop=None，falsy ⇒ 天然不会被当成硬停。
                            if isinstance(node_output, dict):
                                _lfs = node_output.get(LOOP_FORCED_STOP_KEY)
                                if _lfs and _loop_forced_stop is None:
                                    _loop_forced_stop = _lfs
                                    logger.error(
                                        "[LOOP] 硬停生效，本轮将被强制终止（不再自动继续）: "
                                        "user=%s, session=%s, reason=%s, tool=%s, count=%s",
                                        user_id, session_id, _lfs.get("reason"),
                                        _lfs.get("tool_name")
                                        or ",".join(_lfs.get("tool_names") or []),
                                        _lfs.get("count"),
                                    )
                                    # 告知用户真实原因（此前只表现为"莫名其妙停了"）。
                                    # 用 completion_blocked 事件承载：前端已按
                                    # title/reason/hint 通用渲染，无需改前端；code 独立
                                    # 以便与"零产出/产物缺失/空响应降级"三类在复盘时区分。
                                    await _pub({
                                        'type': 'completion_blocked',
                                        'code': LOOP_FORCED_STOP_KEY,
                                        'title': '检测到工具调用循环，已强制收敛',
                                        'reason': (
                                            '本轮反复出现相同/高频的工具调用且迟迟不收敛，'
                                            '系统已强制停止工具调用并要求模型用已有结果作答。'
                                        ),
                                        'hint': (
                                            '若任务较复杂，建议拆成小步骤重发；'
                                            '若反复出现，请把会话 ID 反馈给模型服务维护方。'
                                        ),
                                        'terminated': True,
                                    })

                            # 跳过 middleware 节点（非 agent 关键节点）
                            if node_name in (
                                "SkillsMiddleware.before_agent",
                                "PatchToolCallsMiddleware.before_agent",
                                "MemoryMiddleware.before_agent",
                                "HumanInTheLoopMiddleware.after_model",
                                "TodoListMiddleware.after_model",
                            ):
                                continue

                            messages = (
                                node_output.get("messages")
                                if isinstance(node_output, dict)
                                else None
                            )
                            if (
                                not messages
                                or not isinstance(messages, list)
                                or len(messages) == 0
                            ):
                                continue

                            last_msg = messages[-1]

                            # ─── model 节点可观测性日志：AI 回复/思考摘要 + 工具调用列表 ───
                            # 屏蔽 langgraph print 后，模型输出不再出现在日志（原 values/updates
                            # 快照）；这里补一条精简日志，每步 model 一次（2026-08-13 补）
                            if node_name == "model" and hasattr(last_msg, "content"):
                                _ai_content = str(getattr(last_msg, "content", ""))
                                _tool_names = [
                                    tc.get("name", "?")
                                    for tc in (getattr(last_msg, "tool_calls", None) or [])
                                ]
                                logger.warning(
                                    "[DIAG] model: content=%s tool_calls=%s",
                                    (_ai_content or "(空)")[:150],
                                    _tool_names or [],
                                )

                            if (
                                node_name == "model"
                                and hasattr(last_msg, "tool_calls")
                                and last_msg.tool_calls
                            ):
                                # LLM 调用了工具
                                for tc in last_msg.tool_calls:
                                    await _pub({
                                                'type': 'tool_call',
                                                'tool': tc['name'],
                                                'args': tc['args'],
                                                'id': tc['id'],
                                            })
                                await _pub({'type': 'agent_status', 'status': 'running_tool', 'tool': last_msg.tool_calls[0]['name']})

                            elif node_name == "tools" and hasattr(last_msg, "name"):
                                # 工具执行结果
                                tool_name = last_msg.name
                                tool_call_id = getattr(last_msg, "tool_call_id", None)
                                # 结构化提取：MCP 工具返回 list[{'type':'text','text':...}]，
                                # 直接 str() 会得到 Python repr（单引号），json.loads 会失败，
                                # 进而误判 is_error（曾因 "error":null 命中 error 关键词把成功标为失败）
                                raw = last_msg.content
                                candidate = None
                                if isinstance(raw, list) and raw:
                                    first = raw[0]
                                    if isinstance(first, dict) and first.get("text"):
                                        candidate = first["text"]
                                elif isinstance(raw, dict) and raw.get("text"):
                                    candidate = raw["text"]
                                elif isinstance(raw, str):
                                    candidate = raw
                                content_str = str(candidate) if candidate else ""
                                # ─── 失败判定（2026-09-29 抽为纯函数便于单测）───
                                # 注意 status 优先：护栏拒绝的 ToolMessage(status="error")
                                # 文案不含 exit code/Execution error，旧关键词分支会把它
                                # 误判为成功 → 拒绝文案里的路径被当交付物解析。详见
                                # _tool_result_is_error docstring。
                                is_error = _tool_result_is_error(
                                    tool_name,
                                    content_str,
                                    getattr(last_msg, "status", None),
                                )

                                # ─── tool 节点可观测性日志：工具名 + 成功/失败 + 结果截断 ───
                                # 屏蔽 langgraph print 后工具调用过程不再出现在日志，
                                # 排查时看不到 AI 在调什么工具（2026-08-13 补）
                                logger.warning(
                                    "[DIAG] tool %s → %s | %s",
                                    tool_name,
                                    "OK" if not is_error else "FAIL",
                                    content_str[:150] if content_str else "(空)",
                                )
                                # skill workflow 阶段标记（M3 产物校验用）：
                                # 成功跑过 stage1/stage3 → 后续要求 reports 出现对应产物
                                if not is_error:
                                    if tool_name == "run_pdf_diff_stage1":
                                        _skill_state["stage1"] = True
                                    elif tool_name == "run_pdf_diff_stage3":
                                        _skill_state["stage3"] = True

                                await _pub({
                                            'type': 'tool_result',
                                            'tool': tool_name,
                                            'id': tool_call_id,
                                            'success': not is_error,
                                            # 2026-09-12：result 与持久化副本对齐同一上限，
                                            # 否则会出现「当轮看 2000、刷新后看 8000」的不一致。
                                            'error': _clip(
                                                content_str,
                                                settings.persist_max_error_chars,
                                            )
                                            if is_error
                                            else None,
                                            'result': _clip(
                                                content_str,
                                                settings.persist_max_tool_result_chars,
                                            )
                                            if content_str
                                            else None,
                                        })

                                # ─── 工具连续失败检测：超限提前终止，防止烧满 recursion_limit ───
                                # 模型在错误循环里空转时（如反复 glob 失败/read 失败），
                                # 每次 ToolMessage 都进 messages 撑大步数，直到 100 步才抛
                                # GraphRecursionError（几分钟白等）。这里数连续失败次数，
                                # 达到阈值立即 raise，由 except 分支转成 SSE error 事件止损。
                                if is_error:
                                    _consecutive_tool_failures += 1
                                    if (
                                        _consecutive_tool_failures
                                        >= settings.sandbox_tool_failure_threshold
                                    ):
                                        logger.warning(
                                            "[M3] 工具连续失败 %d 次（最近: %s），提前终止: user=%s, session=%s",
                                            _consecutive_tool_failures, tool_name, user_id, session_id,
                                        )
                                        raise ToolLoopAbortError(
                                            f"工具连续失败 {_consecutive_tool_failures} 次"
                                            f"（最近一次：{tool_name}），判定任务已无法继续，已提前终止"
                                        )
                                else:
                                    _consecutive_tool_failures = 0

                                # ─── P0 无进展循环检测：工具成功但反复同一意图 ───
                                # 模型在"工具全成功但任务未收敛"时空转（2026-08-17/18 两次
                                # Recursion limit 实证：execute 全 OK、报告已生成，模型仍不断
                                # "重新构建 diff.json"）。连续同一工具意图 + 窗口内零新交付物
                                # → 判定循环 → 抛 NoProgressAbortError 由外层注入收敛提示。
                                if not is_error:
                                    sig = _tool_intent_sig(tool_name, content_str)
                                    if sig == _last_tool_sig:
                                        _repeat_count += 1
                                    else:
                                        _last_tool_sig = sig
                                        _repeat_count = 1
                                    # 重复窗口内出现新交付物 → 重置（不是空转）
                                    if _files_in_window >= settings.no_progress_window_files:
                                        _files_in_window = 0
                                        _repeat_count = 0
                                    if (
                                        _repeat_count >= settings.no_progress_repeat_threshold
                                    ):
                                        if (
                                            _no_progress_injections
                                            < settings.no_progress_max_injections
                                        ):
                                            _no_progress_injections += 1
                                            logger.warning(
                                                "[P0] 无进展循环检测: 工具 %s 连续 %d 次同一意图且无新交付物"
                                                "（最近结果: %s）→ 注入收敛提示",
                                                tool_name, _repeat_count, content_str[:120],
                                            )
                                            raise NoProgressAbortError(
                                                f"检测到无进展循环：工具 {tool_name} 连续 "
                                                f"{_repeat_count} 次重复相同操作且无新产出"
                                            )
                                        else:
                                            # 收敛注入已耗尽仍未收敛 → 直接终止（防烧满 recursion_limit）
                                            logger.warning(
                                                "[P0] 无进展循环收敛注入已耗尽（%d 次），终止: 工具 %s 连续 %d 次同一意图",
                                                _no_progress_injections, tool_name, _repeat_count,
                                            )
                                            raise ToolLoopAbortError(
                                                f"检测到无进展循环：已注入 {_no_progress_injections} 次收敛提示"
                                                f"仍未收敛（最近: {tool_name} 连续 {_repeat_count} 次重复），"
                                                "判定任务无法继续，已提前终止"
                                            )

                                # ─── 检测工具返回的文件信息，提取生成/下载的文件 ───
                                # 不限定工具名，任何返回 /reports/ 路径的工具都能触发
                                file_path_virtual = None
                                # B①（2026-09-29）：原实现 `if not is_error` 一刀切跳过——
                                # 报错结果里的 /reports/... 同样可能指向**真实产物**
                                # （GY35377/60c64c4f 实测：`cp /home/user/x.pptx
                                #  /reports/.../x.pptx` 因沙箱内没有该目录而 exit 1，
                                #  源文件 x.pptx 好端端在 /home/user/ 下）。跳过 = 连
                                # "试着从沙箱拉回"的机会都没有。故放开解析，安全性由
                                # 下方 emit 护栏承担：**报错来源的路径只有拉回成功
                                # （file_size > 0）才允许 emit**，绝不凭一行报错文本发卡片。
                                if not is_error or _PARSE_ERROR_TOOL_RESULTS:
                                    # 1. 结构化解析：MCP 工具（如 download_from_sandbox）返回
                                    #    list[{'type','text'}] 或 dict，text 里是 JSON 字符串；
                                    #    直接用 json.loads 取路径字段，避免正则贪婪匹配吃进 JSON 尾巴
                                    #    （旧实现 re.search(r'/reports/\S+') 会把
                                    #    ","size":15758,"error":null 等尾巴一起吞掉 → 前端 404）
                                    parsed_json = None
                                    raw_content = getattr(last_msg, "content", None)
                                    candidate = None
                                    if isinstance(raw_content, list) and raw_content:
                                        first = raw_content[0]
                                        if isinstance(first, dict) and first.get("text"):
                                            candidate = first["text"]
                                    elif isinstance(raw_content, dict) and raw_content.get("text"):
                                        candidate = raw_content["text"]
                                    elif isinstance(raw_content, str):
                                        candidate = raw_content
                                    if isinstance(candidate, str):
                                        try:
                                            parsed_json = json.loads(candidate)
                                        except (json.JSONDecodeError, TypeError):
                                            parsed_json = None
                                    if isinstance(parsed_json, dict):
                                        for key in ("host_path", "path", "file_path", "output_path"):
                                            val = parsed_json.get(key)
                                            if isinstance(val, str) and val.strip():
                                                file_path_virtual = val.strip()
                                                break
                                    # 2. 回退：非 JSON 输出（write_file 的 "Updated file ..."）用正则硬抠
                                    if not file_path_virtual:
                                        if tool_name in ("write_file", "write", "create_file"):
                                            m = re.search(
                                                r"Updated file\s+(/\S+)", content_str
                                            )
                                            if m:
                                                file_path_virtual = m.group(1)
                                        else:
                                            # [^"\s,}\]]+ 匹配到中文等非分隔符字符为止（\w 不含中文）
                                            m = _REPORTS_PATH_RE.search(content_str)
                                            if m:
                                                file_path_virtual = m.group(0).rstrip(
                                                    '"'
                                                ).rstrip("}").rstrip(",")

                                # 规范化文件路径：如果 write_file 返回的路径是 WSL/宿主机完整路径
                                # （如 /mnt/d/.../data/reports/.../file），从中提取 /reports/... 部分
                                if file_path_virtual and not file_path_virtual.startswith("/reports/"):
                                    reports_m = _REPORTS_PATH_RE.search(file_path_virtual)
                                    if reports_m:
                                        file_path_virtual = reports_m.group(0)
                                        logger.debug(
                                            "[DIAG] 文件路径已规范化: path=%s",
                                            file_path_virtual,
                                        )

                                # 清洗 file_path_virtual（对称前端清洗）：上游返回的路径可能
                                # 尾随单引号/空白等脏字符（2026-08-24 实测 .../diff.json'），
                                # 不清洗则磁盘 404 → 前端 HEAD 失败 → 红框"文件不可用"卡。
                                if file_path_virtual:
                                    file_path_virtual = _sanitize_generated_path(file_path_virtual)

                                # 触发条件：/reports/ 交付物 + /skills/__agent__/ 自创 skill
                                # （A1：skill 文件也触发 file_generated，否则前端看不到 skill 卡片
                                #   —— 2026-08-11 15:28 实测 SKILL.md 写成功但前端"文件不可用"）
                                is_report_path = bool(
                                    file_path_virtual
                                    and file_path_virtual.startswith("/reports/")
                                )
                                is_agent_skill = bool(
                                    file_path_virtual
                                    and file_path_virtual.startswith("/skills/__agent__/")
                                )
                                if file_path_virtual and (is_report_path or is_agent_skill):
                                    filename = None
                                    file_size = 0
                                    if is_report_path:
                                        prefix = f"/reports/{user_id}/{session_id}/"
                                        if file_path_virtual.startswith(prefix):
                                            filename = file_path_virtual[len(prefix) :]
                                        # 磁盘大小：report_root → agent_workspace/data/reports 兜底
                                        # （write_file 可能写到了 /mnt/d/... 而不是 /data/myapp/...）
                                        for base in (
                                            settings.report_root,
                                            os.path.join(
                                                settings.agent_workspace, "data", "reports"
                                            ),
                                        ):
                                            try:
                                                disk_path = os.path.join(
                                                    base, user_id, session_id, filename
                                                )
                                                if os.path.isfile(disk_path):
                                                    file_size = os.path.getsize(disk_path)
                                                    break
                                            except Exception:
                                                pass
                                        # 方案A（2026-09-29）：磁盘缺失 → 从沙箱拉回
                                        # 持久化后再 emit（沙箱 execute 产物 TTL 回收即
                                        # 丢失，GY35377/60c64c4f pptx 404 实测根因）。
                                        # 此时沙箱刚执行完工具必然存活，拉回成功率最高。
                                        if file_size == 0 and filename:
                                            file_size = await _pull_report_from_sandbox(
                                                sandbox, file_path_virtual,
                                                user_id, session_id, filename,
                                            )
                                    else:  # /skills/__agent__/
                                        filename = file_path_virtual[
                                            len("/skills/__agent__/") :
                                        ]
                                        try:
                                            disk_path = os.path.join(
                                                settings.agent_workspace,
                                                "skills", "__agent__", filename,
                                            )
                                            if os.path.isfile(disk_path):
                                                file_size = os.path.getsize(disk_path)
                                        except Exception:
                                            pass
                                    # 如果磁盘没取到大小，尝试从 JSON 返回值中提取 size
                                    # B① 收窄：报错结果**不采信**"返回值里的 size"——那是
                                    # 工具/模型自称的数字，不是宿主磁盘事实；采信它等于绕过
                                    # 下面的 emit 护栏，凭空放行一张幽灵卡片。报错来源只认
                                    # "拉回成功"这一条事实来源（见 _pull_report_from_sandbox）。
                                    if file_size == 0 and not is_error:
                                        size_m = re.search(
                                            r'"size"\s*:\s*(\d+)', content_str
                                        )
                                        if size_m:
                                            file_size = int(size_m.group(1))

                                    # 目录路径不产生交付物：write_file 的 file_path 若为目录
                                    # （漏文件名，如 /reports/uid/sid/ 或 /reports/uid/sid），
                                    # 提取出的 filename 为空/会话ID，emit 后前端收到
                                    # file_name="" 的 file_generated → "(未知文件)" 卡片 +
                                    # 空 URL 请求 HEAD 307/401（2026-08-21 实测 350e5f80 会话；
                                    # 与 agent.py _reject_directory_write 双保险）
                                    if is_report_path and not filename:
                                        logger.warning(
                                            "[FILE_GEN] 忽略目录路径 file_generated: path=%s (无文件名)",
                                            file_path_virtual,
                                        )
                                        continue

                                    # ─── B① 安全护栏（2026-09-29）───
                                    # 来自**报错结果**的路径，只有真正拉回成功（file_size>0，
                                    # 即 _pull_report_from_sandbox 已把它落到宿主卷）才允许
                                    # emit。否则一行 `No such file or directory` 里的路径就会
                                    # 变成一张 HEAD 404 的红框卡片 —— 比不显示更糟：它让用户
                                    # 以为文件存在过。护栏放在这里（而非解析处），一次覆盖
                                    # /reports/ 与 /skills/ 两条分支。
                                    if is_error and file_size == 0:
                                        logger.info(
                                            "[FILE_GEN] 报错结果中的路径未拉回成功，跳过 emit"
                                            "（防幽灵卡片）: path=%s",
                                            file_path_virtual,
                                        )
                                        continue

                                    # B：同会话内同一文件只推送一次（覆盖写/重复 write_file 去重）
                                    if file_path_virtual in _emitted_files:
                                        # 事件已发过，仅更新 _generated_files 里的大小，不重复推送
                                        for _f in _generated_files:
                                            if _f["file_path"] == file_path_virtual:
                                                _f["file_size"] = file_size
                                                break
                                    else:
                                        _emitted_files.add(file_path_virtual)
                                        _files_in_window += 1  # P0：新交付物计数（非空转信号）
                                        await _pub({
                                                            'type': 'file_generated',
                                                            'file_name': file_path_virtual.split(
                                                                '/'
                                                            )[-1],
                                                            'file_path': file_path_virtual,
                                                            'file_size': file_size,
                                                            'file_type': _infer_file_type(
                                                                file_path_virtual.split('/')[-1]
                                                            ),
                                                        })
                                        # 同时记录到 _generated_files 列表，保存时按
                                        # tool_call_id 精确关联到**发起该工具调用**的那条
                                        # AI（2026-10-12：锚取自 ToolMessage.tool_call_id，
                                        # 与落盘时 AIMessage.tool_calls[].id 同源）。
                                        # 该键仅供 _attach_generated_files 内部归属用，
                                        # 不会写进 entry。
                                        _generated_files.append({
                                            "file_name": file_path_virtual.split('/')[-1],
                                            "file_path": file_path_virtual,
                                            "file_size": file_size,
                                            "file_type": _infer_file_type(
                                                file_path_virtual.split('/')[-1]
                                            ),
                                            "tool_call_id": tool_call_id,
                                        })
            except ToolLoopAbortError as e:
                # ─── 工具连续失败超限：预期内主动终止，不打印堆栈（非程序 bug）───
                # 只向前端发 error 事件（比 GraphRecursionError 快几十步）
                logger.warning("工具连续失败超限提前终止: %s", e)
                await _pub({'type': 'error', 'content': f'Agent 处理异常: {str(e)[:200]}'})
            except NoProgressAbortError as e:
                # ─── P0 无进展循环：中断本轮，注入收敛提示让模型收敛 ───
                # 触发后 _graph_input 被替换为收敛提示，外层 while 循环继续跑一轮；
                # 收敛轮再触发（超 no_progress_max_injections）则不再注入，
                # 由 except Exception 兜底转 error 终止（防无限收敛轮）。
                # 注意：必须用 HumanMessage 而非 SystemMessage——vLLM 要求所有
                # system 消息连续位于开头，本提示与 checkpoint 恢复的历史消息合并
                # 后不保证位置（2026-08-19 09:44:54 实测 400 'System message must
                # be at the beginning'；资源清单 13dab48→f093303 同款教训）。
                _no_progress_triggered = True  # 标记：收敛轮跳过 M3 零产出拦截
                logger.warning("[P0] 无进展循环，注入收敛提示: %s", e)
                await _pub({'type': 'agent_status', 'status': 'no_progress', 'content': f'{str(e)[:200]}'})
                # 收敛提示：让模型停止重复，验证已有产出并交付
                _graph_input = {"messages": [HumanMessage(
                    f"系统检测到无进展循环：你已连续多轮重复相同操作（{str(e)[:150]}）"
                    "且未产生新交付物。请立即收敛："
                    "1) 检查 /reports/{user_id}/{session_id}/ 下是否已有可交付的报告文件，"
                    "   如有则直接确认交付，停止重新生成；"
                    "2) 若确需重跑，先说明与上一轮的具体差异，一次完成，不要重复相同命令；"
                    "3) 若无法收敛，明确向用户说明卡点和已完成的产出。"
                )]}
                # 重置无进展计数（收敛轮重新统计），基线重设
                _last_tool_sig = None
                _repeat_count = 0
                _files_in_window = 0
                _before_files = snapshot_report_files(settings.report_root, user_id, session_id)
            except Exception as e:
                # ─── 异常保护：任何 agent.astream 内的异常都被捕获，不崩掉 SSE 流 ───
                logger.exception("Agent 流式处理异常: %s", e)
                await _pub({'type': 'error', 'content': f'Agent 处理异常: {str(e)[:200]}'})


        # ─── M3 完成门（设计文档）：系统校验本轮 /reports/ 产出，零产出不放行结束 ───
        # 判定不信任模型自陈：_generated_files（流式识别）与磁盘差集双保险，
        # 两者皆空即视为"任务未产出任何交付物"。
        # v2 自动继续：注入 SystemMessage 让模型再跑一轮（受 auto_continue / max_retries 开关控制，
        # 异常由 _drain_astream 内部吞掉 → 本轮零产出仍会递增重试，超限终止）。
        _completion_retries = 0
        # skill workflow 阶段标记（M3 产物校验用）：本轮是否成功跑过 stage1/stage3
        _skill_state = {"stage1": False, "stage3": False}
        # P0：NoProgressAbortError 触发标记——收敛轮跳过 M3 零产出拦截
        # （收敛轮刚注入提示，本轮零产出是预期的，不应被完成门拦截）
        _no_progress_triggered = False
        # P1-b：收尾状态位 `_persisted` / `_finalized` 刻意声明在本函数**开头**（见
        # 上面「收尾状态位」那个块），**不在这里** —— 从本行往下的第一个 await 是沙箱
        # 产物基线枚举（`asyncio.to_thread`），取消完全可能正好落在那里；而 finally
        # 一旦触发就要读这两个位，此刻才声明的话收尾自身会 NameError：把"丢一轮消息"
        # 升级成"连收尾都崩、run 行永远停在活跃态"。
        try:
            while True:
                await _drain_astream(_graph_input)
                _after_files = snapshot_report_files(settings.report_root, user_id, session_id)
                _new_files = _after_files - _before_files

                # ─── B② 收尾兜底：把沙箱里漏掉的产物拉回宿主 ───
                # 位置放在 M3 完成门**之前**：兜底救回来的产物应当让本轮正常放行，而不是
                # 先弹"未产出交付物"再补救（那是自相矛盾的 UX，正是本次要修的病）。
                # 单次门 _salvage_done：完成门重试轮不重复扫（差集已取过，重扫只是多花
                # 一次沙箱往返且无新信息）。
                #
                # 触发条件两个来源（2026-09-29 A+B 扩展）：
                #   ① 原条件（词表判交付 + 宿主零产出）；② 本轮 execute 命中虚拟路径护栏。
                # ②为什么必要：`_is_deliverable` 是**关键词启发式**，会漏判（实测
                # 60c64c4f：英文 "appointment PPT" 未命中强信号 "pptx" → 判 False →
                # 基线没取、兜底全程没生效）。而"模型试图往 /reports 写"这件事本身就是
                # **交付意图的强信号**，比词表可靠；此刻沙箱里极可能已经躺着它写出来的
                # 产物（先写 /home/user 再 cp /reports 的典型形态）。
                # A 是高频拦截层、不是完备保证（变量拼接/base64/通配符可绕），
                # B 不依赖命令解析、只比沙箱前后差集 ⇒ A 被绕过也不丢产物。
                # 🔭 治本方案 C（把宿主报告卷 bind-mount 进沙箱，通道不对称彻底消失）
                # 本次未做，理由与触发条件见 src/core/execution_guard.py 模块头「后续演进」。
                _exec_violation = execution_guard.peek_execute_path_violation(
                    user_id, session_id
                )
                if (
                    not _salvage_done
                    and _before_sandbox_files is not None
                    and (
                        (
                            not _new_files
                            and not _generated_files
                            and _is_deliverable
                        )
                        or _exec_violation is not None
                    )
                ):
                    _salvage_done = True
                    # 条件成立才 consume：条件不满足（如基线不可用）时把信号留给本轮
                    # 后续循环/下一轮，避免"吃掉信号却什么都没做"。
                    _exec_violation = execution_guard.consume_execute_path_violation(
                        user_id, session_id
                    )
                    if _exec_violation is not None:
                        logger.warning(
                            "[FILE_GEN] 命中 execute 越界写信号 → 强制收尾兜底: "
                            "user=%s, session=%s, cmd=%s",
                            user_id, session_id,
                            str(_exec_violation.get("command_head", ""))[:160],
                        )
                    _salvaged = await _salvage_sandbox_artifacts(
                        sandbox, user_id, session_id, _before_sandbox_files
                    )
                    for _sf in _salvaged:
                        if _sf["file_path"] in _emitted_files:
                            continue
                        _emitted_files.add(_sf["file_path"])
                        _files_in_window += 1  # 新交付物计数（非空转信号，与 B① 一致）
                        # 注意：沙箱兜底拉回**没有**对应的工具调用 ⇒ 不带 tool_call_id，
                        # 归属走 _attach_generated_files 的回退链（本轮最后一条带 tool_calls
                        # 的 AI）。这与流式一致：兜底在流尾 emit，前端也挂到当时最后一条 AI。
                        _generated_files.append(_sf)
                        await _pub({'type': 'file_generated', **_sf})
                    if _salvaged:
                        _after_files = snapshot_report_files(
                            settings.report_root, user_id, session_id
                        )
                        _new_files = _after_files - _before_files
                # ─── 空响应降级优先于 M3 零产出拦截（2026-09-16）───
                # 模型层一个字都没产出时，「/reports 为空」只是它的**后果**，
                # 不能按"漏了 download_from_sandbox / write_file"归因（de18ad37 实锤）。
                # 命中即上报真实原因并结束本轮——此时再多跑一轮完成门重试也无意义
                #（上游产出为空，重试同样拿不到交付物；auto_continue 开启时同理，
                #  因为根因是单次输出预算，不是"模型忘了下一步"）。
                _fallback = pop_fallback_signal(thread_id)
                if _fallback is not None and _fallback.get("quick_mode"):
                    # 关思考兜底**成功**：本轮有正文产出，绝不能走下面的完成门拦截
                    # （那会把一条正常回复误判成「模型未产出」）。只需告知用户这条是
                    # 「快速模式」产物 —— 关思考的产出与正常回复外观完全一致，不留痕
                    # 用户会当完整答案用（隐性失败比空白气泡更危险）。
                    # 注意：**不设** `_terminal_fallback` —— 那不是「降级收尾」，
                    # 设了会污染落库标记与断连路径的判断。
                    _qm_attempt = int(_fallback.get("attempt", 1) or 1)
                    _qm_reason, _qm_hint = quick_mode_notice(_qm_attempt)
                    logger.warning(
                        "[Guard] 本轮为关思考兜底产出（快速模式，第 %d 次重试）"
                        "→ 提示用户: user=%s, session=%s",
                        _qm_attempt, user_id, session_id,
                    )
                    await _pub({
                        'type': 'quick_mode_notice',
                        'code': 'thinking_disabled_fallback',
                        'title': '本轮为快速模式产出',
                        'reason': _qm_reason,
                        'hint': _qm_hint,
                        'terminated': False,
                    })
                    _fallback = None
                if _fallback is not None:
                    _terminal_fallback = _fallback
                    _ft_reason, _ft_hint = fallback_notice(
                        str(_fallback.get("branch", "")),
                        int(_fallback.get("retries", 0) or 0),
                    )
                    logger.warning(
                        "[M3] 空响应降级收尾 → 跳过零产出拦截并直报模型层原因: "
                        "user=%s, session=%s, branch=%s, finish_reason=%s, reasoning_len=%d",
                        user_id, session_id, _fallback.get("branch"),
                        _fallback.get("finish_reason"), int(_fallback.get("reasoning_len", 0) or 0),
                    )
                    await _pub({
                        'type': 'completion_blocked',
                        'code': 'model_output_truncated',
                        'title': '模型本轮未产出内容',
                        'reason': _ft_reason,
                        'hint': _ft_hint,
                        'terminated': False,
                    })
                    break
                if _no_progress_triggered:
                    # 收敛轮：跳过零产出拦截，直接继续下一轮（_graph_input 已换成收敛提示）
                    _no_progress_triggered = False
                    _before_files = _after_files  # 重新基线
                    continue
                if _loop_forced_stop is not None:
                    # ─── 硬停：本轮到此为止，绝不自动继续（②A，2026-10-10）───
                    # 这是整个 ② 的落点：中间件已把 tool_calls 剥空逼模型出纯文本，
                    # 若此处再让 M3 完成门按"零产出"注入自动继续轮，模型会被重新推回
                    # 循环 —— 硬停 → 完成门继续 → 再硬停，无限打转，正是 a2719d7b
                    # 「hard stop 未能真正止损」的最后一环。
                    # 注意：**不设** _completion_blocked —— 该标记会把最后一条 AI
                    # 消息写成 completion="blocked_no_output"（"零交付物"），而硬停轮
                    # 完全可能有产物（文件已 emit + 已入 _generated_files），标注会误导
                    # 复盘。硬停事实已由上面的 SSE 事件（code=loop_forced_stop）与
                    # ERROR 日志承载，二者在复盘时比这个标记更精确。
                    logger.error(
                        "[LOOP] 硬停轮终止（跳过 M3 完成门自动继续）: user=%s, session=%s, "
                        "reason=%s, generated=%d, new_files=%d",
                        user_id, session_id, _loop_forced_stop.get("reason"),
                        len(_generated_files), len(_new_files),
                    )
                    break
                if _generated_files or _new_files:
                    # 有本轮产出 → 再校验 skill 工作流关键产物（跑过 stage1/stage3 时）。
                    # 缺失即"流程没走完"（如 stage1 产物没拉回 reports），不放行，
                    # 注入收敛提示继续——不信任模型自陈"做完了"。
                    _missing = _missing_skill_artifacts(
                        settings.report_root, user_id, session_id,
                        _skill_state["stage1"], _skill_state["stage3"],
                    )
                    if not _missing:
                        break  # 产物齐，放行
                    if (
                        _completion_retries >= settings.sandbox_completion_gate_max_retries
                        or not settings.sandbox_completion_gate_auto_continue
                    ):
                        _completion_blocked = True
                        logger.warning(
                            "[M3] skill 产物缺失且重试耗尽: user=%s, session=%s, missing=%s",
                            user_id, session_id, _missing,
                        )
                        await _pub({
                            'type': 'completion_blocked',
                            'code': 'skill_artifact_missing',
                            'title': 'skill 工作流产物缺失',
                            'reason': f'skill 工作流产物缺失: {_missing}',
                            'hint': '请检查是否遗漏 download_from_sandbox 拉回产物',
                            'terminated': True,
                        })
                        break
                    _completion_retries += 1
                    logger.warning(
                        "[M3] skill 产物缺失，注入收敛继续（%d/%d）: missing=%s",
                        _completion_retries, settings.sandbox_completion_gate_max_retries, _missing,
                    )
                    _graph_input = {"messages": [HumanMessage(
                        f"系统校验：本轮 skill 工作流关键产物缺失：{'、'.join(_missing)}。"
                        f"请立即用 download_from_sandbox 将对应文件拉回 /reports/{user_id}/{session_id}/，"
                        "然后继续完成。禁止自写替代代码或跳过流程步骤。"
                    )]}
                    _before_files = _after_files  # 重新基线
                    continue
                if not settings.sandbox_completion_gate_enabled or not _is_deliverable:
                    # 完成门关闭（回滚开关），或本轮为非文件交付任务（内容型任务如
                    # 散文/问答，交付物即对话回复本身）→ 零产出是正常结果，直接放行。
                    break
                if (
                    _completion_retries >= settings.sandbox_completion_gate_max_retries
                    or not settings.sandbox_completion_gate_auto_continue
                ):
                    _completion_blocked = True
                    logger.warning(
                        "[M3] 完成门拦截（本轮零产出）: user=%s, session=%s, generated=%d, new_files=%d, retries=%d",
                        user_id, session_id, len(_generated_files), len(_new_files), _completion_retries,
                    )
                    # C② 反幻觉（2026-09-29）：模型常在**未做任何验证**的情况下于正文声称
                    # "已保存/可直接下载"（GY35377/60c64c4f 实测：连续四轮零工具调用纯复述，
                    # 同屏还挂着"本轮未产出交付物"横幅，界面自相矛盾）。
                    # 零产出是磁盘事实，故这里**明确否定**该类表述——与本门"只描述事实、
                    # 不做无据推测"的原则不冲突：否定句同样是事实断言（文件确实不存在），
                    # 不是对"模型漏了哪一步"的猜测。
                    # 措辞随核查范围变化：B② 沙箱兜底跑过才有资格说"沙箱也没有"。
                    _verified_scope = (
                        "宿主报告目录与沙箱均已核查，均无本轮新交付物"
                        if _salvage_done
                        else "宿主报告目录无本轮新交付物"
                    )
                    await _pub({
                        'type': 'completion_blocked',
                        'code': 'no_artifact_in_reports',
                        'title': '本轮未产出交付物',
                        'reason': f'本轮任务未产出任何交付物（{_verified_scope}）；'
                                  '若上文中出现“已保存/已生成/可直接下载”一类说法，该说法不成立，'
                                  '请勿据此操作（文件不存在）',
                        # 措辞只描述事实 + 条件式建议：本门只看得到磁盘差集，看不到
                        # 模型是否漏了哪一步，原文"请检查是否遗漏 ..."是无据推测
                        #（2026-09-16 会话 de18ad37 因"输出"×"表格"顺带词误命中而错报）。
                        # 模型层真没产出的情况已由 terminal_response 降级信号在更早处分流。
                        'hint': '若任务本应产出文件，请检查是否遗漏 download_from_sandbox / write_file 步骤',
                        'terminated': _completion_retries >= settings.sandbox_completion_gate_max_retries,
                    })
                    break
                _completion_retries += 1
                logger.warning(
                    "[M3] 零产出，注入 SystemMessage 自动继续（第 %d/%d 次）: user=%s, session=%s",
                    _completion_retries, settings.sandbox_completion_gate_max_retries, user_id, session_id,
                )
                _graph_input = {"messages": [SystemMessage(
                    f"系统检测：/reports/{user_id}/{session_id}/ 当前无本轮新文件，本轮任务未产出任何交付物。"
                    "请检查是否遗漏 download_from_sandbox / write_file 步骤，并继续完成。"
                )]}
                _before_files = _after_files  # 重新基线
            # ─── 正常收尾（顺序有语义，见 _finish_run）───────────────────────────
            # 刻意放在 try **内**：取消可能正好落在这段里（POST /cancel 或进程关停），
            # 此时下面的 finally 必须能看出"收尾没做完"并补一次。
            # turn_ended 兜底：while 循环有 4 个 break 出口都没主动设 turn_ended，
            # 这里统一设值为 now（持久化时刻），这样无论 break / 异常早退 / 被取消，
            # 都至少能算到"持久化时刻"。偏差是 _persist_session 的执行耗时
            #（读 checkpoint + 写 store，几十到几百毫秒，可忽略）。
            if _turn_ended_at_ms is None:
                _turn_ended_at_ms = int(time.time() * 1000)
            await _persist_session(
                agent, thread_id, user_id, session_id, store, body,
                generated_files=_generated_files,
                disk_files=_new_files,
                completion_blocked=_completion_blocked,
                terminal_fallback=_terminal_fallback,
                reasoning_started_at_ms=_reasoning_started_at_ms,
                reasoning_ended_at_ms=_reasoning_ended_at_ms,
                turn_started_at_ms=_turn_started_at_ms,
                turn_ended_at_ms=_turn_ended_at_ms,
                reason="normal",
            )
            _persisted = True
            # release → 落终态 → publish_end（= 前端看到的 `data: [DONE]`）
            await _finish_run(run_id, run_registry.STATUS_COMPLETED)
            _finalized = True
        except BaseException as e:
            # 含 CancelledError（POST /cancel 与进程关停都走这条）。这里只做**分类与
            # 记录**，真正的收尾统一在 finally 里做 —— 收尾本身要 await，而本 task
            # 此刻已在取消中，只能交给脱离取消作用域的后台任务。
            if isinstance(e, asyncio.CancelledError):
                logger.warning(
                    "[RUN] 本轮被取消（用户终止任务 / 进程关停），收尾移交后台任务: "
                    "run=%s user=%s session=%s",
                    run_id, user_id, session_id,
                )
            else:
                _status_on_failure = run_registry.STATUS_FAILED
                logger.exception("[RUN] 本轮异常退出，收尾移交后台任务: run=%s", run_id)
            raise
        finally:
            _beat_task.cancel()
            # detach 是非阻塞的纯 contextvar 复位，先做：观测侧动作不能拖住后面的收尾。
            if _ctx_token is not None:
                try:
                    _otel_context_api.detach(_ctx_token)
                except Exception:
                    logger.debug("[TRACING] 恢复 OTel Context 失败（已忽略）", exc_info=True)

            # ★ 置位必须**无条件**、且先于任何 create_task（1.0.28 修复，2026-10-12）。
            #   `_worker_finalize_started` 的语义是「收尾责任已由 worker 的 finally
            #   接手」；done-callback 的兜底**只该**覆盖「连 finally 都跑不到」的那个
            #   窗口（开轮 prologue 的 await 被取消/抛异常，见 _on_worker_done）。
            #   原实现把它放在 `if not _finalized:` **内部**，于是正常完成路径
            #   （_finalized=True）从不置位 ⇒ done-callback 一路走到兜底，用
            #   `_finish_run(FAILED)` 把刚落好的 COMPLETED 覆盖成 failed
            #   （_finish_run 刻意不幂等：它无条件 run_registry.mark）。
            #   1.0.27 线上实证：每轮正常对话都留下一条
            #     「[RUN] worker 未进收尾机制，执行兜底收尾 … status=failed」；
            #   run 行终态恒为 failed（正常完成 / 主动断连两种场景均复现）。
            _worker_finalize_started["started"] = True

            if not _finalized:
                # 中断 / 异常 / 收尾中途被打断：把剩余收尾交给**脱离取消作用域**的后台任务。
                #
                # ⚠ 这里**绝不能 await**：本 task 已在取消中，await 会在第一个 await 点抛
                #   CancelledError，收尾整段蒸发（4863afff 事故现场：打了「SSE 流中断」
                #   之后一条日志都没有；实证见 tests/spikes/sse_disconnect_repro.py 的
                #   direct 场景）。
                try:
                    # 中断路径也要取走降级信号（若守卫已登记但本轮没走到消费点）：
                    # 否则它会以 thread_id 为键留在登记表里被下一轮消费，造成误报。
                    if _terminal_fallback is None:
                        _terminal_fallback = pop_fallback_signal(thread_id)
                    _interrupted_end = (
                        _turn_ended_at_ms
                        if _turn_ended_at_ms is not None
                        else int(time.time() * 1000)
                    )
                    _spawn_detached_save(
                        _persist_session_and_release(
                            run_id,
                            persisted=_persisted,
                            status=_status_on_failure,
                            agent=agent,
                            thread_id=thread_id,
                            user_id=user_id,
                            session_id=session_id,
                            store=store,
                            body=body,
                            generated_files=_generated_files,
                            disk_files=_new_files,
                            completion_blocked=_completion_blocked,
                            terminal_fallback=_terminal_fallback,
                            reasoning_started_at_ms=_reasoning_started_at_ms,
                            reasoning_ended_at_ms=_reasoning_ended_at_ms,
                            turn_started_at_ms=_turn_started_at_ms,
                            turn_ended_at_ms=_interrupted_end,
                            reason="interrupted",
                        ),
                        label=f"finalize-interrupted:{session_id}",
                    )
                except BaseException as e:
                    # 保底：连 create_task 都失败（事件循环将关闭等）也必须可见，
                    # 绝不静默 —— 走到这里意味着本轮消息真的会丢。
                    logger.error(
                        "[RUN] 中断收尾**无法启动**（关键错误，本轮消息可能丢失）: %s",
                        e, exc_info=True,
                    )

    # ─── 登记轮次（P1-c D5：从 worker 提前到端点）─────────────────────────────────
    # 原来只在 _run_worker 里 acquire。现在 setup 阶段要往 store 预写本轮 user 消息，
    # 而删会话端点的护栏查的正是这张表 —— 若仍等 worker 起来才登记，删除请求可以抢在
    # 预写之前完成，我们随后就把消息写进一个**已被删除**的会话（孤儿行）。抢先一步
    # 登记即可把这个窗口关掉。acquire 是同步 dict 写且幂等，worker 里那句再调一次
    # 只是刷新时刻戳，无副作用。
    turn_registry.acquire(thread_id)

    try:
        # D5：预写本轮 user 消息（失败只告警，绝不阻断本轮 —— 流尾会兜底）
        if setup_user_message_id is not None:
            await _write_setup_user_message(
                store,
                user_id=user_id,
                session_id=session_id,
                message_id=setup_user_message_id,
                content=setup_user_message_content,
            )

        # ─── 登记 run（P1-b 的核心：run 不再随连接存活）──────────────────────────
        # 顺序上刻意放在 setup **之后**：setup 里的失败（沙箱磁盘不足 503 等）要保持
        # HTTP 语义 —— 那是"请求能不能开始"，必须同步告诉调用方；而 run 一旦开始，
        # 就不该再由连接决定生死。
        run = run_registry.create(user_id=user_id, session_id=session_id, thread_id=thread_id)
        # 落行，供"刷新后查 active-run"使用（写失败不抛，见 run_registry.persist）
        await run_registry.persist(store, run)

        # ⚠ 必须 create_task，**不能**在响应生成器里 await：
        #   worker 要脱离**请求 task** 的取消作用域 —— 客户端断连时 Starlette 取消的是
        #   请求 task，而 create_task 出来的任务不在它的 task group 里，取消不会传染。
        #   这正是"刷新不再杀任务"的落点（同一手法的实证见本文件顶部 _spawn_detached_save
        #   与 tests/spikes/sse_disconnect_repro.py 的 detached 场景）。
        _worker = asyncio.create_task(_run_worker(run), name=f"run:{run.run_id}")
    except BaseException:
        # 还没把 run 交给 worker（上面任何一步炸了，含请求被取消）⇒ 台账必须回退。
        # 不回退的话这个会话会因为"看起来还有轮次在跑"而在 STALE_MS（5 分钟）内删不掉，
        # 而实际上根本没有 worker 会来 release 它。
        turn_registry.release(thread_id)
        raise

    run_registry.attach_task(run.run_id, _worker)
    _worker.add_done_callback(_on_worker_done)

    logger.warning(
        "[RUN] 本轮已启动（脱离连接）: run=%s user=%s session=%s",
        run.run_id, user_id, session_id,
    )
    return _sse_response(run.run_id, bridge)


# ─── P1-b 新增端点 ①：续订（刷新 / 断线后重新挂上同一个 run）──────────────────


@router.get("/chat/runs/{run_id}/stream")
async def resume_run_stream(
    run_id: str,
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
    last_event_id_query: str | None = Query(default=None, alias="last_event_id"),
    bridge=Depends(get_stream_bridge),
    current_user: dict = Depends(get_current_user),
):
    """订阅一个**已经存在**的 run。与 POST /chat 的唯一区别：这里不启动任何东西。

    游标有两个入口，优先取标准头：浏览器原生 `EventSource` 重连时会自动带
    `Last-Event-ID`；本仓前端走 fetch + ReadableStream，得自己填。同名 query 参数
    是给手工重放 / 调试用的兜底（用 curl 塞头不方便）。游标缺失或格式不符**不报错**
    —— 桥会退化成"从头重放"，代价只是多收一段；报错会让用户以为任务没了。

    为什么必须先校验**归属**：桥的 `subscribe` 对未知 run_id 是**惰性建流**的
    （见 MemoryStreamBridge._get_or_create_stream）——不校验的话，任何编造的 run_id
    都能换到一个永远只吐心跳的连接：既是资源消耗面，也是纯粹的进程内泄漏。
    所以顺序固定为：查登记表 → 校验归属 → 才允许订阅。

    已进入终态但尚未被回收（RUN_REAP_DELAY_SECONDS 宽限期内）的 run 仍可订阅，且
    会立刻拿到重放窗口 + `[DONE]` —— "刷新时任务刚好结束"走的正是这条路。回收之后
    才返回 404，此时前端应改走 `GET /sessions/{sid}/active-run` 拿 null 并回权威
    存储重取（P1-c）。
    """
    rec = run_registry.get(run_id)
    # 不区分"不存在"与"不属于你"：统一 404，避免把状态码当成探测别人 run_id 的探针。
    if rec is None or rec.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="run 不存在")

    last_event_id = last_event_id_header or last_event_id_query

    logger.info(
        "[RUN] 续订: run=%s user=%s session=%s status=%s cursor=%s",
        run_id, rec.user_id, rec.session_id, rec.status, last_event_id or "(none)",
    )
    return _sse_response(run_id, bridge, last_event_id=last_event_id)


# ─── P1-b 新增端点 ②：显式终止（「终止任务」按钮的新落点）─────────────────────


@router.post("/chat/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    current_user: dict = Depends(get_current_user),
):
    """显式终止一个 run：取消它的 worker task。**只取消，不在这里落状态。**

    为什么不在这里 mark(interrupted) / publish_end —— 本仓既有的顺序不变量（见
    `_finish_run`）：前端把 `data: [DONE]` 当作"本轮结束"的唯一判据，收到它就解锁
    编辑；因此 [DONE] 必须**晚于**消息落盘，否则编辑用的 from_index 会越界
    （2026-08 实测）。所以落库/终态/[DONE] 一律由 worker 被取消后的收尾链完成
    （finally → 脱离取消作用域的后台任务）。这里若抢先发 [DONE]，等于亲手造出
    "前端已解锁、消息却没落盘"的窗口 —— 正是本次改造要消灭的那类时序缺陷。

    返回体里的 `status` 因此是取消**前**的活跃态：它是"已受理"的凭据而非终态。
    终态请以 `[DONE]` 或 `GET /sessions/{sid}/active-run` 为准。
    """
    rec = run_registry.get(run_id)
    if rec is None or rec.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="run 不存在")

    if not rec.is_active:
        # 幂等：已经结束了（正常完成 / 已被终止 / 失败）。不报错 —— 前端可能因网络
        # 重试而重复提交终止，报错只会弹一个对用户毫无意义的红框。
        logger.info(
            "[RUN] 终止请求落在已结束的 run 上（幂等返回）: run=%s status=%s",
            run_id, rec.status,
        )
        return {"run_id": run_id, "cancelled": False, "status": rec.status}

    target = rec.task
    if target is None or target.done():
        # 窗口极小但真实存在：① run 已 create、worker task 还没 attach（persist 的
        # await 期间）；② worker 已退出、但兜底收尾还没把状态推成终态。两种都没有可
        # 取消的目标 —— 如实回报"没取消到"，而不是谎报成功。
        logger.warning(
            "[RUN] 终止请求无可取消目标（worker 未 attach 或已退出）: run=%s status=%s",
            run_id, rec.status,
        )
        return {"run_id": run_id, "cancelled": False, "status": rec.status}

    target.cancel()
    logger.warning(
        "[RUN] 已请求终止: run=%s user=%s session=%s",
        run_id, rec.user_id, rec.session_id,
    )
    return {"run_id": run_id, "cancelled": True, "status": rec.status}
