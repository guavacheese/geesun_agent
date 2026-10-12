"""P1-b 三个新端点的 **HTTP 层**验证（真路由 + 真依赖注入 + 真状态码）。

与相邻两个文件的分工：
  · ``test_run_endpoints.py`` —— 帧编码的行为 + 桥→帧的跨层字节流 + 接线断言。
  · ``test_chat_wiring.py``   —— chat.py 的结构不变量（AST 静态）。
  · 本文件 —— 把 router 真的挂到 FastAPI 上跑请求。它覆盖前两者**结构上覆盖不到**
    的东西：路径参数解析（``{run_id}``）、``Header(alias="Last-Event-ID")`` 是否真的
    从请求头取值、依赖注入是否把假 store / 假用户送进来、404/409/200 的**状态码与
    响应体形状**。

为什么值得单独测状态码：P1-c 的前端要按状态码分支（404 ⇒ 放弃续订并回权威存储
重取；200 + ``run_id: null`` ⇒ 本轮已结束）。状态码写错不会让任何单测变红 ——
只会让前端走进错误分支。

运行前提（本机沙箱已备齐，见文件末的构造说明）:
    python -m pytest tests/api/test_run_endpoints_http.py -q

⚠ 本模块顶层导入会拖进整个服务栈（chat.py → MCP / 沙箱 / model）。缺运行期依赖的
环境（例如只想跑纯 AST 断言的轻量 CI）会自动**整体跳过**，而不是报 collection error。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

try:
    import httpx
    from fastapi import FastAPI

    from src.api.deps import get_current_user, get_store, get_stream_bridge
    from src.api.endpoints.chat import router as chat_router
    from src.api.endpoints.sessions import router as sessions_router
    from src.core import run_registry
    from src.core.stream_bridge import DELTA_TOKEN, MemoryStreamBridge
except Exception as e:  # noqa: BLE001 - 导入期任何失败都按"本环境不具备"整体跳过
    pytest.skip(f"端点模块不可导入（缺运行期依赖）: {e}", allow_module_level=True)


UID = "GY24428"
OTHER_UID = "GY99999"
SID = "sess-p1b"
THREAD = f"{UID}:{SID}"
PREFIX = "/api/v1"


# ─── 假依赖 ─────────────────────────────────────────────────────────────────


class _FakeStore:
    """只实现 run_registry / active-run 端点用到的两个方法。

    刻意不 mock 掉 ``run_registry`` 本身 —— 那样就测不到"孤儿校正 + 写回"这类真实
    行为。这里假的是**存储介质**，逻辑仍是生产代码。
    """

    def __init__(self, rows: dict | None = None) -> None:
        self.rows: dict[tuple, dict] = dict(rows or {})
        self.puts: list[tuple] = []

    async def aget(self, namespace, key):
        return self.rows.get((namespace, key))

    async def aput(self, namespace, key, value):
        self.rows[(namespace, key)] = value
        self.puts.append((namespace, key))


def _build_app(store: _FakeStore, bridge: MemoryStreamBridge) -> FastAPI:
    app = FastAPI()
    app.include_router(chat_router, prefix=PREFIX)
    app.include_router(sessions_router, prefix=PREFIX)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_stream_bridge] = lambda: bridge
    app.dependency_overrides[get_current_user] = lambda: {"user_id": UID}
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _run_http(coro_fn):
    """所有 HTTP + 桥的操作都跑在**同一个事件循环**里。

    不能分成多次 ``asyncio.run``：桥内部持有 ``asyncio.Condition`` / ``Queue``，
    它们虽然不再在构造时绑定 loop，但跨 loop 复用同一批对象是自找麻烦。
    """
    run_registry.reset_for_tests()
    return asyncio.run(coro_fn())


# ─── ① 续订端点：404 的两种来源，与"能真的吐出帧" ───────────────────────────


def test_stream_unknown_run_returns_404():
    """编造的 run_id 必须 404。

    这条守的是"桥对未知 run_id 会惰性建流"这个坑：若端点不先查登记表就 subscribe，
    任何编造的 id 都能换到一个永远只吐心跳的连接 —— 既是资源消耗面，也是进程内泄漏。
    """
    async def _go():
        app = _build_app(_FakeStore(), MemoryStreamBridge())
        async with _client(app) as client:
            r = await client.get(f"{PREFIX}/chat/runs/nope/stream")
            assert r.status_code == 404, f"实际 {r.status_code}: {r.text}"

    _run_http(_go)


def test_stream_other_users_run_returns_404_not_403():
    """别人的 run 一律 404（不是 403）。

    403 会告诉探测者"这个 run_id 真实存在"，等于把状态码当存在性探针。
    """
    async def _go():
        rec = run_registry.create(user_id=OTHER_UID, session_id=SID, thread_id=f"{OTHER_UID}:{SID}")
        app = _build_app(_FakeStore(), MemoryStreamBridge())
        async with _client(app) as client:
            r = await client.get(f"{PREFIX}/chat/runs/{rec.run_id}/stream")
            assert r.status_code == 404, f"实际 {r.status_code}"

    _run_http(_go)


def test_stream_serves_done_for_finished_run():
    """已结束（尚未回收）的 run 仍可订阅，且立刻拿到 ``data: [DONE]``。

    "刷新时任务刚好结束"走的正是这条路。它同时证明端点是真把桥接上了、并且
    ``[DONE]`` 这个前端唯一的正常结束判据确实在线上字节里。
    """
    async def _go():
        bridge = MemoryStreamBridge()
        rec = run_registry.create(user_id=UID, session_id=SID, thread_id=THREAD)
        await bridge.publish_end(rec.run_id)

        app = _build_app(_FakeStore(), bridge)
        async with _client(app) as client:
            r = await client.get(f"{PREFIX}/chat/runs/{rec.run_id}/stream")
            assert r.status_code == 200, f"实际 {r.status_code}: {r.text}"
            assert "text/event-stream" in r.headers.get("content-type", "")
            assert r.text.endswith("data: [DONE]\n\n"), f"实际字节: {r.text!r}"

    _run_http(_go)


def test_stream_honours_cursor_header_and_degrades_without_it():
    """同一份产物，两种订阅者拿到的正文帧形状**必须不同**：

      · 带 ``Last-Event-ID``（P1-c 起的前端）⇒ 替换型 ``snapshot`` 帧；
      · 不带（P1-b 上线时的前端）⇒ 降级成追加型 ``token`` 帧。

    这条同时证明两件事：``Header(alias="Last-Event-ID")`` 真的从请求头取到了值（写成
    普通参数就会恒为 None，而这个错误在纯函数单测里看不出来），以及 query 兜底入口
    可用。P1-c 之前上线后端时，前端正是靠后者这条路径才不丢正文。
    """
    async def _go():
        bridge = MemoryStreamBridge()
        rec = run_registry.create(user_id=UID, session_id=SID, thread_id=THREAD)
        await bridge.publish(rec.run_id, "ai_message_start", {"index": 0})
        await bridge.publish_delta(rec.run_id, DELTA_TOKEN, "正文")
        # 取游标：桥没有对外暴露"读某条事件的 id"的入口，测试直接读内部事件表
        event_id = bridge._streams[rec.run_id].events[0].id
        await bridge.publish_end(rec.run_id)

        app = _build_app(_FakeStore(), bridge)
        async with _client(app) as client:
            # ① 带游标（标准头）：替换语义的快照帧
            by_header = await client.get(
                f"{PREFIX}/chat/runs/{rec.run_id}/stream",
                headers={"Last-Event-ID": event_id},
            )
            assert by_header.status_code == 200, by_header.text
            assert '"type": "snapshot"' in by_header.text, by_header.text

            # ② 同一个游标走 query 兜底：结果一致
            by_query = await client.get(
                f"{PREFIX}/chat/runs/{rec.run_id}/stream",
                params={"last_event_id": event_id},
            )
            assert by_query.status_code == 200, by_query.text
            assert '"type": "snapshot"' in by_query.text, by_query.text

            # ③ 不带游标（老前端）：必须降级成追加型 token 帧
            fresh = await client.get(f"{PREFIX}/chat/runs/{rec.run_id}/stream")
            assert fresh.status_code == 200
            assert '{"type": "token", "content": "正文"}' in fresh.text, (
                f"新订阅者没有拿到降级追加帧（会丢前缀正文）: {fresh.text!r}"
            )
            assert '"type": "snapshot"' not in fresh.text, (
                f"新订阅者收到了 snapshot —— 未升级的前端会静默丢弃整帧: {fresh.text!r}"
            )

    _run_http(_go)


# ─── ② 终止端点：幂等、404、真的取消到 task ─────────────────────────────────


def test_cancel_unknown_run_returns_404():
    async def _go():
        app = _build_app(_FakeStore(), MemoryStreamBridge())
        async with _client(app) as client:
            r = await client.post(f"{PREFIX}/chat/runs/nope/cancel")
            assert r.status_code == 404

    _run_http(_go)


def test_cancel_finished_run_is_idempotent():
    """对已结束的 run 点终止：200 + ``cancelled: false`` + 如实回报终态。

    不报错是刻意的 —— 前端可能因网络重试而重复提交终止，报错只会弹一个对用户毫无
    意义的红框。
    """
    async def _go():
        rec = run_registry.create(user_id=UID, session_id=SID, thread_id=THREAD)
        rec.status = run_registry.STATUS_COMPLETED

        app = _build_app(_FakeStore(), MemoryStreamBridge())
        async with _client(app) as client:
            r = await client.post(f"{PREFIX}/chat/runs/{rec.run_id}/cancel")
            assert r.status_code == 200, f"实际 {r.status_code}: {r.text}"
            assert r.json() == {
                "run_id": rec.run_id,
                "cancelled": False,
                "status": run_registry.STATUS_COMPLETED,
            }

    _run_http(_go)


def test_cancel_active_run_cancels_worker_task():
    """活跃 run：真的把 worker task 取消掉（这正是「终止任务」按钮的落点）。

    同时验证返回体里的 status 仍是**取消前**的活跃态 —— 端点的职责是"受理终止"，
    终态由 worker 的收尾链落库（否则会造出「前端已解锁、消息却没落盘」的窗口）。
    """
    async def _go():
        rec = run_registry.create(user_id=UID, session_id=SID, thread_id=THREAD)
        rec.status = run_registry.STATUS_RUNNING

        async def _worker_body():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_worker_body())
        run_registry.attach_task(rec.run_id, task)

        app = _build_app(_FakeStore(), MemoryStreamBridge())
        async with _client(app) as client:
            r = await client.post(f"{PREFIX}/chat/runs/{rec.run_id}/cancel")
            assert r.status_code == 200, f"实际 {r.status_code}: {r.text}"
            body = r.json()
            assert body["cancelled"] is True
            assert body["status"] == run_registry.STATUS_RUNNING  # 取消前的态，非终态

        # 取消是异步投递的：让出一次循环再看 task 是否真的收到取消
        await asyncio.sleep(0)
        assert task.cancelled(), "worker task 没有被取消 —— 「终止任务」形同虚设"
        with contextlib.suppress(asyncio.CancelledError):
            await task

    _run_http(_go)


# ─── ③ active-run 端点：刷新后前端的第一站 ──────────────────────────────────


def test_active_run_returns_null_when_none():
    """没有在跑的 run ⇒ ``run_id: null``（前端据此不订阅）。

    会话不存在时也走同一分支 —— 本端点回答的是"有没有在跑的"，不是"会话在不在"。
    """
    async def _go():
        app = _build_app(_FakeStore(), MemoryStreamBridge())
        async with _client(app) as client:
            r = await client.get(f"{PREFIX}/sessions/{SID}/active-run")
            assert r.status_code == 200, f"实际 {r.status_code}: {r.text}"
            assert r.json() == {"run_id": None}

    _run_http(_go)


def test_active_run_returns_own_run_when_active():
    async def _go():
        rec = run_registry.create(user_id=UID, session_id=SID, thread_id=THREAD)
        rec.status = run_registry.STATUS_RUNNING
        store = _FakeStore()
        await run_registry.persist(store, rec)

        app = _build_app(store, MemoryStreamBridge())
        async with _client(app) as client:
            r = await client.get(f"{PREFIX}/sessions/{SID}/active-run")
            assert r.status_code == 200, f"实际 {r.status_code}: {r.text}"
            body = r.json()
            assert body["run_id"] == rec.run_id
            assert body["status"] == run_registry.STATUS_RUNNING

    _run_http(_go)


def test_active_run_corrects_and_writes_back_orphan():
    """孤儿行（上一个进程留下的活跃行）⇒ 校正为 failed **并写回**，对外返回 null。

    两半都要证：
      · 不校正的话，前端会永远等一个不存在的 run（进程都被杀了）；
      · 不写回的话，同一个孤儿行会在**每次**刷新时被重新"发现"一遍，store 里始终
        躺着一条 running。
    """
    async def _go():
        orphan = {
            "run_id": "run-from-dead-process",
            "thread_id": THREAD,
            "status": run_registry.STATUS_RUNNING,
            "created_at": 1,
            "started_at": 1,
            "ended_at": None,
            "error": None,
            "boot_id": "deadbeef0000",  # 明确不是本进程
            "updated_at": 1,
        }
        store = _FakeStore({(run_registry.namespace(UID), SID): orphan})

        app = _build_app(store, MemoryStreamBridge())
        async with _client(app) as client:
            r = await client.get(f"{PREFIX}/sessions/{SID}/active-run")
            assert r.status_code == 200
            assert r.json() == {"run_id": None}, "孤儿 run 被当成了活跃 run"

        row = store.rows[(run_registry.namespace(UID), SID)]
        assert row["status"] == run_registry.STATUS_FAILED, "孤儿行没有被校正"
        assert row["error"] and "orphaned" in row["error"]
        assert store.puts, "校正结果没有写回 store（每次刷新都会重新发现同一个孤儿）"

    _run_http(_go)


def test_active_run_keeps_terminal_row_out_of_active_answer():
    """终态行**不返回**但也**不清掉** —— 它承载"上一轮结局"（如"已中断"）。"""
    async def _go():
        row = {
            "run_id": "run-done",
            "thread_id": THREAD,
            "status": run_registry.STATUS_INTERRUPTED,
            "created_at": 1,
            "started_at": 1,
            "ended_at": 2,
            "error": None,
            "boot_id": run_registry.BOOT_ID,
            "updated_at": 2,
        }
        store = _FakeStore({(run_registry.namespace(UID), SID): row})

        app = _build_app(store, MemoryStreamBridge())
        async with _client(app) as client:
            r = await client.get(f"{PREFIX}/sessions/{SID}/active-run")
            assert r.json() == {"run_id": None}

        assert (run_registry.namespace(UID), SID) in store.rows, "终态行被清掉了"
        assert not store.puts, "终态行被无谓地重写了一遍"

    _run_http(_go)
