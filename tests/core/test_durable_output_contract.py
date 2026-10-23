# -*- coding: utf-8 -*-
"""durable 长任务返回值契约（2026-10-23，589e99f5 事故）。

事故：GY24428:589e99f5 的 262 页 OCR 跑满 4h45m，第 60 轮 status=completed，
0.4 秒后整轮崩在

    Tool ocr_pdf_to_text returned a list with invalid element types:
    expected all Command or ToolMessage

结论全丢（沙箱已回收、宿主只落了两份 PDF），用户只看到这句异常。

三跳根因（每一跳在本文件里都有独立证据，不用"看起来像"代替验证）：
  ① `long_task.drive` 的**句柄续查**分支（方案 D）构造
     `poll_args = {"sandbox_id", "job_id", "wait_seconds"}` —— 纯句柄 dict，
     既没有 `type="tool_call"` 也没有 `id`，`_is_tool_call()` 判 False ⇒
     `tool_call_id=None`；
     首轮 `invoke(call_args)` 是 `dict(tool_call)`，保留了 `type`/`id`，
     所以**首轮正常、续查轮才出问题**（这也是 589e99f5 跑到第 60 轮才炸的
     直接原因：中间 59 轮都不外抛）；
  ② langchain-core `BaseTool._format_output` 有一条硬规则
     `if isinstance(content, ToolOutputMixin) or tool_call_id is None:
      return content` —— 无 id 就原样返回裸 content（MCP 的
     `[{"type":"text","text":"..."}]`）；
  ③ langgraph `ToolNode._normalize_tool_response` 只接受
     `Command` / `ToolMessage` / `list[Command|ToolMessage]`，裸 list 直接
     `raise TypeError`。

★ 为什么上线两周才暴露：前 59 轮 status=running 全在 drive 内部循环、
  从不外抛，**completed 才第一次真正把结果交出去** —— 这条路径一次都没走通过
  （此前长任务要么被循环检测掐死、要么被用户取消）。
★ 为什么 e2e 没抓到：`_ocr_space_inner.py` / `_ocr_batch_inner.py` 在容器内
  直调 MCP，绕过 agent 的 tool_node —— 测不到「返回值要过
  _normalize_tool_response」这一层。「桩比实现更宽容」的又一例。
"""

import asyncio
import json

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt.tool_node import ToolNode
from pydantic import BaseModel

from src.core.config import settings
from src.core.mcp import _MCPErrorShieldTool


# ── MCP 工具替身：**故意用真实的 BaseTool.ainvoke + content_and_artifact** ──
# 自己写 `async def ainvoke(): return [...]` 的桩会绕过 `_format_output`，
# 于是第 ② 跳根因根本不会被复现，测试就会假绿。这里让 langchain 自己包装。


class _OcrArgs(BaseModel):
    file_path: str = ""
    wait_seconds: int = 90  # 有它 ⇒ supports_wait=True，drive 会抬到 300


class _StatusArgs(BaseModel):
    sandbox_id: str = ""
    job_id: str = ""
    wait_seconds: int = 90


_RUNNING = {
    "status": "running",
    "batch": "3/7",
    "batch_index": 3,
    "batches_done": 2,
    "pages_ocred": 120,
    "pages_total": 262,
}


def _blocks(payload: dict) -> list:
    """MCP content blocks（生产同形）。"""
    return [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]


class _McpLikeTool(BaseTool):
    """模拟 langchain_mcp_adapters 产出的工具（response_format=content_and_artifact）。

    `_arun` 返回 `(content, artifact)`；content 是 MCP 的 text blocks list。
    """

    name: str = "ocr_pdf_to_text"
    description: str = "stub"
    args_schema: type[BaseModel] = _OcrArgs
    response_format: str = "content_and_artifact"

    payloads: list = []
    seen: list = []

    def _run(self, *args, **kwargs):  # pragma: no cover - 只用异步路径
        raise NotImplementedError

    async def _arun(self, file_path: str = "", wait_seconds: int = 90, **kwargs):
        self.seen.append(("ocr", {"file_path": file_path,
                                  "wait_seconds": wait_seconds}))
        data = self.payloads[min(len(self.seen) - 1, len(self.payloads) - 1)]
        return _blocks(data), _blocks(data)


