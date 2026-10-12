"""P1-c D5：setup 阶段预写本轮 user 消息（幂等与边界）。

## 为什么这件事值得单独一组测试

改造前 `_persist_session` 只在**流结束**时落盘，于是"第 2 轮跑到一半刷新"会撞上两个
同源现象：消息列表里看不到刚问的问题；续订拿到的追加帧被拼到**上一轮**的 AI 上（串轮）。
补上预写才有正确的落点 —— 但预写条目一旦写进去就**不会再被流尾改写**（水印只追加其后），
所以内容必须一次对、定位必须一次准。这两点都不是"看一眼代码像对的"，故逐条钉住。

被测三件事：
  ① ``_strip_injected_path_hint`` —— 与流尾 ``_persist_session`` 逐字同源；
  ② ``_write_setup_user_message`` —— 写到水位的下一个序号、失败不阻断、旧格式不写；
  ③ **id 传递是承重的** —— setup 与 LangGraph checkpoint 必须共用同一个 uuid，
     否则流尾会退化成 by_length，而那时 store 与 history 一样长 ⇒ 判为"无新增"，
     **AI 消息永远写不进去**（下方 `test_mismatched_id_*` 是这条的负向对照）。

运行前提（本机沙箱已备齐）:
    python -m pytest tests/api/test_setup_user_message.py -q

⚠ 顶层导入会拖进整个服务栈；缺运行期依赖的环境整体跳过，不报 collection error。
"""

from __future__ import annotations

import asyncio

import pytest

try:
    from src.api.endpoints.chat import (
        _plan_message_append,
        _strip_injected_path_hint,
        _write_setup_user_message,
    )

    _IMPORT_ERROR: Exception | None = None
except Exception as e:  # noqa: BLE001 - 导入期任何失败都按"本环境不具备"整体跳过
    _IMPORT_ERROR = e

if _IMPORT_ERROR is not None:  # pragma: no cover - 环境相关
    pytest.skip(
        f"chat.py 不可导入（缺运行期依赖）: {_IMPORT_ERROR}", allow_module_level=True
    )


UID = "GY24428"
SID = "sess-d5"


# ─── 假存储介质（只实现 D5 用到的两个方法，语义对齐 database.py） ─────────────


class _MsgStore:
    """把 ``store`` 的 messages 命名空间做成一个 ``{序号: entry}`` 字典。

    · ``aget_message_watermark`` —— 返回 (最大数字序号, 该条目的 id, 非数字条目数)，
      与 ``database.py:343`` 的语义一致；
    · ``awrite_messages`` —— 覆盖写给定 key。

    只换存储介质，被测的定位逻辑（``_plan_message_append``）仍是生产代码。
    """

    def __init__(self, rows: dict[int | str, dict] | None = None) -> None:
        self.rows: dict[int | str, dict] = dict(rows or {})
        self.fail_watermark = False
        self.fail_write = False

    async def aget_message_watermark(self, prefix: str):
        if self.fail_watermark:
            raise RuntimeError("watermark boom")
        numeric = [k for k in self.rows if isinstance(k, int)]
        legacy = sum(1 for k in self.rows if not isinstance(k, int))
        if not numeric:
            return 0, None, legacy
        top = max(numeric)
        return top, self.rows[top].get("id"), legacy

    async def awrite_messages(self, namespace, pairs):
        if self.fail_write:
            raise RuntimeError("write boom")
        for key, entry in pairs:
            self.rows[int(key)] = entry


def _run(coro_fn):
    return asyncio.run(coro_fn())


def _write(store, *, message_id: str, content: str = "你好"):
    return _write_setup_user_message(
        store, user_id=UID, session_id=SID, message_id=message_id, content=content
    )


# ─── ① 前缀清洗：setup 与流尾必须逐字同源 ──────────────────────────────────


def test_strip_removes_injected_path_hint():
    """注入的 path_hint 与正文之间用空行分隔 ⇒ 只留正文。"""
    injected = "沙箱 ID：abc\n当前用户：张三（user）\n\n【当前会话路径】\n输入文件：/uploads/x/\n\n分析这份报表"
    assert _strip_injected_path_hint(injected) == "分析这份报表"


def test_strip_is_passthrough_without_blank_line():
    """没有空行 ⇒ 原样返回（不能把整条正文当成前缀删掉）。"""
    assert _strip_injected_path_hint("单段正文，没有空行") == "单段正文，没有空行"


def test_strip_keeps_only_last_paragraph():
    """既有语义（本次只做提取、不改判据）：用户自己在正文里打空行 ⇒ 只留最后一段。

    这是**已知的既有行为**，不是本函数引入的。写成测试是为了让"setup 预写"和
    "流尾落盘"共用它这件事有据可查 —— 两边一起错 = 行为与改造前一致，两边不一致
    才会变成用户可见的差异。要改判据需单独一轮并带迁移。
    """
    assert _strip_injected_path_hint("第一段\n\n第二段") == "第二段"


