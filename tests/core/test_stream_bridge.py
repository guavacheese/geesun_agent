"""StreamBridge 单测（P1-a，2026-10-12）。

判据取向：**断言桥的行为，不是复刻桥的实现**。全部用 `asyncio.run` 直接驱动，
不依赖任何插件（本仓 tests/core 亦无 pytest-asyncio 依赖）。

核心要证的几件事（对应 P1 方案 §5.2 的三个坑）：
  1. 逐 token 事件**不得**冲垮有界日志 —— deer-flow 的 256 条缓冲是按"一 step 一条"
     设计的，我们的 token 是逐字级的，照抄必丢正文。本桥把两类事件分流：
     结构性事件进有界日志（可游标重放），token/reasoning 进累积器（快照补齐）。
  2. `Last-Event-ID` 续订只送**游标之后**的事件（不重复、不漏）。
  3. 游标被裁剪过 → 给 `StreamGap`（保守：宁可让调用方走快照，也不假装重放完整）。
  4. 订阅者来去不影响生产侧 —— 这是整个 P1 的目的（刷新不再杀任务）。
"""

from __future__ import annotations

import asyncio

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

RUN = "run-1"
HB = 0.05


async def collect(
    bridge: MemoryStreamBridge,
    run_id: str = RUN,
    *,
    last_event_id: str | None = None,
    limit: int | None = None,
    timeout: float = 1.0,
) -> list:
    """收集订阅帧（收到 limit 帧或 END 即停）。"""
    items: list = []

    async def _drain() -> None:
        async for item in bridge.subscribe(
            run_id, last_event_id=last_event_id, heartbeat_interval=HB
        ):
            items.append(item)
            if item is END_SENTINEL:
                return
            if limit is not None and len(items) >= limit:
                return

    await asyncio.wait_for(_drain(), timeout)
    return items


def structs(items: list) -> list[StreamEvent]:
    """只看结构性事件（排除心跳与增量）。"""
    return [
        i for i in items if isinstance(i, StreamEvent) and i.data is not None
    ]


def deltas(items: list) -> list[StreamDelta]:
    return [i for i in items if isinstance(i, StreamDelta)]


# ─────────────────────────── 1. 基本发布 / 实时订阅 ───────────────────────────


def test_subscribe_first_frame_is_snapshot_then_live_events():
    """首帧恒为快照；其后是实时结构性事件，其 id 可直接当游标。"""

    async def main():
        bridge = MemoryStreamBridge()
        await bridge.publish(RUN, "tool_call", {"tool": "a"})  # worker 先跑了一段
        got: list = []

        async def drain():
            async for item in bridge.subscribe(RUN, heartbeat_interval=HB):
                got.append(item)
                if len(structs(got)) >= 2:
                    return

        task = asyncio.create_task(drain())
        await asyncio.sleep(0.01)  # 让订阅者先挂上
        await bridge.publish(RUN, "tool_result", {"ok": True})
        await asyncio.wait_for(task, 1.0)
        return got

    got = asyncio.run(main())
    assert isinstance(got[0], StreamSnapshot)
    evs = structs(got)
    assert [e.event for e in evs] == ["tool_call", "tool_result"]
    # id 形如 {ts}-{seq}：seq 是该 run 内结构性事件的绝对序号
    assert evs[0].id.endswith("-0") and evs[1].id.endswith("-1")


# ─────────────────────────── 2. 逐 token 不冲垮有界日志（核心） ───────────────────────────


def test_token_flood_does_not_evict_structural_log():
    """★ 几百条逐 token 事件不得挤掉结构性事件（deer-flow 的 256 条缓冲照抄必丢）。"""

    async def main():
        bridge = MemoryStreamBridge(queue_maxsize=4)
        await bridge.publish(RUN, "tool_call", {"tool": "ocr"})
        for i in range(300):  # 10 倍于 queue_maxsize
            await bridge.publish_delta(RUN, DELTA_TOKEN, f"字{i} ")
        items = await collect(bridge, limit=1)
        raw = bridge._streams[RUN]
        return items, raw

    items, raw = asyncio.run(main())
    snap = items[0]
    assert isinstance(snap, StreamSnapshot)
    assert snap.content == "".join(f"字{i} " for i in range(300))
    assert snap.content_dropped_chars == 0
    # 结构性日志一条没丢
    assert [e.data for e in raw.events] == [{"tool": "ocr"}]


