# -*- coding: utf-8 -*-
"""run 看门狗代行收尾单测（2026-10-19 方案 4）。

锁三件事：
  1. 挂死 run（活跃 + 心跳停摆被升级）⇒ _watchdog_finalize 取消 worker task，
     宽限后仍活跃则自行 release + 落 failed + publish_end（前端拿到 [DONE] 解封）；
  2. worker 已自行收尾 / 已被回收 ⇒ 看门狗让位（不重复落态、不发第二个 [DONE]）；
  3. 落态失败不阻断 publish_end（与 _finish_run 的同一条顺序语义）。
"""

import asyncio

import pytest

from src.api.endpoints import chat as chat_mod
from src.core import run_registry as rr
from src.core import turn_registry
from src.core.config import settings

USER, SESSION, THREAD = "u-wd", "s-wd", "u-wd:s-wd"


@pytest.fixture(autouse=True)
def _clean():
    rr.reset_for_tests()
    turn_registry.release(THREAD)
    chat_mod._WATCHDOG_ESCALATED.clear()
    yield
    rr.reset_for_tests()
    turn_registry.release(THREAD)
    chat_mod._WATCHDOG_ESCALATED.clear()


class FakeStore:
    def __init__(self, *, fail_write: bool = False):
        self.data: dict[tuple, object] = {}
        self.fail_write = fail_write

    async def aput(self, namespace, key, value):
        if self.fail_write:
            raise RuntimeError("store down")
        self.data[(tuple(namespace), key)] = value

    async def aget(self, namespace, key):
        return self.data.get((tuple(namespace), key))


class FakeBridge:
    def __init__(self):
        self.ended: list[str] = []

    async def publish_end(self, run_id):
        self.ended.append(run_id)

    async def cleanup(self, run_id):
        pass


class FakeTask:
    """worker task 的最小替身：只被看门狗 cancel()。"""

    def __init__(self):
        self.cancel_called = False

    def done(self):
        return False

    def cancel(self):
        self.cancel_called = True


def _make_active_run():
    rec = rr.create(user_id=USER, session_id=SESSION, thread_id=THREAD)
    asyncio.run(rr.mark(FakeStore(), rec.run_id, rr.STATUS_RUNNING))
    turn_registry.acquire(THREAD)
    return rec


def test_finalize_takes_over_stuck_run():
    """活跃 run ⇒ 取消 task + 宽限后仍活跃 ⇒ 代行收尾三元组。"""
    rec = _make_active_run()
    task = FakeTask()
    rr.attach_task(rec.run_id, task)
    store, bridge = FakeStore(), FakeBridge()
    old = settings.run_watchdog_grace_sec
    settings.run_watchdog_grace_sec = 0  # 测试不拖宽限
    try:
        asyncio.run(chat_mod._watchdog_finalize(rec.run_id, store, bridge))
    finally:
        settings.run_watchdog_grace_sec = old

    assert task.cancel_called, "看门狗必须先取消 worker task（循环健康时它才能走取消收尾）"
    assert rec.status == rr.STATUS_FAILED
    assert "watchdog" in (rec.error or "")
    row = store.data[(rr.namespace(USER), SESSION)]
    assert row["status"] == rr.STATUS_FAILED
    assert bridge.ended == [rec.run_id], "必须 publish_end，前端才拿得到 [DONE] 解封"
    assert turn_registry.is_active(THREAD) is False, "release 必须执行，否则会话被台账锁死"
    assert rec.run_id not in chat_mod._WATCHDOG_ESCALATED, "处置后升级标记必须清除（自愈）"


def test_finalize_yields_when_worker_already_finalized():
    """worker 已自行收尾（终态）⇒ 看门狗让位：不重复 mark、不发第二个 [DONE]。"""
    rec = _make_active_run()
    asyncio.run(rr.mark(FakeStore(), rec.run_id, rr.STATUS_COMPLETED))
    task = FakeTask()
    rr.attach_task(rec.run_id, task)
    store, bridge = FakeStore(), FakeBridge()
    asyncio.run(chat_mod._watchdog_finalize(rec.run_id, store, bridge))

    assert not task.cancel_called, "终态 run 不得再取消 task"
    assert rec.status == rr.STATUS_COMPLETED, "不得把已完成的 run 改判 failed"
    assert bridge.ended == [], "不得重复 publish_end"
    assert store.data == {}, "让位时不应写任何状态"


def test_finalize_survives_dropped_run():
    """run 已被回收（drop）⇒ 不崩、不写、不发。"""
    rec = _make_active_run()
    rr.drop(rec.run_id)
    store, bridge = FakeStore(), FakeBridge()
    asyncio.run(chat_mod._watchdog_finalize(rec.run_id, store, bridge))
    assert bridge.ended == []
    assert store.data == {}


def test_finalize_publishes_end_even_if_mark_fails():
    """落态失败不阻断 [DONE]（与 _finish_run 相同的顺序语义：
    前端解封优先于状态快照的完美）。"""
    rec = _make_active_run()
    store, bridge = FakeStore(fail_write=True), FakeBridge()
    old = settings.run_watchdog_grace_sec
    settings.run_watchdog_grace_sec = 0
    try:
        asyncio.run(chat_mod._watchdog_finalize(rec.run_id, store, bridge))
    finally:
        settings.run_watchdog_grace_sec = old
    assert bridge.ended == [rec.run_id]
    assert rec.status == rr.STATUS_FAILED, "进程内态仍要推进（store 写不进去是旁路问题）"


def test_finalize_clears_escalation_marker_on_all_paths():
    """早退路径同样清升级标记——否则恢复心跳后该 run 永远无法再被升级。"""
    rec = _make_active_run()
    asyncio.run(rr.mark(FakeStore(), rec.run_id, rr.STATUS_INTERRUPTED))
    chat_mod._WATCHDOG_ESCALATED.add(rec.run_id)
    asyncio.run(chat_mod._watchdog_finalize(rec.run_id, FakeStore(), FakeBridge()))
    assert rec.run_id not in chat_mod._WATCHDOG_ESCALATED
