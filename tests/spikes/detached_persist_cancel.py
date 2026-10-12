"""Spike：验证「生成器 finally 里的 await 在 task 被取消后必然失败」

用途：为 chat.py 断连强制保存（_persist_session）的修复选型提供实测依据。
背景：2026-10-10 会话 4863afff 事故 —— 断连后 store 里 0 行，服务端既没有
      "断连强制保存完成" 也没有 "断连强制保存失败" 日志，说明整个保存路径静默蒸发。

待验证的两个假设：
  H1：task 被 cancel 后，生成器 finally 里的 await 会在**下一个 await 点**
      立即抛 CancelledError，保存函数体基本一行都跑不到。
  H2：CancelledError 是 BaseException 子类（Py3.8+），所以 `except Exception`
      抓不到它 → 这就是"只打一条 warning 就过去"的直接原因。
  H3：用 asyncio.create_task 把保存协程剥离出去后，即使原 task 已被取消，
      它仍能跑完（不在同一个取消作用域内）。

运行：python tests/spikes/detached_persist_cancel.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="    %(message)s", stream=sys.stdout)
log = logging.getLogger("spike")

RECORD: list[str] = []


async def fake_persist(tag: str) -> int:
    """模拟 _persist_session：第一个 await 就相当于 store.aget。"""
    RECORD.append(f"{tag}: enter")
    await asyncio.sleep(0.05)  # ← 相当于 await store.aget(...)
    RECORD.append(f"{tag}: wrote 42 msgs")
    return 42


async def gen_direct_await():
    """现状：finally 里直接 await（chat.py:2248）。"""
    try:
        yield "chunk-1"
        await asyncio.sleep(10)  # 长流
        yield "chunk-2"
    finally:
        try:
            n = await fake_persist("A_direct")
            RECORD.append(f"A_direct: OK n={n}")
        except Exception as e:  # ← chat.py:2260 就是这种写法
            RECORD.append(f"A_direct: except Exception caught {e!r}")
        except BaseException as e:
            RECORD.append(f"A_direct: except BaseException caught {type(e).__name__}")


async def gen_detached():
    """修复：create_task 剥离出当前取消作用域。"""
    task = None
    try:
        yield "chunk-1"
        await asyncio.sleep(10)
        yield "chunk-2"
    finally:
        task = asyncio.create_task(fake_persist("B_detached"), name="persist-B")
        RECORD.append("B_detached: spawned, not awaited")
    _ = task


async def consumer(gen) -> None:
    """模拟 SSE：消费生成器，每个 chunk 后挂住。"""
    async for _ in gen:
        await asyncio.sleep(10)


async def scenario(name: str, factory) -> None:
    log.info("=== 场景 %s ===", name)
    RECORD.clear()
    task = asyncio.create_task(consumer(factory()), name=f"sse-{name}")
    await asyncio.sleep(0.1)  # 让生成器跑到 await 点
    task.cancel()  # ← 等价于客户端断连
    try:
        await task
    except asyncio.CancelledError:
        log.info("  SSE task 已取消（等价客户端断连）")
    await asyncio.sleep(0.4)  # 给"后台"任务留出执行窗口

    for line in RECORD:
        log.info("  · %s", line)
    wrote = any("wrote" in r for r in RECORD)
    log.info("  ⇒ 是否真正写入: %s", "是" if wrote else "**否**")


async def main() -> None:
    await scenario("A 现状：finally 里直接 await", gen_direct_await)
    await scenario("B 修复：create_task 剥离", gen_detached)

    log.info("=== 结论 ===")
    log.info("  H2 CancelledError 是 BaseException 子类: %s",
             issubclass(asyncio.CancelledError, BaseException))
    log.info("  H2 CancelledError 是 Exception  子类: %s",
             issubclass(asyncio.CancelledError, Exception))


if __name__ == "__main__":
    asyncio.run(main())
