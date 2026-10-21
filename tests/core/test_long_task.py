# -*- coding: utf-8 -*-
"""长任务自动续查（core/long_task.py，2026-10-21 方案）。

## 这份测试在锁什么

生产事故 add0229b：262 页 OCR 要跑 5 小时，而单次 MCP 调用被封顶 600s，
MCP 侧只能每 300s 返回一次 `status=running`，由**模型**再调一次同参工具。
模型因此连续调同一工具 30~70 次 —— 与死循环同形，三套判据全部误杀
（第 5 轮 79%、第 6 轮 95% 工具占比被硬停，140 页结果全丢）。

修法是把续查循环沉到**工具内部**（对齐 deer-flow
`task_tool::_await_subagent_terminal`：1 次调用 + 工具内轮询）。
所以这里必须锁住四类行为，缺一条这个修法就不成立：

1. **只在 running 时续查**：completed/failed/无结构化返回必须立刻交回模型。
   「无结构化返回」尤其关键 —— 参数错、工具不存在这类确定性失败重试
   多少次都是同一结果，会白烧 8 小时预算。
2. **有边界**：总预算 + 进度停滞熔断 + 连续 RPC 失败上限，三者都不能缺，
   否则任务真死时会把整个 run 拖到天荒地老。
3. **取消能穿透**：run 被用户取消/看门狗取消时，CancelledError 必须原样
   抛出（吞掉它 = 收尾逻辑永远不跑）。
4. **不产生新的 tool_call**：这是收益的来源，也是它不会触发循环检测的原因
   —— 由 AST 接线测试（tests/api/test_chat_wiring.py）从调用形态上锁。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel

from src.core import long_task as lt
from src.core.config import settings


# ─────────────────────────── stubs ───────────────────────────


class _WaitSchema(BaseModel):
    file_path: str = ""
    wait_seconds: int = 90


class _NoWaitSchema(BaseModel):
    file_path: str = ""


class _Inner:
    """最小内层工具 stub：按预设脚本返回/抛错，并记录每次收到的参数。"""

    description = "stub"
    return_direct = False

    def __init__(self, name: str, script: list, args_schema=None):
        self.name = name
        self._script = list(script)
        self.args_schema = args_schema
        self.calls: list = []
        self.cancelled = 0

    async def ainvoke(self, input, config=None, **kwargs):
        self.calls.append(input)
        try:
            step = self._script.pop(0) if self._script else {"status": "completed"}
            if isinstance(step, BaseException):
                raise step
            if callable(step):
                return await step()
            return json.dumps(step) if isinstance(step, dict) else step
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def _running(pages: int, batch: int | None = None) -> dict:
    payload = {
        "success": True,
        "status": "running",
        "pages_total": 30,
        "pages_ocred": pages,
        "progress_hint": "OCR 仍在沙箱后台运行（本次已等待 300s）。后续是模型行动建议。",
    }
    if batch is not None:
        payload["batch_index"] = batch
        payload["batches_done"] = batch - 1
    return payload


def _completed(pages: int = 30) -> dict:
    return {"success": True, "status": "completed", "pages_ocred": pages,
            "pages_total": pages, "total_texts": 8561}


async def _drive(inner, **overrides):
    kwargs = dict(
        budget_sec=3600,
        rpc_timeout_sec=600,
        poll_wait_sec=300,
        supports_wait=False,
    )
    kwargs.update(overrides)
    return await lt.drive(
        inner.name,
        {"file_path": "/data/x.pdf"},
        lambda args: inner.ainvoke(args),
        **kwargs,
    )


# ─────────────────────── 结果形态归一 ───────────────────────


def test_result_text_handles_all_production_shapes():
    """四种形态都要能归一：生产实测 str / list[dict] 都出现过。

    Python repr 形态（`str([{'type': 'text', ...}])`）不会出现在生产运行路径，
    但会出现在「从 store 重放历史」里（store 用 str 落库）。不还原的话重放会
    一律判成"无结构化返回 ⇒ 不续查"，看起来像修法失效 —— 静默退化是最坏的
    失败模式，所以这里显式锁住还原。
    """
    assert lt.result_text('{"status":"running"}') == '{"status":"running"}'
    assert lt.result_text([{"type": "text", "text": '{"a":1}'}]) == '{"a":1}'
    assert lt.result_text(["甲", "乙"]) == "甲\n乙"
    # repr 形态 → 还原成 payload 文本
    assert lt.result_text(str([{"type": "text", "text": '{"status":"running"}'}])) == '{"status":"running"}'
    assert lt.payload_status(str([{"type": "text", "text": '{"status":"running"}'}])) == "running"
    assert lt.result_text(None) == ""
    # 不是 repr 的普通方括号文本不能被误当 list 解析
    assert lt.result_text("[1, 2, 3] 是页数范围") == "[1, 2, 3] 是页数范围"


def test_payload_status_only_trusts_structured_json():
    """只认结构化 JSON：正文里出现 status 字样不算。"""
    assert lt.payload_status('{"status":"running"}') == "running"
    assert lt.payload_status([{"type": "text", "text": '{"status":"completed"}'}]) == "completed"
    # 非 JSON / 正文提及 ⇒ None（不续查）
    assert lt.payload_status("工具执行失败：status field missing") is None
    assert lt.payload_status("") is None


def test_is_durable_tool_reads_whitelist(monkeypatch):
    """白名单精确匹配：不做前缀/模式匹配，避免 execute 之类被误当长任务。"""
    monkeypatch.setattr(settings, "mcp_durable_task_tools", "ocr_pdf_to_text,paddleocr_vl")
    assert lt.is_durable_tool("ocr_pdf_to_text")
    assert lt.is_durable_tool("paddleocr_vl")
    assert not lt.is_durable_tool("ocr_pdf_to_text_v2")
    assert not lt.is_durable_tool("execute")

    monkeypatch.setattr(settings, "mcp_durable_task_tools", "")
    assert not lt.is_durable_tool("ocr_pdf_to_text"), "空白名单必须等于全关"


# ─────────────────────────── 主循环 ───────────────────────────


def test_completed_on_first_call_returns_immediately():
    async def _case():
        inner = _Inner("ocr_pdf_to_text", [_completed()])
        result = await _drive(inner)
        assert json.loads(result)["status"] == "completed"
        assert len(inner.calls) == 1
    asyncio.run(_case())


def test_loops_until_completed_without_touching_model():
    async def _case():
        """running×3 → completed：4 次真实调用，但模型侧只看到 1 次工具调用。

        这条是整个方案的核心断言：续查次数从「模型发 N 次 tool_call」
        变成「工具内部 N 次 invoke」。
        """
        inner = _Inner("ocr_pdf_to_text", [_running(5), _running(12), _running(24), _completed()])
        result = await _drive(inner)
        assert json.loads(result)["status"] == "completed"
        assert len(inner.calls) == 4
    asyncio.run(_case())


def test_unstructured_result_is_returned_not_retried():
    async def _case():
        """确定性失败（参数错/工具不存在）必须立刻返回，不能烧预算重试。"""
        inner = _Inner("ocr_pdf_to_text", ["Error: unknown parameter pages_v2", _completed()])
        result = await _drive(inner)
        assert "unknown parameter" in result
        assert len(inner.calls) == 1, "无结构化 status 时不该续查"
    asyncio.run(_case())


def test_failed_status_is_not_retried():
    async def _case():
        inner = _Inner("ocr_pdf_to_text", [{"status": "failed", "error": "sandbox died"}, _completed()])
        result = await _drive(inner)
        assert json.loads(result)["status"] == "failed"
        assert len(inner.calls) == 1
    asyncio.run(_case())


def test_stagnation_breaker_stops_after_no_progress():
    async def _case():
        """进度签名连续 N 轮不变 ⇒ 停（任务真死时不能把 run 拖到天荒地老）。"""
        inner = _Inner("ocr_pdf_to_text", [_running(7) for _ in range(40)])
        result = await _drive(inner)
        assert json.loads(result)["status"] == "running"
        # 第 1 轮建立基准，之后每轮 +1，到 _MAX_STALE_ROUNDS 触发
        assert len(inner.calls) == lt._MAX_STALE_ROUNDS + 1
        assert len(inner.calls) < 40, "熔断没生效，把整份脚本跑完了"
    asyncio.run(_case())


def test_progress_growth_keeps_loop_alive():
    async def _case():
        """页数在涨就不能熔断 —— 哪怕轮次远超 _MAX_STALE_ROUNDS。"""
        script = [_running(i) for i in range(1, lt._MAX_STALE_ROUNDS * 2)] + [_completed()]
        inner = _Inner("ocr_pdf_to_text", script)
        result = await _drive(inner)
        assert json.loads(result)["status"] == "completed"
    asyncio.run(_case())


def test_budget_exhaustion_returns_last_running():
    async def _case():
        """预算耗尽返回最后一次结果（running），不抛异常、不无限等。

        每轮带 20ms 耗时，把"1 秒预算"换算成可数的轮次 —— 否则瞬间跑完
        脚本，测的就不是预算而是脚本长度。
        """

        async def _tick(pages: int) -> str:
            await asyncio.sleep(0.02)
            return json.dumps(_running(pages))

        # 页数持续增长 ⇒ 不会触发停滞熔断，只能靠预算停
        inner = _Inner("ocr_pdf_to_text", [(lambda p=p: _tick(p)) for p in range(1, 400)])
        result = await _drive(inner, budget_sec=0.3)
        assert json.loads(result)["status"] == "running", "预算耗尽应返回 running 交回模型"
        assert 3 <= len(inner.calls) < 100, (
            "预算没起作用（跑了 %d 轮）" % len(inner.calls)
        )
    asyncio.run(_case())


def test_cancel_propagates_untouched():
    async def _case():
        """取消必须原样穿透：吞掉 CancelledError = 收尾逻辑永远不跑。"""
        started = asyncio.Event()

        async def _slow():
            started.set()
            await asyncio.sleep(30)
            return _completed()

        inner = _Inner("ocr_pdf_to_text", [{"status": "running"}, _slow])
        task = asyncio.create_task(_drive(inner))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert inner.cancelled == 1, "取消没传到内层工具"
    asyncio.run(_case())


def test_transient_rpc_errors_are_retried():
    async def _case():
        """MCP 滚动重启/网络抖动：连续几次失败不该断送 5 小时的任务。"""
        inner = _Inner("ocr_pdf_to_text", [
            _running(3),
            TimeoutError("rpc hang"),
            ConnectionError("mcp restarting"),
            _running(9),
            _completed(),
        ])
        result = await _drive(inner, rpc_timeout_sec=1)
        assert json.loads(result)["status"] == "completed"
        assert len(inner.calls) == 5
    asyncio.run(_case())


def test_gives_up_after_consecutive_rpc_errors():
    async def _case():
        """连续失败超过上限 ⇒ 抛出，交由 _MCPErrorShieldTool 转 ToolInvocationError。"""
        inner = _Inner("ocr_pdf_to_text", [TimeoutError("hang") for _ in range(5)])
        with pytest.raises(TimeoutError):
            await _drive(inner, rpc_timeout_sec=1)
        assert len(inner.calls) == lt._MAX_CONSECUTIVE_RPC_ERRORS
    asyncio.run(_case())


# ─────────────────────────── 参数与进度 ───────────────────────────


def test_wait_seconds_is_raised_to_poll_wait():
    async def _case():
        """模型不传 wait_seconds 时每轮只睡 90s（默认值）⇒ 续查轮次翻三倍。

        这里锁住：schema 有 wait_seconds 就统一抬到 mcp_durable_poll_wait_sec。
        """
        inner = _Inner("ocr_pdf_to_text", [_running(3), _completed()], args_schema=_WaitSchema)
        await _drive(inner, supports_wait=True, poll_wait_sec=300)
        assert all(c.get("wait_seconds") == 300 for c in inner.calls)

        # 模型显式给了别的值时也要被抬（不同 wait_seconds 会让参数指纹漂移）
        inner2 = _Inner("ocr_pdf_to_text", [_running(3), _completed()], args_schema=_WaitSchema)
        await lt.drive(
            inner2.name,
            {"file_path": "/x.pdf", "wait_seconds": 30},
            lambda args: inner2.ainvoke(args),
            budget_sec=60, rpc_timeout_sec=10, poll_wait_sec=300, supports_wait=True,
        )
        assert all(c.get("wait_seconds") == 300 for c in inner2.calls)
    asyncio.run(_case())


def test_no_wait_seconds_injected_when_schema_lacks_it():
    async def _case():
        """schema 没有该字段时不能凭空加参数（MCP 侧会报未知字段）。"""
        inner = _Inner("ocr_pdf_to_text", [_running(3), _completed()], args_schema=_NoWaitSchema)
        await _drive(inner, supports_wait=False)
        assert all("wait_seconds" not in c for c in inner.calls)
    asyncio.run(_case())


def test_progress_is_published_through_contextvar():
    async def _case():
        """进度必须能推给前端，否则工具内部跑几小时 = 前端毫无信号。"""
        seen: list[dict] = []

        async def _collect(payload):
            seen.append(payload)

        token = lt.bind_progress(_collect)
        try:
            inner = _Inner("ocr_pdf_to_text", [_running(5), _running(18), _completed()])
            await _drive(inner)
        finally:
            lt.reset_progress(token)

        assert seen, "一次进度都没上报"
        assert all(p["type"] == "tool_progress" for p in seen)
        assert [p.get("pages_ocred") for p in seen] == [5, 18]
        assert seen[0]["tool"] == "ocr_pdf_to_text"
        # stage 只取 progress_hint 的第一句：给模型看的行动建议不该灌到 UI
        assert "行动建议" not in seen[0]["stage"]
    asyncio.run(_case())


def test_reporter_absent_does_not_break_task():
    async def _case():
        """没有绑定 reporter（单测/CLI 调用）时任务照跑。"""
        lt.reset_progress(None)
        inner = _Inner("ocr_pdf_to_text", [_running(5), _completed()])
        result = await _drive(inner)
        assert json.loads(result)["status"] == "completed"
    asyncio.run(_case())


# ─────────────── 端到端：真的 _MCPErrorShieldTool（不是只测 drive）───────────────


def test_shield_loops_durable_tool_end_to_end(monkeypatch):
    """通过真实 shield 调用：模型侧 1 次 ainvoke，内部 3 次续查。

    只测 `drive` 会漏掉「接线是否真的接上」——本项目已有先例（1.0.13
    实现全绿但漏了模块级 import，上线即崩）。这里用生产真实包装类跑一遍。
    """
    from src.core.mcp import _MCPErrorShieldTool

    monkeypatch.setattr(settings, "mcp_durable_task_tools", "ocr_pdf_to_text")
    monkeypatch.setattr(settings, "mcp_durable_task_budget_sec", 600)
    monkeypatch.setattr(settings, "mcp_durable_poll_wait_sec", 300)

    inner = _Inner(
        "ocr_pdf_to_text",
        [_running(5), _running(19), _completed()],
        args_schema=_WaitSchema,
    )
    tool = _MCPErrorShieldTool(inner)
    result = asyncio.run(tool.ainvoke({"file_path": "/data/x.pdf"}))

    assert json.loads(result)["status"] == "completed"
    assert len(inner.calls) == 3, "shield 没有把续查接起来"
    # schema 有 wait_seconds ⇒ 每轮都抬到 300（否则续查轮次翻三倍）
    assert all(c.get("wait_seconds") == 300 for c in inner.calls)


def test_shield_leaves_non_durable_tools_untouched(monkeypatch):
    """非白名单工具必须保持旧行为：调一次就返回（running 也直接给模型）。

    没有这条，一旦白名单误配全开，`execute` 之类也会被内部循环吞住。
    """
    from src.core.mcp import _MCPErrorShieldTool

    monkeypatch.setattr(settings, "mcp_durable_task_tools", "ocr_pdf_to_text")

    inner = _Inner("execute", [_running(3), _completed()])
    tool = _MCPErrorShieldTool(inner)
    result = asyncio.run(tool.ainvoke({"command": "ls"}))

    assert json.loads(result)["status"] == "running", "非白名单工具不该被续查"
    assert len(inner.calls) == 1
