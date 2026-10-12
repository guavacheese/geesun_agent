"""run_registry 单测（P1-a，2026-10-12）。

判据取向同 `test_stream_bridge.py`：**断言对外可观察的行为**（进程内表状态 + store
里落了什么行），不去戳实现细节。全部 `asyncio.run` 直接驱动，不依赖 pytest-asyncio。

核心要证的几件事：
  1. 状态机：只有 running 记 `started_at`、只有终态记 `ended_at`，且**不覆盖已记的**
     （worker 收尾可能重复 mark，幂等是硬要求）。
  2. `persist` 失败**不抛**（状态快照是旁路信息，写不进去不该打断正在跑的任务），
     但必须返回 False 让调用方感知。
  3. `active_for_thread` 是 D2（重复提交 → 409 拒绝）的判据，终态 run 必须放行。
  4. 孤儿校正：`boot_id` 明确来自别的进程的活跃行 → 读到即改 failed(orphaned) 并
     写回；`boot_id` 缺失 → **不**校正（无从判断，宁可多等也不误杀）。
  5. 读路径的两种语义分工：`fetch_row` 含终态、`select_active_run` 只给活跃态。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.core import run_registry as rr


class _Item:
    """langgraph `BaseStore` 的 `Item` 最小替身（真实现同样有 `.value`）。"""

    def __init__(self, value: Any) -> None:
        self.value = value


class FakeStore:
    """只实现 run_registry 用到的两个方法。

    `raw_dict=True` 时 `aget` 直接返回 dict 而非 `_Item`——覆盖"后端不包 Item"
    这条分支（run_registry 用 `getattr(row, "value", row)` 两者都吃）。
    """

    def __init__(self, *, fail_write: bool = False, raw_dict: bool = False) -> None:
        self.data: dict[tuple, Any] = {}
        self.writes: list[tuple] = []
        self.fail_write = fail_write
        self.raw_dict = raw_dict

    async def aget(self, namespace, key):
        value = self.data.get((tuple(namespace), key))
        if value is None:
            return None
        return value if self.raw_dict else _Item(value)

    async def aput(self, namespace, key, value):
        if self.fail_write:
            raise RuntimeError("store is down")
        self.writes.append((tuple(namespace), key, value))
        self.data[(tuple(namespace), key)] = value


USER = "GY24428"
SESSION = "sess-1"
THREAD = "thread-1"


@pytest.fixture(autouse=True)
def _clean_registry():
    rr.reset_for_tests()
    yield
    rr.reset_for_tests()


def _make_run(user_id: str = USER, session_id: str = SESSION, thread_id: str = THREAD):
    return rr.create(user_id=user_id, session_id=session_id, thread_id=thread_id)


# ─────────────────────────── 1. 基础登记 ───────────────────────────


def test_create_registers_in_process_and_looks_up_by_both_keys():
    rec = _make_run()
    assert rr.get(rec.run_id) is rec
    assert rr.get_by_thread(THREAD) is rec
    assert rr.get("nope") is None
    assert rr.get_by_thread("nope") is None
    assert rec.status == rr.STATUS_PENDING and rec.is_active


def test_run_id_is_short_and_unique():
    ids = {rr.new_run_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i) == 16 for i in ids)


def test_namespace_is_namespaced_per_user():
    assert rr.namespace(USER) == ("runs", USER)
    assert rr.namespace("other") != rr.namespace(USER)


def test_drop_clears_thread_mapping_but_keeps_store_row():
    """drop 只清进程内索引；store 行要留着（它是"上一轮结局"的查询来源）。"""

    async def main():
        store = FakeStore()
        rec = _make_run()
        await rr.persist(store, rec)

        rr.drop(rec.run_id)
        return store, rec

    store, rec = asyncio.run(main())
    assert rr.get(rec.run_id) is None
    assert rr.get_by_thread(THREAD) is None
    assert store.data[(("runs", USER), SESSION)]["run_id"] == rec.run_id


def test_drop_does_not_evict_a_newer_run_on_same_thread():
    """换代后 drop 旧 run，不能把 `_by_thread` 里指向新 run 的映射删掉。"""
    old = _make_run()
    new = _make_run()
    assert rr.get_by_thread(THREAD) is new
    rr.drop(old.run_id)
    assert rr.get_by_thread(THREAD) is new


# ─────────────────────────── 2. active_for_thread（D2 判据） ───────────────────────────


def test_active_for_thread_rejects_while_pending_or_running_and_allows_after_terminal():
    store = FakeStore()
    rec = _make_run()
    assert rr.active_for_thread(THREAD) is rec  # pending 即拒绝重复提交

    asyncio.run(rr.mark(store, rec.run_id, rr.STATUS_RUNNING))
    assert rr.active_for_thread(THREAD) is rec

    for terminal in (rr.STATUS_COMPLETED, rr.STATUS_INTERRUPTED, rr.STATUS_FAILED):
        asyncio.run(rr.mark(store, rec.run_id, terminal))
        assert rr.active_for_thread(THREAD) is None, f"{terminal} 后必须放行"


def test_active_for_thread_unknown_thread_is_none():
    assert rr.active_for_thread("ghost") is None


# ─────────────────────────── 3. 状态推进 ───────────────────────────


def test_mark_rejects_illegal_status():
    store = FakeStore()
    rec = _make_run()
    with pytest.raises(ValueError, match="非法 run 状态"):
        asyncio.run(rr.mark(store, rec.run_id, "cancelled"))
    assert store.writes == [], "非法状态不该落盘"


def test_mark_unknown_run_warns_and_returns_none_without_writing():
    store = FakeStore()
    assert asyncio.run(rr.mark(store, "ghost", rr.STATUS_RUNNING)) is None
    assert store.writes == []


def test_mark_records_timestamps_once_and_is_idempotent():
    """worker 收尾可能重复 mark（正常收尾 + 外层兜底），时间戳不得被后一次改写。"""

    async def main():
        store = FakeStore()
        rec = _make_run()
        await rr.mark(store, rec.run_id, rr.STATUS_RUNNING)
        started = rec.started_at
        assert started is not None and rec.ended_at is None

        await rr.mark(store, rec.run_id, rr.STATUS_COMPLETED)
        ended = rec.ended_at
        assert ended is not None

        # 再 mark 一次同状态：两个时间戳都保持原值
        await rr.mark(store, rec.run_id, rr.STATUS_COMPLETED)
        return store, rec, started, ended

    store, rec, started, ended = asyncio.run(main())
    assert (rec.started_at, rec.ended_at) == (started, ended)
    assert store.data[(("runs", USER), SESSION)]["status"] == rr.STATUS_COMPLETED


def test_mark_error_only_set_when_provided():
    """不传 error 时不得把上一次的错误信息抹掉（终态也是"事实"，别覆盖成 None）。"""

    async def main():
        store = FakeStore()
        rec = _make_run()
        await rr.mark(store, rec.run_id, rr.STATUS_FAILED, error="boom")
        await rr.mark(store, rec.run_id, rr.STATUS_FAILED)
        return rec

    assert asyncio.run(main()).error == "boom"


# ─────────────────────────── 4. 持久化 ───────────────────────────


def test_persist_writes_row_under_session_key():
    async def main():
        store = FakeStore()
        rec = _make_run()
        ok = await rr.persist(store, rec)
        return store, rec, ok

    store, rec, ok = asyncio.run(main())
    assert ok is True
    ns, key, row = store.writes[0]
    assert ns == ("runs", USER)
    assert key == SESSION, "每会话一行（key=session_id），active-run 查询才是 O(1)"
    assert row["run_id"] == rec.run_id
    assert row["thread_id"] == THREAD
    assert row["boot_id"] == rr.BOOT_ID
    assert "task" not in row, "进程内对象不得落盘"


def test_persist_swallows_store_failure_and_reports_false():
    """写快照失败不能打断正在跑的任务，但必须能感知（刷新后查不到状态）。"""

    async def main():
        store = FakeStore(fail_write=True)
        return await rr.persist(store, _make_run())

    assert asyncio.run(main()) is False


def test_mark_survives_store_failure():
    """store 挂了 mark 仍要推进进程内状态（task 还在跑，状态机不能卡住）。"""

    async def main():
        store = FakeStore(fail_write=True)
        rec = _make_run()
        await rr.mark(store, rec.run_id, rr.STATUS_RUNNING)
        return rec

    rec = asyncio.run(main())
    assert rec.status == rr.STATUS_RUNNING and rec.started_at is not None


def test_to_row_snapshot_shape():
    rec = _make_run()
    row = rec.to_row()
    assert set(row) == {
        "run_id",
        "thread_id",
        "status",
        "created_at",
        "started_at",
        "ended_at",
        "error",
        "boot_id",
        "updated_at",
    }


# ─────────────────────────── 5. 读路径与孤儿校正 ───────────────────────────


def _row(**over: Any) -> dict[str, Any]:
    base = {
        "run_id": "r-old",
        "thread_id": THREAD,
        "status": rr.STATUS_RUNNING,
        "created_at": 1,
        "started_at": 2,
        "ended_at": None,
        "error": None,
        "boot_id": "deadbeef",
        "updated_at": 3,
    }
    base.update(over)
    return base


def _seed(store: FakeStore, row: dict[str, Any]) -> None:
    store.data[(("runs", USER), SESSION)] = row


def test_fetch_row_returns_none_for_missing_or_non_dict():
    async def main():
        store = FakeStore()
        missing = await rr.fetch_row(store, user_id=USER, session_id=SESSION)
        _seed(store, "not-a-dict")  # type: ignore[arg-type]
        garbage = await rr.fetch_row(store, user_id=USER, session_id=SESSION)
        return missing, garbage

    assert asyncio.run(main()) == (None, None)


def test_fetch_row_reads_plain_dict_backend():
    """后端不包 `Item` 时（直接给 dict）也必须能读。"""

    async def main():
        store = FakeStore(raw_dict=True)
        _seed(store, _row(status=rr.STATUS_COMPLETED, boot_id=rr.BOOT_ID))
        return await rr.fetch_row(store, user_id=USER, session_id=SESSION)

    assert asyncio.run(main())["status"] == rr.STATUS_COMPLETED


def test_orphan_active_run_is_reconciled_and_written_back():
    """★ 上个进程留下的 running：读到即改 failed(orphaned)，并写回 store。"""

    async def main():
        store = FakeStore()
        _seed(store, _row())
        row = await rr.fetch_row(store, user_id=USER, session_id=SESSION)
        stored = store.data[(("runs", USER), SESSION)]
        return row, stored, store.writes

    row, stored, writes = asyncio.run(main())
    assert row["status"] == rr.STATUS_FAILED
    assert "orphaned" in row["error"]
    assert row["ended_at"] is not None
    assert row["boot_id"] == rr.BOOT_ID, "校正后归属本进程"
    # 写回是必须的：不写回则每次读都重复发现、重复告警
    assert stored["status"] == rr.STATUS_FAILED and len(writes) == 1


def test_select_active_run_hides_orphan_and_writes_back():
    async def main():
        store = FakeStore()
        _seed(store, _row())
        active = await rr.select_active_run(store, user_id=USER, session_id=SESSION)
        stored = store.data[(("runs", USER), SESSION)]
        return active, stored

    active, stored = asyncio.run(main())
    assert active is None, "孤儿 run 不得被当成活跃（否则前端永远等一个不存在的 run）"
    assert stored["status"] == rr.STATUS_FAILED


def test_live_run_of_this_process_is_returned_and_not_rewritten():
    """本进程的活跃 run：原样返回、**不**写回（读路径不该有副作用）。"""

    async def main():
        store = FakeStore()
        _seed(store, _row(status=rr.STATUS_PENDING, boot_id=rr.BOOT_ID))
        active = await rr.select_active_run(store, user_id=USER, session_id=SESSION)
        return active, store.writes

    active, writes = asyncio.run(main())
    assert active is not None and active["status"] == rr.STATUS_PENDING
    assert writes == []


def test_missing_boot_id_is_not_treated_as_orphan():
    """`boot_id` 缺失 = 无从判断 ≠ 确定来自别的进程 → 不校正（宁可多等，不误杀）。"""

    async def main():
        store = FakeStore()
        _seed(store, _row(boot_id=None))
        active = await rr.select_active_run(store, user_id=USER, session_id=SESSION)
        return active, store.writes

    active, writes = asyncio.run(main())
    assert active is not None, "无从判断时必须保留，别把同进程的活 run 误标 failed"
    assert writes == []


def test_orphan_terminal_row_is_left_alone():
    """已是终态的旧行：不校正、不改写（它承载"上一轮的结局"）。"""

    async def main():
        store = FakeStore()
        row = _row(status=rr.STATUS_INTERRUPTED, boot_id="deadbeef")
        _seed(store, row)
        fetched = await rr.fetch_row(store, user_id=USER, session_id=SESSION)
        active = await rr.select_active_run(store, user_id=USER, session_id=SESSION)
        return fetched, active, store.writes

    fetched, active, writes = asyncio.run(main())
    assert fetched["status"] == rr.STATUS_INTERRUPTED
    assert active is None, "终态不算活跃 → 刷新后不该显示「任务进行中」"
    assert writes == []


def test_orphan_row_corrected_only_once_across_repeated_reads():
    """重复读不得反复告警/反复写（写回一次后行已归属本进程）。"""

    async def main():
        store = FakeStore()
        _seed(store, _row())
        for _ in range(3):
            await rr.fetch_row(store, user_id=USER, session_id=SESSION)
        return len(store.writes)

    assert asyncio.run(main()) == 1


def test_end_to_end_refresh_scenario():
    """端到端：worker 建 run → 落盘 → 中途刷新（只有读）→ 收尾 mark 终态。

    这一串就是 P1-b 端点的读路径：刷新后必须能查到"有 run 在跑 + 它的 run_id 可续订"，
    且 run 本身不因刷新而死（本模块只管状态，桥负责不死亡）。
    """

    async def main():
        store = FakeStore()
        rec = _make_run()
        await rr.mark(store, rec.run_id, rr.STATUS_RUNNING)

        # 刷新：前端问 active-run
        active = await rr.select_active_run(store, user_id=USER, session_id=SESSION)
        # worker 收尾
        await rr.mark(store, rec.run_id, rr.STATUS_INTERRUPTED)
        rr.drop(rec.run_id)
        # 再刷新：没有活跃 run 了，但结局仍可读
        after = await rr.select_active_run(store, user_id=USER, session_id=SESSION)
        last = await rr.fetch_row(store, user_id=USER, session_id=SESSION)
        return rec, active, after, last

    rec, active, after, last = asyncio.run(main())
    assert active is not None and active["run_id"] == rec.run_id
    assert after is None
    assert last["status"] == rr.STATUS_INTERRUPTED
    assert last["started_at"] is not None and last["ended_at"] is not None
