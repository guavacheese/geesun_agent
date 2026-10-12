"""Spike v3（真实复现）：Starlette + uvicorn 下，SSE 断连时内层生成器 finally 里的 await 能否完成。

为什么不能用纯 asyncio / 纯 anyio 模拟：
  v1（asyncio task.cancel）与 v2（anyio scope.cancel）里，finally 中的 await **都成功了**，
  复现不出事故现象。真实链路上还有 Starlette StreamingResponse 的 task_group 结构
  与 uvicorn 的连接关闭路径，必须端到端跑。

四组对照（同一份 app，用 ?mode= 切换 finally 里的写法）：
  direct           现状   ：finally 里直接 await        （对应 chat.py:2248 修复前）
  detached         候选修复：create_task 剥离出取消作用域
  detached_release 最终实现：剥离 + 把 release 一并移交 （对应 _persist_session_and_release）
  shield           对照   ：await asyncio.shield(...)

判定：
  ① 服务端是否出现 "WROTE 42 msgs"（= 真正落库）
  ② detached_release 场景下 RELEASE 必须**晚于** WROTE（顺序不变量）

运行：python tests/spikes/sse_disconnect_repro.py
"""

from __future__ import annotations

import asyncio
import socket
import struct
import threading
import time

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

PORT = 18099
T0 = time.time()
_LOCK = threading.Lock()
ORDER: dict[str, float] = {}


def log(msg: str) -> None:
    with _LOCK:
        print(f"  [{time.time() - T0:6.3f}s] {msg}", flush=True)


BG: set[asyncio.Task] = set()
DETACHED: dict[str, asyncio.Task | None] = {"task": None}


async def fake_persist(tag: str) -> int:
    """模拟 _persist_session 的第一个 await（store.aget → DB 往返，~百毫秒）。"""
    log(f"{tag}: enter  ← 第一个 await 之前")
    await anyio.sleep(0.15)
    log(f"{tag}: WROTE 42 msgs  ← 真正落库")
    ORDER[f"{tag}:write"] = time.time() - T0
    return 42


def fake_release(tag: str) -> None:
    """模拟 turn_registry.release（同步、不打日志的那种）。"""
    log(f"{tag}: RELEASE（在跑轮次台账释放）")
    ORDER[f"{tag}:release"] = time.time() - T0


async def persist_and_release() -> int:
    """对应 _persist_session_and_release：写完台账才 release。"""
    try:
        return await fake_persist("final")
    finally:
        fake_release("final")


async def inner(mode: str):
    """对应 chat.py 的 _event_stream_inner（内层，含断连 finally）。"""
    try:
        i = 0
        while True:
            yield f"data: chunk{i}\n\n"
            i += 1
            await anyio.sleep(0.05)
    finally:
        log(f"[{mode}] inner finally ENTER")
        try:
            if mode == "direct":
                n = await fake_persist("direct")
            elif mode == "detached":
                t = asyncio.get_running_loop().create_task(fake_persist("detached"))
                BG.add(t)
                t.add_done_callback(BG.discard)
                n = 0
                log("[detached] spawned（脱离取消作用域）")
            elif mode == "detached_release":
                DETACHED["task"] = asyncio.get_running_loop().create_task(
                    persist_and_release(), name="persist-interrupted"
                )
                n = 0
                log("[detached_release] spawned（保存+release 一起移交）")
            elif mode == "shield":
                n = await asyncio.shield(fake_persist("shield"))
            else:
                raise ValueError(mode)
            log(f"[{mode}] inner finally DONE (n={n})")
        except Exception as e:
            log(f"[{mode}] except Exception → {e!r}")
        except BaseException as e:
            log(f"[{mode}] except BaseException → {type(e).__name__}")


async def outer(mode: str):
    """对应 chat.py 的 event_stream（外层包装，release 原位置）。"""
    try:
        async for c in inner(mode):
            yield c
    finally:
        if DETACHED["task"] is None:
            fake_release(mode)
        else:
            log(f"[{mode}] outer 跳过 release（已移交后台任务，等它写完台账）")


async def sse(request):
    mode = request.query_params.get("mode", "direct")
    return StreamingResponse(
        outer(mode),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app = Starlette(routes=[Route("/sse", sse)])


def run_client(mode: str) -> None:
    """连上、收几个 chunk、然后 RST 硬断开（模拟浏览器刷新/关页）。"""
    log(f"--- 客户端连接 mode={mode} ---")
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    s.sendall(
        f"GET /sse?mode={mode} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Accept: text/event-stream\r\n\r\n".encode()
    )
    time.sleep(0.4)
    got = s.recv(300)
    log(f"客户端已收 {len(got)} 字节 → RST 硬断开")
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()


def main() -> None:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
    )
    threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True).start()

    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    log("uvicorn 已启动")

    for mode in ("direct", "detached", "detached_release", "shield"):
        DETACHED["task"] = None
        ORDER.clear()
        run_client(mode)
        time.sleep(1.2)  # 给 finally + 后台任务留窗口

        wrote = any(k.endswith(":write") for k in ORDER)
        log(f"[{mode}] 判定 → 是否落库: {'是' if wrote else '**否（静默丢失）**'}")
        if mode == "detached_release":
            w = ORDER.get("final:write")
            r = ORDER.get("final:release")
            ok = w is not None and r is not None and r >= w
            log(f"[{mode}] 判定 → RELEASE 晚于写入: "
                f"{'是（顺序不变量成立）' if ok else '**否（不变量被破坏）**'}"
                f"  [write={w}, release={r}]")
        log("")

    server.should_exit = True
    time.sleep(0.3)
    log("=== 结束 ===")


if __name__ == "__main__":
    main()