def test_live_subscriber_receives_each_token_delta():
    """订阅期逐 token 走实时通道（不是靠重放日志）。"""

    async def main():
        bridge = MemoryStreamBridge()
        got: list = []

        async def drain():
            async for item in bridge.subscribe(RUN, heartbeat_interval=HB):
                got.append(item)
                if len([x for x in got if isinstance(x, StreamDelta)]) >= 3:
                    return

        task = asyncio.create_task(drain())
        await asyncio.sleep(0.01)
        for t in ("你", "好", "呀"):
            await bridge.publish_delta(RUN, DELTA_TOKEN, t)
        await asyncio.wait_for(task, 1.0)
        return got

    got = asyncio.run(main())
    assert [d.text for d in deltas(got)] == ["你", "好", "呀"]
    assert all(d.kind == DELTA_TOKEN for d in deltas(got))


def test_reasoning_and_token_accumulate_separately():
    """快照里正文与推理分开累积（前端两个渲染区各取各的）。"""

    async def main():
        bridge = MemoryStreamBridge()
        await bridge.publish_delta(RUN, DELTA_REASONING, "先想想…")
        await bridge.publish_delta(RUN, DELTA_TOKEN, "答案是 42")
        items = await collect(bridge, limit=1)
        return items[0]

    snap = asyncio.run(main())
    assert (snap.reasoning, snap.content) == ("先想想…", "答案是 42")


def test_delta_accumulator_caps_and_reports_dropped_head():
    """累积器超上限时保尾部，并如实报告被丢弃的头部字符数（不静默）。"""

    async def main():
        bridge = MemoryStreamBridge(text_max_chars=10)
        for i in range(5):
            await bridge.publish_delta(RUN, DELTA_TOKEN, "0123456789")  # 每次恰好 10 字
        items = await collect(bridge, limit=1)
        return items[0]

    snap = asyncio.run(main())
    assert snap.content == "0" * 10 or len(snap.content) <= 20
    assert snap.content_dropped_chars >= 30  # 至少丢了 3 块


def test_publish_delta_rejects_unknown_kind_and_ignores_empty():
    async def main():
        bridge = MemoryStreamBridge()
        with pytest.raises(ValueError, match="未知的增量类型"):
            await bridge.publish_delta(RUN, "usage", "x")
        await bridge.publish_delta(RUN, DELTA_TOKEN, "")  # 空串忽略
        return bridge

    bridge = asyncio.run(main())
    # 两条路径都不该凭空建流（空增量连流都不创建）
    assert bridge._streams.get(RUN) is None


# ─────────────────────────── 3. 游标续订 ───────────────────────────


def test_resume_with_last_event_id_skips_delivered_events():
    """★ 带游标重连：只送游标之后的事件（不重复、不漏）。"""

    async def main():
        bridge = MemoryStreamBridge()
        for i in range(3):
            await bridge.publish(RUN, "step", {"i": i})

        first = await collect(bridge, limit=4)  # 快照 + 3 条
        evs = [i for i in first if isinstance(i, StreamEvent) and i.data is not None]
        cursor = evs[1].id  # 已确认收到第 0、1 条 → 从第 2 条续

        resumed = await collect(bridge, last_event_id=cursor, limit=2)
        return evs, resumed

    evs, resumed = asyncio.run(main())
    assert len(evs) == 3
    assert isinstance(resumed[0], StreamSnapshot)  # 首帧恒为快照（替换语义）
    assert [e.data for e in resumed[1:] if isinstance(e, StreamEvent)] == [{"i": 2}]


def test_cursor_beyond_tail_replays_nothing():
    """游标指向最后一条（客户端已收全）→ 除快照外不再重放。"""

    async def main():
        bridge = MemoryStreamBridge()
        await bridge.publish(RUN, "step", {"i": 0})
        await bridge.publish(RUN, "step", {"i": 1})
        first = await collect(bridge, limit=3)
        last_id = [i for i in first if isinstance(i, StreamEvent) and i.data is not None][-1].id
        return await collect(bridge, last_event_id=last_id, limit=1)

    items = asyncio.run(main())
    assert len(items) == 1 and isinstance(items[0], StreamSnapshot)


