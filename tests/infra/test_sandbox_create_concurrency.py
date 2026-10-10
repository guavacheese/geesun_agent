"""create_sandbox 并发创建串行化单测（纯 mock，不依赖真实沙箱）。

背景（2026-10-10）：
    chat.py 的 create_sandbox 调用改为 ``await asyncio.to_thread(...)`` 修「同步阻塞
    冻住事件循环」（会话 GY24428:a2719d7b，agent 连续被 swarm 杀 ≥7 次）。to_thread
    化之后，**同一 thread 的并发请求会真的并行跑进来**（改前同步调用把事件循环占死，
    反而不并发），于是原本只是"浪费一次创建"的竞态升级成**自己杀掉自己的沙箱**：
    两路各建一个实例 → 后写覆盖 ``_sandbox_cache[thread_id]`` → 先建那个失去强引用
    被 GC → langchain-cubesandbox 的 ``__del__`` 销毁沙箱（2026-08-14 实测形态）。

判据（spike-then-verify）：
    1. 同 thread_id N 路并发 → get_or_create **只被调用 1 次**，且 N 路拿到同一实例
    2. 不同 thread_id → 各自创建（锁按 thread 隔离，不能串成全局串行）
    3. 已缓存 → 不再创建（保持原有复用语义）

运行：
    docker run --rm -v D:/workspace/geesun_agent:/mnt -w /mnt \
      -e PYTHONPATH=/mnt/.testdeps --entrypoint /app/.venv/bin/python \
      172.16.220.74:8333/geesun_ai/geesun-agent:1.0.22 \
      -m pytest tests/infra/test_sandbox_create_concurrency.py -q
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.infra import sandbox as sb  # noqa: E402


class _FakeCubeSandbox:
    """假 CubeSandbox：记录 get_or_create 调用，返回可 execute 的最小对象。"""

    calls: list[dict] = []
    created: list["_FakeCubeSandbox"] = []

    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        self.sandbox_id = f"sb-{thread_id}"
        self._sandbox = object()  # 非 None：走 pip config / CA 注入分支
        self.executed: list[str] = []

    def execute(self, command: str, timeout: int | None = None):
        self.executed.append(command)
        return SimpleNamespace(output="", exit_code=0)

    @classmethod
    def get_or_create(cls, **kwargs):
        cls.calls.append(kwargs)
        # 放大并发窗口：没有创建锁时这里足以让 N 路全部穿透
        time.sleep(0.05)
        inst = cls(kwargs["thread_id"])
        cls.created.append(inst)
        return inst


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """隔离：清缓存 + 装假 SDK + 给出合法的 e2b_ key。"""
    _FakeCubeSandbox.calls = []
    _FakeCubeSandbox.created = []
    sb._sandbox_cache.clear()
    sb._per_thread_create_locks.clear()
    monkeypatch.setitem(
        sys.modules, "langchain_cubesandbox", SimpleNamespace(CubeSandbox=_FakeCubeSandbox)
    )
    monkeypatch.setattr(sb.settings, "cube_api_key", "e2b_test_key", raising=False)
    yield
    sb._sandbox_cache.clear()
    sb._per_thread_create_locks.clear()


def test_concurrent_same_thread_creates_once():
    """同 thread_id 8 路并发 → get_or_create 仅 1 次，返回同一实例。"""
    n = 8
    results: list[object] = [None] * n
    barrier = threading.Barrier(n)

    def worker(i: int):
        barrier.wait()  # 让 8 个线程尽量同时进入 create_sandbox
        results[i] = sb.create_sandbox("user:sess-1")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(_FakeCubeSandbox.calls) == 1, (
        f"同 thread 并发应只创建 1 次，实际 {len(_FakeCubeSandbox.calls)} 次"
    )
    assert all(r is not None for r in results)
    assert len({id(r) for r in results}) == 1, "并发各路必须拿到同一沙箱实例"


def test_different_threads_create_separately():
    """不同 thread_id → 各自创建（不能退化成全局串行/全局单例）。"""
    a = sb.create_sandbox("user:sess-a")
    b = sb.create_sandbox("user:sess-b")

    assert a is not b
    assert len(_FakeCubeSandbox.calls) == 2
    assert {c["thread_id"] for c in _FakeCubeSandbox.calls} == {"user:sess-a", "user:sess-b"}


def test_cached_thread_reuses_without_creating():
    """二次调用命中缓存 → 不再 get_or_create。"""
    first = sb.create_sandbox("user:sess-c")
    for _ in range(3):
        assert sb.create_sandbox("user:sess-c") is first
    assert len(_FakeCubeSandbox.calls) == 1


def test_without_create_lock_races(monkeypatch):
    """负向对照：拿掉创建锁后同一并发场景**必然**重复创建。

    存在的意义：证明上面那条「只创建 1 次」的断言**真的由锁保证**，而不是并发
    窗口太小碰巧没撞上（否则测试会在实现回退时依然全绿，等于空测）。
    """
    monkeypatch.setattr(sb, "_get_create_lock", lambda _tid: threading.Lock())

    n = 8
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        sb.create_sandbox("user:sess-race")

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(_FakeCubeSandbox.calls) > 1, (
        "无锁对照未复现竞态 —— 说明并发窗口不成立，主用例断言不可信"
    )


def test_invalid_key_short_circuits(monkeypatch):
    """key 非法 → 直接 None，不触碰 SDK（保持原有静默跳过语义）。"""
    monkeypatch.setattr(sb.settings, "cube_api_key", "", raising=False)
    assert sb.create_sandbox("user:sess-d") is None
    assert _FakeCubeSandbox.calls == []
