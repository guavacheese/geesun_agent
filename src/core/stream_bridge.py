"""进程内事件桥：把 run 的生产与消费解耦（P1-a，2026-10-12）。

## 为什么需要它

现状（改造前）里 **run 的生命周期 == SSE 连接的生命周期**：`chat.py` 把业务逻辑
写在一个 async generator 里，由 Starlette 的 `StreamingResponse` 逐条 `__anext__`
驱动。于是浏览器刷新 / 切走 / 关页 → 请求 task 被取消 → `GeneratorExit` 沿调用栈
杀穿到 `agent.astream` → **这一轮任务被销毁**。用户看到的是"刷新一下任务就没了"。

本模块把"产生事件的 run"与"消费事件的连接"拆开：run 往桥上 `publish`，
SSE 端点从桥上 `subscribe`。订阅者来去不再影响 run（对齐 deer-flow
`runtime/stream_bridge/{base,memory}.py`）。

## 与 deer-flow 的关键差异：事件粒度

deer-flow 桥里的事件是**粗粒度**的（`updates` / `events`，一个 step 一条），
所以它 `queue_maxsize=256` 的有界日志够用。而我们的 `token` / `reasoning` 是
**逐 token** 的——几百字回答就是几百条，256 条缓冲会被瞬间冲掉，重连必撞
`StreamGap` 且正文全丢。

故本桥把两类事件**分流**（deer-flow 里没有这一层）：

| 类别 | 事件 | 存放 | 重连时如何补齐 |
|---|---|---|---|
| 结构性 | `ai_message_start` / `tool_call` / `tool_result` / `file_generated` / `error` / `agent_status` | **有界日志**（默认 512 条）+ 序号游标 | 按 `Last-Event-ID` 重放；越界则给 `StreamGap` |
| 逐 token | `token` / `reasoning` | **累积器**（只留尾部 N 字符） | 首帧 `StreamSnapshot` 给权威正文 |

## 帧语义（线上协议由 P1-b 负责落地为 SSE）

- `StreamSnapshot` —— **替换语义**：把它当作当前可见正文/推理的权威值。
  订阅开始恒发一帧（worker 可能先于订阅者启动，那段 token 只存在于累积器里）。
- `StreamDelta` —— **追加语义**：`kind` 为 `token` / `reasoning` 的增量文本。
- `StreamEvent` —— 结构性事件，`id` 形如 `{ts}-{seq}`（`seq` 即该 run 内结构性
  事件的绝对序号，可直接当 SSE 的 `id:` 字段做游标）。
- `StreamGap` —— 游标已被裁剪，重放不完整，调用方应改用快照 + 当前尾部续订。
- `HEARTBEAT_SENTINEL` / `END_SENTINEL` —— 空转保活 / 生产结束。

"快照=替换、增量=追加"这个分工让**队列溢出可以自愈**：慢订阅者的队列塞满时，
生产者清队列并放一个重同步标记，消费者侧据此重新对齐——补一帧快照（正文，替换
语义）**加上**从有界日志重放他还没确认收到的结构性事件。两条通道都不丢：
逐 token 走快照、结构性事件走日志重放。（只补快照是不够的：那会丢掉队列里正躺着的
`file_generated` / `ai_message_start`，前端就少一张产物卡片。）

## 调用契约

1. `subscribe()` 返回的异步生成器**必须显式关闭**（`contextlib.aclosing` / `aclose()`）。
   `async for` 里 break/return 不会自动关闭异步生成器，不关就留下幽灵订阅者。
2. 首帧恒为 `StreamSnapshot`，调用方应把它当作"当前可见正文的权威值"（替换）。
3. `StreamEvent.id` 应原样写进 SSE 的 `id:` 字段；重连时把浏览器的 `Last-Event-ID`
   传回来即可续订。

## 单副本前提

进程内实现，不跨进程（`supports_cross_process = False`）。生产 stack 全部服务
1/1 副本（同 `turn_registry.py` 的设计前提）；扩副本时需换 Redis 桥，接口不变。
"""

from __future__ import annotations

import abc
import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_EVENT_ID_RE = re.compile(r"\d+-(\d+)")

#: 逐 token 事件的两个 kind。放在模块级常量里，避免各处硬编码字符串写错。
DELTA_TOKEN = "token"
DELTA_REASONING = "reasoning"
DELTA_KINDS: tuple[str, ...] = (DELTA_TOKEN, DELTA_REASONING)


# ─────────────────────────── 帧类型 ───────────────────────────


@dataclass(frozen=True)
class StreamEvent:
    """结构性事件。

    `id`：`{ts}-{seq}`，`seq` 是该 run 内结构性事件的绝对序号（从 0 起，不受
    有界日志裁剪影响）——正因为它与绝对偏移一一对应，重连时可以用算术 O(1)
    定位，不必扫描保留缓冲（同 deer-flow `_parse_event_seq` 的取向）。
    """

    id: str
    event: str
    data: Any