def test_trimmed_cursor_yields_gap_not_partial_replay():
    """★ 游标已被裁剪 → 报 StreamGap，且**不**顺手补发窗口内残留的尾部。

    这里把"保守取舍"钉成契约：结构性事件之间有因果引用（`tool_call` 挂在某条
    `ai_message_start` 上），补半截会让前端把半成品状态应用到界面上。调用方拿到
    gap 后应改走"快照 + 回权威存储重取"。故断言 gap 之后**立刻**转实时（心跳），
    而不是接着吐出日志里还剩的 3、4 两条。
    """

    async def main():
        bridge = MemoryStreamBridge(queue_maxsize=2)
        for i in range(5):
            await bridge.publish(RUN, "step", {"i": i})
        # 日志只留最后 2 条（绝对序号 3、4）；用序号 1 的 id 重连
        items = await collect(bridge, last_event_id="1-1", limit=3, timeout=0.5)
        return items

    items = asyncio.run(main())
    assert isinstance(items[0], StreamSnapshot)
    assert isinstance(items[1], StreamGap)
    gap = items[1]
    assert gap.requested_event_id == "1-1"  # 原样带回，便于日志定位
    assert gap.earliest_available_event_id.endswith("-3")
    assert gap.latest_available_event_id.endswith("-4")
    # 关键：gap 之后没有部分重放（直接转实时心跳）
    assert items[2] is HEARTBEAT_SENTINEL
    assert not [i for i in items if isinstance(i, StreamEvent) and i.data is not None]


def test_fresh_subscriber_of_truncated_run_is_told_the_head_is_gone():
    """★ 结构性历史被裁剪时，**全新订阅者**也必须收到 gap。

    反例场景：run 有 600 条结构性事件、窗口只留 512。若把"无游标"当成"游标恰好在
    窗口起点"，这个订阅者就会安静地拿走 512 条、以为历史是完整的。判据是"有事件没能
    交付就必须报"，与有没有游标无关。
    """

    async def main():
        bridge = MemoryStreamBridge(queue_maxsize=2)
        for i in range(5):
            await bridge.publish(RUN, "step", {"i": i})
        return await collect(bridge, limit=2, timeout=0.5)

    items = asyncio.run(main())
    assert isinstance(items[0], StreamSnapshot)
    assert isinstance(items[1], StreamGap)
    # 没带游标 → requested 为 None（"从头要，但开头已经没了"）
    assert items[1].requested_event_id is None
    assert items[1].earliest_available_event_id.endswith("-3")


def test_fresh_subscriber_of_untruncated_run_gets_no_gap():
    """反向：窗口没裁过就**不得**报 gap（否则前端每次都白跑一次全量重取）。"""

    async def main():
        bridge = MemoryStreamBridge(queue_maxsize=4)
        for i in range(3):
            await bridge.publish(RUN, "step", {"i": i})
        return await collect(bridge, limit=4)

    items = asyncio.run(main())
    assert not [i for i in items if isinstance(i, StreamGap)]
    assert [e.data for e in items if isinstance(e, StreamEvent) and e.data is not None] == [
        {"i": 0},
        {"i": 1},
        {"i": 2},
    ]


def test_unknown_cursor_falls_back_to_full_replay():
    """外来 id（非本桥格式）→ 退回从头重放，而不是报 gap 或崩。"""

    async def main():
        bridge = MemoryStreamBridge()
        await bridge.publish(RUN, "step", {"i": 0})
        await bridge.publish(RUN, "step", {"i": 1})
        return await collect(bridge, last_event_id="not-an-id", limit=3)

    items = asyncio.run(main())
    assert [e.data for e in items if isinstance(e, StreamEvent) and e.data is not None] == [
        {"i": 0},
        {"i": 1},
    ]


# ─────────────────────────── 4. 结束 / 心跳 ───────────────────────────


def test_publish_end_delivers_end_sentinel():
    async def main():
        bridge = MemoryStreamBridge()
        await bridge.publish(RUN, "step", {"i": 0})
        await bridge.publish_end(RUN)
        return await collect(bridge, limit=3)

    items = asyncio.run(main())
    assert items[-1] is END_SENTINEL
    assert isinstance(items[0], StreamSnapshot)


def test_subscribe_after_end_terminates_immediately():
    """生产已结束才来订阅（迟到者）→ 立刻拿到快照 + END，不空转。"""

    async def main():
        bridge = MemoryStreamBridge()
        await bridge.publish_delta(RUN, DELTA_TOKEN, "已完成的内容")
        await bridge.publish_end(RUN)
        return await collect(bridge, limit=3)

    items = asyncio.run(main())
    assert isinstance(items[0], StreamSnapshot) and items[0].content == "已完成的内容"
    assert items[-1] is END_SENTINEL


