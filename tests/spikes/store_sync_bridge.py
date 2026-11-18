"""Spike: ReconnectingAsyncPostgresStore 补齐**同步契约**（2026-11-18）。

故障复现（生产 1.0.50，会话 GY35362:c6a0eebd，08:36）：
    模型 glob 没传 path → CompositeBackend 遍历全部路由（含 /workspace/memories、
    /workspace/agent-memory 两个 StoreBackend）→ deepagents 0.6.12 的 StoreBackend
    只有同步 ls/glob（内部 `store.search`），`BackendProtocol.als` 默认实现正是
    `asyncio.to_thread(self.ls)` → `AttributeError: 'ReconnectingAsyncPostgresStore'
    object has no attribute 'search'`；同步 `read` 则是 no attribute 'get'。
    两次 glob 被工具层兜住，紧接一次 ls 未捕获 → SSE 直接「任务执行失败」。

修复：包装器补 `get/put/search/batch` 四个同步方法，用
`asyncio.run_coroutine_threadsafe` 把协程桥回**主事件循环**（psycopg 连接池绑定
在那个循环上），并显式拒绝"在主循环线程上调用同步接口"（`.result()` 等自己 =
死锁）。

本 spike 用**只实现异步接口**的假 store（与 AsyncPostgresStore 同构）验证：
  1. 同步方法确实由包装器提供，且返回值/参数原样透传到异步实现；
  2. 从 worker 线程（deepagents `to_thread` 的形态）调用能拿到结果；
  3. 在主事件循环线程上调用**立即报错而不是挂死**；
  4. deepagents `StoreBackend` 的 ls / 同步 read 真的走通（接线契约）。

运行: python tests/spikes/store_sync_bridge.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve()
# 兼容两种落点：仓内 tests/spikes/（parents[2] = 仓根）与容器里的 /tmp/xxx.py
for _cand in (_ROOT.parents[2] if len(_ROOT.parents) > 2 else _ROOT.parent, Path("/app"), Path.cwd()):
    if (_cand / "src").is_dir():
        sys.path.insert(0, str(_cand))
        break

# Settings 必填字段兜底（import src.* 需要）
for _k, _v in (
    ("BASE_URL", "http://fake:8003/v1"),
    ("OPENAI_API_KEY", "spike-key"),
    ("MODEL_NAME", "Qwen3.6-35B-A3B"),
    ("AGENT_WORKSPACE", "./.spike-workspace"),
):
    os.environ.setdefault(_k, _v)

from src.infra.database import ReconnectingAsyncPostgresStore

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name} {detail}")


class _FakeAsyncStore:
    """与 AsyncPostgresStore 同构：**只有** async 方法，任何同步访问都不存在。

    这正是生产故障的形态（`no attribute 'search'/'get'`），所以只要本 spike 里
    同步路径能跑通，就证明缺口由包装器补上了，而不是被假实现意外掩盖。
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def aget(self, namespace, key):
        self.calls.append(("aget", tuple(namespace), key))
        # 与 AsyncPostgresStore 一致：返回 Item（有 .key/.value），不是裸 dict
        return SimpleNamespace(key=key, value={"content": f"content-of-{key}", "encoding": "utf-8"})

    async def aput(self, namespace, key, value):
        self.calls.append(("aput", tuple(namespace), key, value))
        return None

    async def asearch(self, namespace, **kwargs):
        self.calls.append(("asearch", tuple(namespace), kwargs))
        return [SimpleNamespace(key="/note.md", value={"content": "hello", "encoding": "utf-8"})]

    async def abatch(self, ops):
        self.calls.append(("abatch", tuple(ops)))
        return None


def _make_store(fake: _FakeAsyncStore) -> ReconnectingAsyncPostgresStore:
    """构造包装器并跳过建连（_store 预置 ⇒ _ensure 直接复用）。"""
    store = ReconnectingAsyncPostgresStore("postgresql://fake/db")
    store._store = fake  # type: ignore[assignment]
    store._loop = asyncio.get_running_loop()
    return store