# ─── ② 预写的写入门槛 ──────────────────────────────────────────────────────


def test_setup_writes_at_watermark_plus_one():
    """写入位置 = 已存最大序号 + 1，id 原样保留（这是流尾定位的唯一线索）。"""
    async def _go():
        store = _MsgStore({1: {"id": "old", "role": "user", "content": "旧"}})
        assert await _write(store, message_id="u-new", content="新问题") is True
        assert store.rows[2] == {
            "id": "u-new", "role": "user", "content": "新问题",
            "created_at": store.rows[2]["created_at"],  # 只校验键与关键字段
        }
        assert store.rows[2]["created_at"], "created_at 缺失（前端按它排序）"
        assert store.rows[1]["content"] == "旧", "既有条目被改写"

    _run(_go)


def test_setup_writes_into_empty_store_as_first_entry():
    async def _go():
        store = _MsgStore()
        assert await _write(store, message_id="u1") is True
        assert list(store.rows) == [1]

    _run(_go)


def test_setup_skips_when_legacy_rows_present():
    """存量仍是旧格式（单行 `{"items": [...]}`）⇒ 不写。

    与 ``_persist_session`` 同一道部署顺序防线：写了会让新旧格式并存，迁移脚本的
    前置守卫随即拒绝执行，留下需要人工对账的中间态。
    """
    async def _go():
        store = _MsgStore({"messages": {"items": [{"role": "user"}]}})
        assert await _write(store, message_id="u1") is False
        assert store.rows == {"messages": {"items": [{"role": "user"}]}}, "旧格式存量下写了新条目"

    _run(_go)


def test_setup_swallows_watermark_failure():
    """读水位炸了 ⇒ 返回 False 且**不抛**：预写失败不该把整轮对话拖垮。"""
    async def _go():
        store = _MsgStore()
        store.fail_watermark = True
        assert await _write(store, message_id="u1") is False

    _run(_go)


def test_setup_swallows_write_failure():
    """写库炸了 ⇒ 返回 False 且不抛（消息最终由流尾 ``_persist_session`` 兜底）。"""
    async def _go():
        store = _MsgStore()
        store.fail_write = True
        assert await _write(store, message_id="u1") is False

    _run(_go)


# ─── ③ 承重点：id 必须与 checkpoint 共用同一份 ─────────────────────────────


def _history_with_explicit_id(message_id: str) -> list[dict]:
    """模拟 ``_persist_session`` 从 checkpoint 里读出来的 history（本轮两段）。

    关键在第一条的 ``id``：它来自 ``HumanMessage(id=...)``，即 chat 端点显式喂进去的
    那个 uuid。``_persist_session`` 取的就是 ``getattr(msg, "id", None)``。
    """
    return [
        {"id": message_id, "role": "user", "content": "本轮问题"},
        {"id": "ai-framework-generated", "role": "ai", "content": "本轮回答"},
    ]


def _compressed_turn2_history(user_message_id: str) -> list[dict]:
    """模拟「第 2 轮 + ``SummarizationMiddleware`` 触发」后的 history。

    数字要对上才复现得出来，所以写清楚：

      · setup 预写**之前** store 里是第 1 轮的 ``[u1, a1]``（序号 1、2）；
      · 预写把本轮 u2 写到序号 3 ⇒ 水位的 ``prev_count`` 就是 **3**；
      · 压缩返回 ``[RemoveMessage(REMOVE_ALL_MESSAGES), 摘要, *保留的尾部]``，
        **history 会变短** —— 只剩 ``[摘要, u2, a2]`` 三条。

    于是 ``len(history) == prev_count == 3``：by_length 的判据 ``len > prev_count``
    为假 ⇒ 判"无新增"（``mode="none"``）；而 by_id 只要能在 history 里认出 u2，
    就能从它之后把 a2 补回去。这正是 `_plan_message_append` 设计 by_id 要绕开的场景。
    """
    return [
        {"id": "summary-1", "role": "summary", "content": "[之前对话的摘要]"},
        {"id": user_message_id, "role": "user", "content": "本轮问题"},
        {"id": "ai-turn2", "role": "ai", "content": "本轮回答"},
    ]


def _store_after_turn1_setup() -> _MsgStore:
    """第 1 轮已归档（序号 1、2），等待本轮 setup 预写序号 3。"""
    return _MsgStore({
        1: {"id": "u1", "role": "user", "content": "问一"},
        2: {"id": "a1", "role": "ai", "content": "答一"},
    })