def test_live_subscriber_terminates_when_end_arrives_through_queue():
    """★ END **经队列**到达时，订阅生成器必须自己结束。

    这是与上面那条不同的路径：上面的订阅发生在 `publish_end` 之后（`ended=True` 短路），
    这条是订阅者已在线、END 从队列里来。`END_SENTINEL` 本身也是个 `StreamEvent`，若
    判定顺序写反（先判结构性事件）它就永远匹配不到终止分支 —— 症状是生产早结束了，
    而 SSE 端点还在按心跳周期空转，连接永不关闭。
    """

    async def main():
        bridge = MemoryStreamBridge()
        gen = bridge.subscribe(RUN, heartbeat_interval=HB)
        first = await gen.__anext__()  # 首帧快照
        await bridge.publish(RUN, "step", {"i": 0})
        await bridge.publish_end(RUN)

        seen = []
        while True:
            item = await asyncio.wait_for(gen.__anext__(), 1.0)
            seen.append(item)
            if item is END_SENTINEL:
                break

        # 终点之后再取一次：必须是 StopAsyncIteration，不能是心跳
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), 1.0)
        await gen.aclose()
        return first, seen

    first, seen = asyncio.run(main())
    assert isinstance(first, StreamSnapshot)
    assert seen[-1] is END_SENTINEL
    assert [e.data for e in seen if isinstance(e, StreamEvent) and e.data is not None] == [
        {"i": 0}
    ]
    assert HEARTBEAT_SENTINEL not in seen


def test_idle_subscriber_gets_heartbeat():
    """空转期间以心跳哨兵保活（对应 SSE 的 `: ping`，防代理掐连接）。"""

    async def main():
        bridge = MemoryStreamBridge()
        return await collect(bridge, limit=2, timeout=1.0)

    items = asyncio.run(main())
    assert isinstance(items[0], StreamSnapshot)
    assert items[1] is HEARTBEAT_SENTINEL


# ─────────────────────────── 5. 订阅者来去不影响生产（P1 的目的） ───────────────────────────


def test_subscriber_leaving_does_not_affect_producer_or_others():
    """★ 一个订阅者离场（= 用户刷新页面），生产侧与另一个订阅者都不受影响。

    离场语义：`async for` 里 break/return **不会**自动关闭异步生成器（CPython 只在
    显式 `aclose()` 或 GC 时跑 finally）——所以调用方必须显式关闭（P1-b 的 SSE 端点
    用 `contextlib.aclosing`）。这里就把这条契约断言出来：不关闭时订阅者仍挂着
    （幽灵订阅者），关闭后立刻注销。

    另注意 B 是**全新订阅者**（无 `Last-Event-ID`）→ 语义上它什么都没见过，故从
    窗口起点全量重放（含 A 已见过的 `{i:0}`）再接实时。这正是"刷新后首连"该有的
    行为：客户端不该看到半截历史。
    """

    async def main():
        bridge = MemoryStreamBridge()
        await bridge.publish(RUN, "step", {"i": 0})

        # A：收 1 帧就走（模拟刷新）——不复用 collect()，要自己控 aclose
        gen_a = bridge.subscribe(RUN, heartbeat_interval=HB)
        first_a = await gen_a.__anext__()
        live = bridge._streams[RUN]

        leaked = len(live.subscribers)  # aclose 之前仍挂着
        await gen_a.aclose()
        after_close = len(live.subscribers)

        # B：正常订阅，生产继续
        got: list = []

        async def drain():
            async for item in bridge.subscribe(RUN, heartbeat_interval=HB):
                got.append(item)
                if len(structs(got)) >= 3:
                    return

        task = asyncio.create_task(drain())
        await asyncio.sleep(0.01)
        await bridge.publish(RUN, "step", {"i": 1})
        await bridge.publish(RUN, "step", {"i": 2})
        await asyncio.wait_for(task, 1.0)
        # 生产侧序号照常推进，A 的离场没在流上留下任何缺口
        return first_a, leaked, after_close, got, live.seq

    first_a, leaked, after_close, got, seq = asyncio.run(main())
    assert isinstance(first_a, StreamSnapshot)
    assert leaked == 1, "未 aclose 的生成器仍持有订阅（幽灵订阅者）"
    assert after_close == 0, "aclose 后必须立即注销"
    assert [e.data for e in structs(got)] == [{"i": 0}, {"i": 1}, {"i": 2}]
    assert seq == 3, "生产侧序号必须连续推进（A 来去无影响）"