class _McpLikeStatusTool(_McpLikeTool):
    """轻量续查工具（方案 D，MCP 侧 `ocr_status`）。"""

    name: str = "ocr_status"
    args_schema: type[BaseModel] = _StatusArgs

    async def _arun(self, sandbox_id: str = "", job_id: str = "",
                    wait_seconds: int = 90, **kwargs):
        self.seen.append(("status", {"sandbox_id": sandbox_id,
                                     "job_id": job_id,
                                     "wait_seconds": wait_seconds}))
        data = self.payloads[min(len(self.seen) - 1, len(self.payloads) - 1)]
        return _blocks(data), _blocks(data)


_COMPLETED = {
    "status": "completed",
    "batch": "7/7",
    "batch_index": 7,
    "batches_done": 7,
    "batches_failed": [],
    "pages_ocred": 262,
    "pages_ocred_total": 262,
    "result_path": "/home/user/_ocr/result.txt",
}


def _text_of(content) -> str:
    """ToolMessage.content 可能是 str，也可能是 MCP blocks list。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    return str(content)


def _call(cid: str = "call_abc123") -> dict:
    """langgraph 传给工具的 tool_call 形态（**带 id**）。"""
    return {
        "name": "ocr_pdf_to_text",
        "args": {"file_path": "/home/user/x.pdf"},
        "id": cid,
        "type": "tool_call",
    }


@pytest.fixture(autouse=True)
def _durable_on(monkeypatch):
    monkeypatch.setattr(settings, "mcp_durable_task_tools", "ocr_pdf_to_text")
    monkeypatch.setattr(settings, "mcp_durable_task_budget_sec", 600)
    monkeypatch.setattr(settings, "mcp_durable_poll_wait_sec", 300)
    monkeypatch.setattr(settings, "mcp_durable_status_tool", "ocr_status")


def _shield(ocr_payloads: list, status_payloads: list | None = None):
    """构造带轻量续查工具的 shield（与生产 `_shield_tools` 同形）。"""
    ocr = _McpLikeTool(payloads=ocr_payloads)
    shield = _MCPErrorShieldTool(ocr)
    if status_payloads is not None:
        shield._status_tool = _McpLikeStatusTool(payloads=status_payloads)
    return shield, ocr


# ─────────────────── 第 ①② 跳：裸 list 到底怎么产生的 ───────────────────


def test_plain_args_dict_loses_tool_call_id_and_yields_raw_list():
    """根因机制：**纯参数 dict**（无 type/id）交给内层 ⇒ 内层返回裸 list。

    这正是句柄续查 `poll_args` 的形态，也是 `_format_output` 那条
    `tool_call_id is None` 硬规则被踩中的唯一入口。
    同时反向对照：带 type/id 的 tool_call dict ⇒ langchain 正常包 ToolMessage。
    两组对照一起跑，才能证明"id 是唯一开关"，而不是我们的推测。
    """
    tool = _McpLikeTool(payloads=[_COMPLETED])
    raw = asyncio.run(tool.ainvoke({"file_path": "/home/user/x.pdf"}))
    assert isinstance(raw, list), (
        "纯参数 dict 调用应返回裸 MCP content list（根因第②跳），实际 %s"
        % type(raw).__name__
    )
    wrapped = asyncio.run(tool.ainvoke(_call()))
    assert isinstance(wrapped, ToolMessage), (
        "带 id 的 tool_call 应被 langchain 包成 ToolMessage，实际 %s"
        % type(wrapped).__name__
    )
    assert wrapped.tool_call_id == "call_abc123"


def test_raw_list_is_rejected_by_real_tool_node():
    """第 ③ 跳：裸 list 交给真实 `ToolNode._normalize_tool_response` 必 TypeError。

    这就是 589e99f5 用户看到的那句异常。用真实实现校验，确保"归一化"
    不是可选的化妆。
    """
    node = ToolNode.__new__(ToolNode)  # 只借类型校验，不建图
    with pytest.raises(TypeError) as ei:
        node._normalize_tool_response(
            [{"type": "text", "text": "{}"}], _call(), "tool_calls"
        )
    assert "invalid element types" in str(ei.value)


# ─────────────────── 主线复放：589e99f5 句柄续查路径 ───────────────────


def test_durable_handle_polling_completed_returns_tool_message():
    """589e99f5 复放：首轮下任务句柄 → ocr_status 续查 → completed。

    修法前：续查轮（纯句柄 dict）返回裸 list ⇒ 第 60 轮整轮崩、4h45m 白跑。
    修法后：ToolMessage，tool_call_id 保留（否则记忆/前端的工具调用对不上）。
    这条断言顺序也很关键：先跑 4 轮 running 再 completed，模拟"只有终局才
    外抛"的真实节律 —— 如果只看首轮，这个 bug 永远测不出来。
    """
    shield, ocr = _shield(
        ocr_payloads=[{
            **_RUNNING, "job_id": "job-589", "sandbox_id": "sbx-1",
        }],
        status_payloads=[
            {"status": "running", "pages_ocred": 160, "pages_total": 262},
            {"status": "running", "pages_ocred": 220, "pages_total": 262},
            _COMPLETED,
        ],
    )
    result = asyncio.run(shield.ainvoke(_call("call_7f3a")))

    assert isinstance(result, ToolMessage), (
        "durable 续查返回值必须是 ToolMessage，实际 %s —— 裸 list 会被 "
        "ToolNode 判 invalid element types（589e99f5）" % type(result).__name__
    )
    assert result.tool_call_id == "call_7f3a", (
        "tool_call_id 丢了：前端/记忆里的工具调用会对不上"
    )
    assert result.name == "ocr_pdf_to_text"
    payload = json.loads(_text_of(result.content))
    assert payload["status"] == "completed"
    assert payload["pages_ocred"] == 262
    assert payload["batches_failed"] == []

    # 关键判据：真实 ToolNode 归一化函数接受它（事故现场就是死在这一步）
    node = ToolNode.__new__(ToolNode)
    normalized = node._normalize_tool_response(result, _call("call_7f3a"), "tool_calls")
    assert isinstance(normalized, ToolMessage)

    # 句柄续查确实走了（否则这条测试就只是"首轮正常"的重复覆盖）
    assert len(ocr.seen) == 1, "首轮之后应切轻量续查，不该反复重发全量参数"


def test_durable_single_round_completion_also_tool_message():
    """首轮就 completed（无句柄）：也必须返回 ToolMessage。

    这条路径本来就正常（tool_call dict 被 langchain 包装），但它保证
    归一化层不会把已有正确形态改坏 —— 修法不能只修一条路。
    """
    shield, _ = _shield(ocr_payloads=[_COMPLETED])
    result = asyncio.run(shield.ainvoke(_call("call_single")))
    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == "call_single"
    assert json.loads(_text_of(result.content))["status"] == "completed"


def test_durable_without_tool_call_id_still_returns_tool_message():
    """探针/单测裸调（无 id）：仍返回 ToolMessage（空 id），类型契约不破。

    若这里退化成返回裸 list，说明归一化依赖了 id 存在 —— 那正是原 bug 的
    前提条件，修复本身不能依赖它。
    """
    shield, _ = _shield(ocr_payloads=[_COMPLETED])
    result = asyncio.run(shield.ainvoke({"file_path": "/home/user/x.pdf"}))
    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == ""
    assert json.loads(_text_of(result.content))["status"] == "completed"