@dataclass(frozen=True)
class StreamSnapshot:
    """权威正文快照（替换语义）。逐 token 事件不进有界日志，重连只能靠它补齐。"""

    content: str
    reasoning: str
    #: 因缓冲上限被丢弃的头部字符数（>0 时前端应提示"开头部分未回放"）
    content_dropped_chars: int = 0
    reasoning_dropped_chars: int = 0


@dataclass(frozen=True)
class StreamDelta:
    """逐 token 增量（追加语义）。"""

    kind: str
    text: str


@dataclass(frozen=True)
class StreamGap:
    """订阅游标已被裁剪，无法完整重放。"""

    requested_event_id: str | None
    earliest_available_event_id: str
    latest_available_event_id: str


HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)
END_SENTINEL = StreamEvent(id="", event="__end__", data=None)


class _ResyncMarker:
    """内部标记（**不对外暴露**）：慢订阅者队列溢出，消费者据此重新对齐。

    刻意不做成 `StreamEvent`：它若混进事件分支会被当成结构性事件下发给前端。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return "<RESYNC>"


RESYNC_MARKER = _ResyncMarker()

type StreamItem = StreamEvent | StreamSnapshot | StreamDelta | StreamGap
type _QueueItem = StreamItem | _ResyncMarker


# ─────────────────────────── 抽象接口 ───────────────────────────


class StreamBridge(abc.ABC):
    """生产者 / 消费者之间的桥。接口刻意保持最小（同 deer-flow base.py）。"""

    supports_cross_process: bool = False

    @abc.abstractmethod
    async def publish(self, run_id: str, event: str, data: Any) -> None:
        """发布一个结构性事件（生产者侧）。"""

    @abc.abstractmethod
    async def publish_delta(self, run_id: str, kind: str, text: str) -> None:
        """发布一段逐 token 增量（生产者侧）。`kind` ∈ :data:`DELTA_KINDS`。"""

    @abc.abstractmethod
    async def publish_end(self, run_id: str) -> None:
        """声明该 run 不会再产出事件。"""

    @abc.abstractmethod
    def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamItem]:
        """订阅（消费者侧）。首帧恒为快照，其后是重放/实时事件。"""

    @abc.abstractmethod
    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        """释放该 run 的资源；`delay > 0` 时先等一会儿，给迟到的订阅者机会。"""

    async def close(self) -> None:  # pragma: no cover - 默认空实现
        """释放后端资源。默认无操作。"""


# ─────────────────────────── 内存实现 ───────────────────────────


@dataclass
class _Subscriber:
    """一个订阅者的投递给队列。

    为什么用"每订阅者一个队列"而不是"共享日志 + 按字符游标"：逐 token 事件下
    共享游标方案要么每次唤醒扫描整个缓冲（O(n²)），要么得维护"块索引 + 块内偏移"
    并在头部裁剪时做偏移重算（易错）。队列方案把"新文本"直接点对点投递，
    O(1)/事件；重连/溢出这两条异常路径才付一次 join 的代价。
    """

    queue: asyncio.Queue[_QueueItem]


@dataclass
class _RunStream:
    """单个 run 的内存态。"""

    #: 结构性事件的有界日志
    events: list[StreamEvent] = field(default_factory=list)
    #: 结构性事件绝对序号（== len(events) + 已裁剪数）
    seq: int = 0
    #: 已被裁剪的结构性事件条数（= 日志首条的绝对序号）
    start_offset: int = 0
    #: 逐 token 累积器（只留尾部），按 kind 分
    chunks: dict[str, list[str]] = field(
        default_factory=lambda: {k: [] for k in DELTA_KINDS}
    )
    #: 累积器当前字符数（避免每次 len("".join(...)) 的 O(n)）
    chunk_chars: dict[str, int] = field(
        default_factory=lambda: {k: 0 for k in DELTA_KINDS}
    )
    #: 因上限被丢弃的头部字符数
    dropped_chars: dict[str, int] = field(
        default_factory=lambda: {k: 0 for k in DELTA_KINDS}
    )
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    subscribers: list[_Subscriber] = field(default_factory=list)
    ended: bool = False


class MemoryStreamBridge(StreamBridge):
    """进程内桥：有界结构性日志 + 尾部保留的正文累积器 + 每订阅者队列。"""

    def __init__(
        self,
        *,
        queue_maxsize: int = 512,
        text_max_chars: int = 200_000,
    ) -> None:
        if queue_maxsize <= 0:
            raise ValueError("queue_maxsize 必须为正整数")
        if text_max_chars <= 0:
            raise ValueError("text_max_chars 必须为正整数")
        self._maxsize = queue_maxsize
        self._text_max_chars = text_max_chars
        self._streams: dict[str, _RunStream] = {}

    # -- 内部工具 --------------------------------------------------------------

    def _get_or_create_stream(self, run_id: str) -> _RunStream:
        stream = self._streams.get(run_id)
        if stream is None:
            stream = _RunStream()
            self._streams[run_id] = stream
        return stream

    def _next_id(self, stream: _RunStream) -> str:
        seq = stream.seq
        stream.seq += 1
        return f"{int(time.time() * 1000)}-{seq}"

    @staticmethod
    def _parse_event_seq(event_id: str) -> int | None:
        """从 `{ts}-{seq}` 取出绝对序号；格式不符返回 None。"""
        match = _EVENT_ID_RE.fullmatch(event_id)
        return int(match.group(1)) if match is not None else None

    @staticmethod
    def _make_gap(stream: _RunStream, requested_event_id: str | None) -> StreamGap:
        return StreamGap(
            requested_event_id=requested_event_id,
            earliest_available_event_id=(
                stream.events[0].id if stream.events else ""
            ),
            latest_available_event_id=(
                stream.events[-1].id if stream.events else ""
            ),
        )

    def _append_delta(self, stream: _RunStream, kind: str, text: str) -> None:
        """追加增量并按上限裁剪头部（保尾部：与随后的实时增量天然衔接）。"""
        chunks = stream.chunks[kind]
        chunks.append(text)
        total = stream.chunk_chars[kind] + len(text)
        while total > self._text_max_chars and len(chunks) > 1:
            head = chunks.pop(0)
            total -= len(head)
            stream.dropped_chars[kind] += len(head)
        stream.chunk_chars[kind] = total

    def _snapshot(self, stream: _RunStream) -> StreamSnapshot:
        return StreamSnapshot(
            content="".join(stream.chunks[DELTA_TOKEN]),
            reasoning="".join(stream.chunks[DELTA_REASONING]),
            content_dropped_chars=stream.dropped_chars[DELTA_TOKEN],
            reasoning_dropped_chars=stream.dropped_chars[DELTA_REASONING],
        )

    async def _catch_up(
        self,
        stream: _RunStream,
        delivered: int,
        requested_event_id: str | None = None,
    ) -> tuple[StreamSnapshot, list[StreamItem], int]:
        """补齐"调用方已确认收到的游标之后"的全部内容（订阅开始与溢出重同步共用）。

        返回 `(快照, 待补发帧, 新游标)`：
          - 快照恒在（替换语义，覆盖正文/推理）；
          - 待补发帧 = 游标之后、仍在保留窗口内的结构性事件；游标被裁剪过则**只**给
            一帧 `StreamGap`（见下）；
          - 新游标 = 补发到哪了，供后续队列去重用。

        不变式：**当且仅当"有早于保留窗口的结构性事件没能交付"时给一帧 `StreamGap`。**
        对全新订阅者（`delivered = -1`，"一条都没确认过"）同样成立：只要窗口开头已被
        裁掉（`start_offset > 0`），也如实报 gap——否则调用方会以为手里那份历史是完整的。

        **游标越界时的取舍（保守）**：`delivered + 1 < start_offset` 说明客户端要的下
        一条已不在窗口里——缺的那条找不回来了。此时我们**不**顺手把仍在窗口内的尾部
        （`start_offset .. seq-1`）也补出去，只报 gap 后转实时。理由：结构性事件之间
        有因果引用（`tool_call` 挂在某条 `ai_message_start` 上），只补半截会让前端把
        半成品状态应用到界面上；调用方拿到 gap 后应改走"快照 + 回权威存储重取"的
        路径（P1-b 的 SSE 端点据此下发 `resync` 帧）。宁可不补，也不给错的状态。

        必须在 `stream.condition` 保护下读：日志/累积器/游标三者要同一时刻的视图。
        """
        async with stream.condition:
            snapshot = self._snapshot(stream)
            if delivered + 1 < stream.start_offset:
                # 游标落在已裁剪区间：结构性事件重放不完整，如实上报（并带上原游标）
                gap = self._make_gap(stream, requested_event_id)
                return snapshot, [gap], stream.start_offset - 1
            replay: list[StreamItem] = [
                stream.events[i - stream.start_offset]
                for i in range(max(delivered + 1, stream.start_offset), stream.seq)
            ]
            new_delivered = stream.seq - 1 if replay else delivered
            return snapshot, replay, new_delivered

    def _fanout(self, stream: _RunStream, item: StreamItem) -> None:
        """投递给全部订阅者。队列满 → 清空并放一个**重同步标记**。

        为什么不直接补一帧快照了事：快照只覆盖逐 token 的正文/推理，**不覆盖结构性
        事件**。若队列里正躺着一条 `file_generated` / `ai_message_start`，清队列会把
        它丢掉，而结构性事件是有语义的（少一条，前端就少一张产物卡片）。故标记交给
        消费者侧去 `_catch_up`：快照（正文）+ 从日志重放（结构性事件）＝ 两边都不丢。
        """
        for sub in list(stream.subscribers):
            try:
                sub.queue.put_nowait(item)
            except asyncio.QueueFull:
                while not sub.queue.empty():
                    try:
                        sub.queue.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover - 竞态兜底
                        break
                try:
                    sub.queue.put_nowait(RESYNC_MARKER)  # 清空后必有空位
                except asyncio.QueueFull:  # pragma: no cover
                    logger.warning("订阅者队列放入重同步标记失败，本轮订阅可能不完整")

    # -- StreamBridge API ------------------------------------------------------

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            entry = StreamEvent(id=self._next_id(stream), event=event, data=data)
            stream.events.append(entry)
            if len(stream.events) > self._maxsize:
                overflow = len(stream.events) - self._maxsize
                del stream.events[:overflow]
                stream.start_offset += overflow
            self._fanout(stream, entry)

    async def publish_delta(self, run_id: str, kind: str, text: str) -> None:
        if kind not in DELTA_KINDS:
            raise ValueError(f"未知的增量类型 {kind!r}（应为 {DELTA_KINDS}）")
        if not text:
            return
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            self._append_delta(stream, kind, text)
            self._fanout(stream, StreamDelta(kind=kind, text=text))

    async def publish_end(self, run_id: str) -> None:
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            stream.ended = True
            self._fanout(stream, END_SENTINEL)

    async def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamItem]:
        """订阅。首帧恒为快照，其后是重放 + 实时事件。

        ⚠ 调用方**必须显式关闭**这个异步生成器（`contextlib.aclosing` / `aclose()`）：
        `async for` 里 break/return 不会自动关（CPython 只在 aclose 或 GC 时跑
        finally），不关就会留下"幽灵订阅者"（一直往它的队列投递，白占内存）。
        """
        stream = self._get_or_create_stream(run_id)
        sub = _Subscriber(queue=asyncio.Queue(maxsize=self._maxsize))

        # 挂队列 + 定初始游标必须原子：否则两步之间的发布既不在重放窗口里、
        # 也没投进队列（静默丢失整段）。
        async with stream.condition:
            stream.subscribers.append(sub)
            seq = self._parse_event_seq(last_event_id) if last_event_id else None
            if last_event_id is not None and seq is None:
                logger.warning(
                    "last_event_id=%s 不是本桥的事件 id（{ts}-{seq}），退化为从头重放: run=%s",
                    last_event_id,
                    run_id,
                )
            # seq=None（首订 / 外来 id）→ 从头要：游标记 -1（"一条都没确认过"）。
            # 刻意**不**记成 start_offset-1，否则"窗口开头已被裁掉"这件事会被算作
            # "刚好衔接上"，全新订阅者就会静默拿到一份缺头的结构性历史（见 _catch_up）。
            delivered = seq if seq is not None else -1
            ended = stream.ended

        try:
            # 首帧恒为快照：worker 可能先于订阅者启动，那段 token 只在累积器里
            snapshot, replay, delivered = await self._catch_up(
                stream, delivered, last_event_id
            )
            yield snapshot
            for item in replay:
                yield item

            if ended:
                yield END_SENTINEL
                return

            while True:
                try:
                    item = await asyncio.wait_for(
                        sub.queue.get(), timeout=heartbeat_interval
                    )
                except TimeoutError:
                    yield HEARTBEAT_SENTINEL
                    continue

                if item is RESYNC_MARKER:
                    # 慢订阅者：队列被清过 ⇒ 重新对齐（快照替换正文 + 从日志补结构性事件）
                    snapshot, replay, delivered = await self._catch_up(stream, delivered)
                    yield snapshot
                    for entry in replay:
                        yield entry
                    continue

                # ⚠ END 必须先于"结构性事件"判定：END_SENTINEL 也是个 StreamEvent，
                # 放后面会被上面那条宽条件吃掉，变成"永远不结束、只吐心跳"。
                if item is END_SENTINEL:
                    yield END_SENTINEL
                    return

                if isinstance(item, StreamEvent):
                    # 队列与重放窗口可能重叠（挂队列到补齐之间发布的事件两条路都有）
                    seq = self._parse_event_seq(item.id)
                    if seq is not None:
                        if seq <= delivered:
                            continue
                        delivered = seq
                yield item
        finally:
            async with stream.condition:
                if sub in stream.subscribers:
                    stream.subscribers.remove(sub)

    async def stream_exists(self, run_id: str) -> bool:
        """该 run 在进程内还有事件态吗（区分"从没开始"与"已清理"）。"""
        return run_id in self._streams

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        self._streams.pop(run_id, None)

    async def close(self) -> None:
        self._streams.clear()