def test_slow_subscriber_recovers_via_replacement_snapshot():
    """★ 慢订阅者队列塞满 → 不静默丢 token，而是补一帧**替换语义**的快照。

    判据用"可重建性"而不是"某一帧的内容"：从**最后一帧快照**起，把其后的增量接上，
    必须恰好等于全文——这正是客户端 reducer 的合并语义（快照=替换、增量=追加），
    溢出路径下也不允许丢字。
    """

    async def main():
        bridge = MemoryStreamBridge(queue_maxsize=4)
        got: list = []

        async def drain():
            async for item in bridge.subscribe(RUN, heartbeat_interval=HB):
                got.append(item)
                if isinstance(item, StreamEvent) and item.event == "done":
                    return

        task = asyncio.create_task(drain())
        await asyncio.sleep(0.01)
        for _ in range(60):  # 远超队列容量，且订阅者消费得很慢
            await bridge.publish_delta(RUN, DELTA_TOKEN, "x")
        await bridge.publish(RUN, "done", {})
        await asyncio.wait_for(task, 1.0)
        return got

    got = asyncio.run(main())
    snaps = [i for i in got if isinstance(i, StreamSnapshot)]
    assert len(snaps) >= 2, "溢出时应至少补发一帧快照"
    last = max(idx for idx, it in enumerate(got) if isinstance(it, StreamSnapshot))
    rebuilt = got[last].content + "".join(
        it.text for it in got[last + 1 :] if isinstance(it, StreamDelta)
    )
    assert rebuilt == "x" * 60, f"自最后一帧快照重建应等于全文，实得 {len(rebuilt)} 字"


def test_no_subscribers_is_fine():
    """完全没有订阅者时发布不得报错（run 可以在没人看的时候跑）。"""

    async def main():
        bridge = MemoryStreamBridge()
        for i in range(10):
            await bridge.publish(RUN, "step", {"i": i})
            await bridge.publish_delta(RUN, DELTA_TOKEN, "t")
        await bridge.publish_end(RUN)
        return await collect(bridge, limit=1)

    items = asyncio.run(main())
    assert items[0].content == "t" * 10


# ─────────────────────────── 6. 生命周期 ───────────────────────────


def test_cleanup_removes_stream_and_exists_reflects_it():
    async def main():
        bridge = MemoryStreamBridge()
        await bridge.publish(RUN, "step", {})
        before = await bridge.stream_exists(RUN)
        await bridge.cleanup(RUN)
        after = await bridge.stream_exists(RUN)
        await bridge.publish(RUN, "step", {})
        recreated = await bridge.stream_exists(RUN)
        # cleanup 后重新发布 = 全新的流（序号从 0 起）
        raw = bridge._streams[RUN]
        return before, after, recreated, raw

    before, after, recreated, raw = asyncio.run(main())
    assert (before, after, recreated) == (True, False, True)
    assert len(raw.events) == 1 and raw.events[0].id.endswith("-0")


def test_cleanup_delay_gives_late_subscriber_a_chance():
    async def main():
        bridge = MemoryStreamBridge()
        await bridge.publish(RUN, "step", {"i": 0})
        task = asyncio.create_task(bridge.cleanup(RUN, delay=0.05))
        items = await collect(bridge, limit=2, timeout=1.0)
        await asyncio.wait_for(task, 1.0)
        gone = not await bridge.stream_exists(RUN)
        return items, gone

    items, gone = asyncio.run(main())
    assert [e.data for e in items if isinstance(e, StreamEvent) and e.data is not None] == [
        {"i": 0}
    ]
    assert gone is True


def test_invalid_constructor_args_rejected():
    with pytest.raises(ValueError):
        MemoryStreamBridge(queue_maxsize=0)
    with pytest.raises(ValueError):
        MemoryStreamBridge(text_max_chars=0)


def test_close_clears_everything():
    async def main():
        bridge = MemoryStreamBridge()
        await bridge.publish(RUN, "step", {})
        await bridge.close()
        return await bridge.stream_exists(RUN)

    assert asyncio.run(main()) is False
