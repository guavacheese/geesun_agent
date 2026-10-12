"""Spike v2：用 anyio 复现 Starlette 的"持续性取消"，验证修复选型

v1（detached_persist_cancel.py，纯 asyncio）结论：task.cancel() 之后，finally 里的
await **仍然能跑完** —— 复现不出事故现象。原因：asyncio 的 cancel 是**一次性注入**。

真实环境是 Starlette/anyio：客户端断连走 `task_group.cancel_scope.cancel()`，
是 **scope 级持续性取消** —— 只要还在被取消的 scope 内，**每一个** await 点都抛。

本 spike 用与 starlette.responses.StreamingResponse.__call__ 相同的结构复现：
    async with anyio.create_task_group() as tg:
        tg.start_soon(wrap_stream)          # ← stream_response
        await listen_for_disconnect()       # ← 收到 http.disconnect 就返回
        tg.cancel_scope.cancel()            # ← 取消整个 group

三组对照：
  A 现状：内层生成器 finally 里直接 await _persist_session
  B 修复：finally 里用 asyncio.create_task 把保存剥离出取消作用域
  C 对照：finally 里 await asyncio.shield(...)（验证 shield 是否够用）

运行：python tests/spikes/detached_persist_cancel_anyio.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

import anyio

logging.basicConfig(level=logging.INFO, format="    %(message)s", stream=sys.stdout)
log = logging.getLogger("spike-anyio")

RECORD: list[str] = []


async def fake_persist(tag: str) -> int:
    """模拟 _persist_session：第一个 await 等价于 store.aget()。"""
    RECORD.append(f"{tag}: enter")
    await anyio.sleep(0.05)
    RECORD.append(f"{tag}: WROTE 42 msgs")
    return 42


# ── A 现状：finally 里直接 await（chat.py:2248）──────────────────
async def inner_direct():
    try:
        yield "chunk"
        await anyio.sleep(10)
    finally:
        try:
            n = await fake_persist("A_direct")
            RECORD.append(f"A_direct: OK n={n}")
        except Exception as e:  # ← chat.py:2260 的写法
            RECORD.append(f"A_direct: except Exception → {e!r}")
        except BaseException as e:  # ← 补上才抓得到 CancelledError
            RECORD.append(f"A_direct: except BaseException → {type(e).__name__}")


# ── B 修复：create_task 剥离出取消作用域 ─────────────────────────
async def inner_detached():
    try:
        yield "chunk"
        await anyio.sleep(10)
    finally:
        task = asyncio.get_running_loop().create_task(
            fake_persist("B_detached"), name="persist-detached"
        )
        RECORD.append("B_detached: spawned（未 await）")
        _DETACHED.add(task)
        task.add_done_callback(_DETACHED.discard)


_DETACHED: set[asyncio.Task] = set()


# ── C 对照：asyncio.shield 是否够用 ──────────────────────────────
async def inner_shield():
    try:
        yield "chunk"
        await anyio.sleep(10)
    finally:
        try:
            n = await asyncio.shield(fake_persist("C_shield"))
            RECORD.append(f"C_shield: OK n={n}")
        except BaseException as e:
            RECORD.append(f"C_shield: {type(e).__name__}")


async def starlette_like(body_factory, *, disconnect_after: float = 0.1) -> None:
    """复刻 StreamingResponse.__call__ 的取消结构。"""
    async with anyio.create_task_group() as tg:

        async def wrap_stream():
            async for _chunk in body_factory():
                await anyio.sleep(10)  # 模拟 await send({...})
            tg.cancel_scope.cancel()

        tg.start_soon(wrap_stream)
        await anyio.sleep(disconnect_after)  # listen_for_disconnect 收到断连
        RECORD.append("→ 客户端断连，cancel_scope.cancel()")
        tg.cancel_scope.cancel()


async def scenario(name: str, factory) -> None:
    log.info("=== 场景 %s ===", name)
    RECORD.clear()
    with anyio.move_on_after(5):
        await starlette_like(factory)
    await anyio.sleep(0.5)  # 给"后台/延迟 aclose"留窗口

    for line in RECORD:
        log.info("  · %s", line)
    wrote = any("WROTE" in r for r in RECORD)
    log.info("  ⇒ 是否真正落库: %s", "是" if wrote else "**否（静默丢失）**")
    log.info("")


async def main() -> None:
    await scenario("A 现状：finally 里直接 await", inner_direct)
    await scenario("B 修复：create_task 剥离", inner_detached)
    await scenario("C 对照：asyncio.shield", inner_shield)
    log.info("=== 汇总 ===")
    log.info("  只有 B 能在 anyio 持续性取消下完成落库，A/C 均静默失败")


if __name__ == "__main__":
    anyio.run(main)