async def main() -> None:
    fake = _FakeAsyncStore()
    store = _make_store(fake)

    # ── 1. 契约：同步方法确实挂在包装器上，内层假 store 一个都没有 ──
    print("\n[1] 同步契约存在性")
    check(
        "包装器提供 get/put/search/batch",
        all(hasattr(store, m) for m in ("get", "put", "search", "batch")),
    )
    check(
        "内层 store 只有异步接口（与生产 AsyncPostgresStore 同构）",
        not any(hasattr(fake, m) for m in ("get", "put", "search", "batch")),
    )

    # ── 2. worker 线程调用（deepagents asyncio.to_thread 的真实形态）──
    print("\n[2] worker 线程 → 桥回主循环")
    got = await asyncio.to_thread(store.search, ("__agent__",), limit=5)
    check(
        "search() 返回内层 asearch 的结果",
        isinstance(got, list) and got and got[0].key == "/note.md",
        f"got {got!r}",
    )
    check(
        "kwargs 原样透传（limit=5）",
        ("asearch", ("__agent__",), {"limit": 5}) in fake.calls,
        f"calls={fake.calls}",
    )
    got_get = await asyncio.to_thread(store.get, ("memories", "u1"), "/note.md")
    check(
        "get() 走通（返回 Item，与 AsyncPostgresStore 同构）",
        getattr(got_get, "key", None) == "/note.md"
        and (getattr(got_get, "value", {}) or {}).get("content") == "content-of-/note.md",
        f"got {got_get!r}",
    )
    check("put() 走通", await asyncio.to_thread(store.put, ("memories",), "/a.md", {"content": "x"}) is None)
    check("batch() 走通", await asyncio.to_thread(store.batch, [("op1",)]) is None)

    # ── 3. 主循环线程上调用 → 必须立刻报错，绝不能挂死 ──
    print("\n[3] 主循环线程调用 → 死锁守卫")
    try:
        store.search(("__agent__",), limit=1)
        raised, msg = False, ""
    except RuntimeError as e:
        raised, msg = True, str(e)
    check("主循环线程调用同步接口 → RuntimeError（不挂死）", raised, "未抛错，说明会自等死锁")
    check("报错信息指引改异步", "await asearch" in msg, f"msg={msg[:80]}")

    # ── 4. 接线契约：deepagents StoreBackend 的同步路径真的走通 ──
    print("\n[4] deepagents StoreBackend 接线")
    from deepagents.backends.store import StoreBackend

    be = StoreBackend(namespace=lambda _rt: ("__agent__",), store=store)
    ls_res = await asyncio.to_thread(be.ls, "/")
    entries = ls_res.entries or []
    check(
        "StoreBackend.ls（同步 → store.search）不再 AttributeError",
        ls_res.error is None and any(e.get("path") == "/note.md" for e in entries),
        f"error={ls_res.error} entries={entries!r}",
    )
    read_res = await asyncio.to_thread(be.read, "/note.md")
    check(
        "StoreBackend.read（同步 → store.get）不再 AttributeError",
        read_res.error is None
        and "content-of-/note.md" in ((read_res.file_data or {}).get("content") or ""),
        f"error={read_res.error} data={read_res.file_data!r}",
    )
    aread_res = await be.aread("/note.md")
    check(
        "StoreBackend.aread（原生异步）仍正常 —— 未被本次改动影响",
        aread_res.error is None
        and "content-of-/note.md" in ((aread_res.file_data or {}).get("content") or ""),
        f"error={aread_res.error}",
    )

    # ── 5. store 未初始化 / 已关闭的显式报错 ──
    print("\n[5] 边界")
    fresh = ReconnectingAsyncPostgresStore("postgresql://fake/db")
    try:
        await asyncio.to_thread(fresh.search, ("x",))
        ok = False
    except RuntimeError as e:
        ok = "尚未初始化" in str(e)
    check("未初始化时给明确错误（不是 NoneType.wait）", ok)
    closed = _make_store(_FakeAsyncStore())
    closed._closed = True
    try:
        await asyncio.to_thread(closed.get, ("x",), "k")
        ok = False
    except RuntimeError as e:
        ok = "已关闭" in str(e)
    check("aclose() 之后给明确错误", ok)


asyncio.run(main())

print(f"\n{'=' * 60}")
print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    sys.exit(1)
print("ALL OK — 同步契约补齐验证通过")