def test_stream_end_appends_only_ai_when_ids_match():
    """setup 预写 + 流尾水印**同一 id** ⇒ 只补写 AI，user 条目不重复、不被改写。"""
    async def _go():
        store = _MsgStore()
        assert await _write(store, message_id="u-turn1", content="本轮问题") is True

        prev_count, last_stored_id, legacy = await store.aget_message_watermark("p")
        assert (prev_count, last_stored_id, legacy) == (1, "u-turn1", 0)

        next_seq, new_items, mode = _plan_message_append(
            _history_with_explicit_id("u-turn1"), prev_count, last_stored_id
        )
        assert mode == "by_id", f"没走 by_id 定位（退化路径，AI 可能写不进去）: {mode}"
        assert next_seq == 2
        assert [i["role"] for i in new_items] == ["ai"], (
            f"追加范围应只含 AI 消息，实际 {[i['role'] for i in new_items]}"
        )

    _run(_go)


def test_missing_id_loses_turn_when_history_compressed():
    """**负向对照**：预写的条目没带 id ⇒ 流尾退化成 by_length，压缩后整轮回答丢失。

    这条是本组最要紧的一条，因为失败形态**完全静默**：无异常、无日志、无告警 ——
    只有用户刷新后看不到回答（store 里少了本轮 AI 那段）。

    退化链：预写把水位的 ``last_stored_id`` 换成了"刚写的这条"；它没有 id（或 id 在
    history 里找不到）⇒ ``_plan_message_append`` 只能退回 ``by_length`` ⇒ 压缩后
    ``len(history) == prev_count`` ⇒ 判"无新增" ⇒ 一条都不写。

    下一组同数据、只把 id 补上就能正确写回，两者对照才说明 id 是承重的。
    """
    async def _go():
        store = _store_after_turn1_setup()
        # message_id=None 模拟"预写条目没有 id"（改造前 dict 形式：id 由框架生成，拿不到）
        assert await _write(store, message_id=None, content="本轮问题") is True

        prev_count, last_stored_id, _ = await store.aget_message_watermark("p")
        assert (prev_count, last_stored_id) == (3, None), "前置条件不对，复现不出来"

        _next_seq, new_items, mode = _plan_message_append(
            _compressed_turn2_history("u2"), prev_count, last_stored_id
        )
        assert mode == "none", f"预期退化成 none，实际 {mode}"
        assert new_items == [], "负向对照没有复现丢失 —— 本组断言失去意义"

    _run(_go)


def test_matching_id_survives_history_compression():
    """同一份被压缩的 history，只要 id 对得上 ⇒ by_id 仍能把本轮 AI 补写回去。"""
    async def _go():
        store = _store_after_turn1_setup()
        assert await _write(store, message_id="u2", content="本轮问题") is True

        prev_count, last_stored_id, _ = await store.aget_message_watermark("p")
        assert (prev_count, last_stored_id) == (3, "u2")

        next_seq, new_items, mode = _plan_message_append(
            _compressed_turn2_history("u2"), prev_count, last_stored_id
        )
        assert mode == "by_id", f"压缩后没有走 by_id（回退成 {mode}）"
        assert next_seq == 4
        assert [i["role"] for i in new_items] == ["ai"], (
            f"追加范围应只含 AI 消息，实际 {[i['role'] for i in new_items]}"
        )

    _run(_go)


def test_two_turns_produce_gapless_store():
    """连跑两轮：store 序列必须是 ``[u1, ai1, u2, ai2]``，无空洞、无重复。"""
    async def _go():
        store = _MsgStore()

        # 第 1 轮
        assert await _write(store, message_id="u1", content="问一") is True
        prev, wid, _ = await store.aget_message_watermark("p")
        seq, items, mode = _plan_message_append(
            [{"id": "u1", "role": "user"}, {"id": "a1", "role": "ai"}], prev, wid
        )
        assert mode == "by_id" and seq == 2 and [i["id"] for i in items] == ["a1"]
        await store.awrite_messages("p", [(f"{seq}", items[0])])

        # 第 2 轮
        assert await _write(store, message_id="u2", content="问二") is True
        prev, wid, _ = await store.aget_message_watermark("p")
        seq, items, mode = _plan_message_append(
            [
                {"id": "u1", "role": "user"},
                {"id": "a1", "role": "ai"},
                {"id": "u2", "role": "user"},
                {"id": "a2", "role": "ai"},
            ],
            prev, wid,
        )
        assert mode == "by_id" and seq == 4 and [i["id"] for i in items] == ["a2"]
        await store.awrite_messages("p", [(f"{seq}", items[0])])

        assert [store.rows[k]["id"] for k in sorted(store.rows)] == ["u1", "a1", "u2", "a2"]

    _run(_go)
