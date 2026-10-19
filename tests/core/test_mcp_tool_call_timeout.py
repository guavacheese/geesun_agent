# -*- coding: utf-8 -*-
"""MCP 工具调用墙钟超时（方案 3 工具侧，2026-10-19）。

事故背景 653d8515：MCP 服务端同步 e2b 调用堵死事件循环，工具调用 55 分钟
不返回，agent 侧无任何边界 ⇒ run 挂死 38 小时。MCP 侧已修
（asyncio.to_thread + submit-and-poll），本测试锁 agent 侧背压：
_MCPErrorShieldTool.ainvoke 必须 asyncio.wait_for 封顶，超时转
ToolInvocationError 回灌模型（不崩流），且消息引导原参重试续查。
"""

import asyncio

import pytest
from langgraph.prebuilt.tool_node import ToolInvocationError

from src.core.config import settings
from src.core.mcp import _MCPErrorShieldTool


class _Inner:
    """最小内层工具 stub：记录被取消次数，ainvoke 可配置为挂死/快返/抛错。"""

    name = "fake_tool"
    description = "stub"
    args_schema = None
    return_direct = False

    def __init__(self, behavior):
        self._behavior = behavior
        self.cancelled = 0

    async def ainvoke(self, input, config=None, **kwargs):
        try:
            return await self._behavior()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


async def _hang():
    await asyncio.sleep(3600)
    return {"never": True}


async def _fast():
    return {"ok": True}


async def _boom():
    raise RuntimeError("inner exploded")


def _wrap(behavior):
    return _MCPErrorShieldTool(_Inner(behavior))


def test_timeout_wraps_into_tool_invocation_error_not_crash():
    """挂死的工具调用必须在超时后转 ToolInvocationError（langgraph 唯一
    放行类型），而不是把 TimeoutError/CancelledError 原样抛出崩流。"""
    tool = _wrap(_hang)
    settings.mcp_tool_call_timeout_sec = 1  # 秒级，测试不拖时长
    try:
        with pytest.raises(ToolInvocationError) as ei:
            asyncio.run(tool.ainvoke({"x": 1}))
        msg = str(ei.value)
        assert "1s" in msg, "错误信息必须带上超时时值，模型才能判断等多久：%s" % msg[:200]
        assert "再调一次" in msg or "重试" in msg, (
            "超时信息必须引导模型重试/续查（submit-and-poll 工具的恢复路径）：%s" % msg[:200]
        )
        assert tool._inner.cancelled >= 1, (
            "wait_for 超时必须取消内层协程，否则挂死的调用仍在后台占资源"
        )
    finally:
        settings.mcp_tool_call_timeout_sec = 600


def test_fast_call_passes_through_unaffected():
    """正常返回不得被超时逻辑改写（值原样透传）。"""
    tool = _wrap(_fast)
    r = asyncio.run(tool.ainvoke({"x": 1}))
    assert r == {"ok": True}


def test_inner_exception_still_shielded():
    """内层普通异常仍走原有 shield 通道（ToolInvocationError），
    超时包装不得吞掉或改变非超时错误的语义。"""
    tool = _wrap(_boom)
    with pytest.raises(ToolInvocationError) as ei:
        asyncio.run(tool.ainvoke({"x": 1}))
    assert "inner exploded" in str(ei.value)


async def _slow():
    await asyncio.sleep(2)
    return {"late": True}


def test_timeout_fires_for_slow_but_finite_call():
    """慢而不挂的调用同样在到点被掐（负向验证的敏感体：无 wait_for 时
    本测试会 2s 后正常返回 ⇒ pytest.raises 失败，不会挂死）。"""
    tool = _wrap(_slow)
    old = settings.mcp_tool_call_timeout_sec
    settings.mcp_tool_call_timeout_sec = 1
    try:
        with pytest.raises(ToolInvocationError):
            asyncio.run(tool.ainvoke({"x": 1}))
    finally:
        settings.mcp_tool_call_timeout_sec = old


def test_timeout_disabled_when_non_positive():
    """配置 <=0 时不过超时逻辑（排障 escape hatch），挂死调用不被秒掐。"""
    tool = _wrap(_hang)
    old = settings.mcp_tool_call_timeout_sec
    settings.mcp_tool_call_timeout_sec = 0

    async def _probe():
        task = asyncio.ensure_future(tool.ainvoke({"x": 1}))
        await asyncio.sleep(0.1)
        alive = not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return alive

    try:
        assert asyncio.run(_probe()) is True, "超时关闭时调用不得被秒掐"
    finally:
        settings.mcp_tool_call_timeout_sec = old


def test_config_default_is_two_times_client_read_timeout():
    """默认 600s 的结构性理由：≥2× MCP 客户端 httpx 300s read 超时，
    不误杀 ocr_pdf_to_text（wait_seconds≤300 + 上传预处理）这类合法长调用。"""
    from src.core.config import Settings

    default = Settings.model_fields["mcp_tool_call_timeout_sec"].default
    assert default == 600, (
        "默认值漂移——600 = 2×300s 客户端 read 超时的推导失效，需重新评估"
    )
