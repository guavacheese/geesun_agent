"""spike v2: _MCPErrorShieldTool 走 langgraph 原生错误通道验证（2026-09-09）。

v1 教训：shield 直接返回错误 str，实测 langgraph 新版
_normalize_tool_response（tool_node.py:1432-1454）只接受 Command /
ToolMessage，裸 str 抛 TypeError 照样崩流：
  TypeError: Tool paddleocr_vl returned unexpected type: <class 'str'>
（server.log 16:19:18 实证，错误路径 1116 → 1453 → 崩）

v2 方案：捕获一切异常 → raise ToolInvocationError —— langgraph
ToolNode._execute_tool_async 的 except 分支捕获后经 _default_handle_tool_errors
（tool_node.py:383-393，唯一放行类型）自动转 ToolMessage(status="error")
回灌模型自纠，不崩流。

本 spike 因 mcp.py import langchain/langgraph（Windows python 无此依赖），
用最小 stub 复刻 langgraph 侧语义（_default_handle_tool_errors 等价逻辑 +
_execute_tool_async except 分支等价逻辑）+ 复刻 mcp.py 的 _shield_error/
shield except 结构（与 mcp.py 实现逐字一致），断言错误通道行为正确。
langgraph 真实行为以源码引用为准，端到端由重启后端复测确认。
"""

import asyncio

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


# ── langgraph 侧最小 stub（语义与 tool_node.py:383-393,1149-1156 一致）──
class ToolInvocationError(Exception):
    """stub：与 langgraph ToolInvocationError 等价的 message 模板。"""

    def __init__(self, tool_name, source, tool_kwargs):
        self.tool_name = tool_name
        self.source = source
        self.tool_kwargs = tool_kwargs
        super().__init__(
            f"Error invoking tool '{tool_name}' with kwargs {tool_kwargs} with error:\n"
            f" {source}\n"
            f" Please fix the error and try again."
        )


def langgraph_default_handler(e: Exception) -> str:
    """stub：langgraph _default_handle_tool_errors（tool_node.py:383-393）。"""
    if isinstance(e, ToolInvocationError):
        return str(e)
    raise e


# ── 复刻 mcp.py（与 src/core/mcp.py 逐字一致）─────────────────────────
def _first_exception(exc: BaseException) -> BaseException | None:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _shield_error(tool_name, exc, tool_kwargs) -> ToolInvocationError:
    inner = _first_exception(exc) or exc
    kw = {}
    if isinstance(tool_kwargs, dict):
        for k, v in tool_kwargs.items():
            s = str(v)
            kw[k] = s[:200] + "…(截断)" if len(s) > 200 else v
    return ToolInvocationError(tool_name=tool_name, source=inner, tool_kwargs=kw)


async def shielded_ainvoke(inner, input_):
    """复刻 _MCPErrorShieldTool.ainvoke 的 try-except-raise 结构。"""
    try:
        return await inner(input_)
    except Exception as e:  # noqa: BLE001
        raise _shield_error("paddleocr_vl", e, input_ if isinstance(input_, dict) else {}) from e


async def langgraph_execute(inner, input_):
    """复刻 _execute_tool_async except 分支（tool_node.py:1124-1156）：捕获
    异常 → 默认 handler → ToolInvocationError 转错误文本，其余 raise。"""
    try:
        return await shielded_ainvoke(inner, input_)
    except Exception as e:  # noqa: BLE001
        try:
            content = langgraph_default_handler(e)
            return ("ToolMessage(status=error)", content)
        except Exception:
            raise


# ── 用例 ────────────────────────────────────────────────────────────────
async def main():
    print("=== S1: 成功路径原样返回（shield 不干预）===")
    async def ok(_): return "识别结果: 立项审批表"
    r = await shielded_ainvoke(ok, {"input_data": "/x.pdf"})
    check("S1 成功返回不包装", r == "识别结果: 立项审批表", f"r={r!r}")

    print("=== S2: MCP 失败 → 抛 ToolInvocationError 而非裸异常 ===")
    async def mcp_err(_): raise RuntimeError("Error calling tool 'paddleocr_vl'")
    try:
        await shielded_ainvoke(mcp_err, {"input_data": "/home/user/a.pdf"})
        check("S2 raise 了 ToolInvocationError", False, "未抛异常")
    except ToolInvocationError as e:
        check("S2 类型正确", True)
        check("S2 message 含工具名", "paddleocr_vl" in str(e), str(e)[:120])
        check("S2 message 含根因", "Error calling tool" in str(e), str(e)[:120])
    except Exception as e:
        check("S2 raise 了 ToolInvocationError", False, f"抛了 {type(e).__name__}")

    print("=== S3: langgraph 原生通道——ToolInvocationError 转错误文本不崩 ===")
    async def mcp_err2(_): raise RuntimeError("isError from server")
    status, content = await langgraph_execute(mcp_err2, {"input_data": "/x"})
    check("S3 返回 error ToolMessage 语义", status == "ToolMessage(status=error)", status)
    check("S3 content 含根因", "isError from server" in content, content[:150])

    print("=== S4: 非 ToolInvocationError（未包装直抛）→ langgraph 仍 raise（对照）===")
    async def raw_raise(_): raise ConnectionError("connect refused")
    try:
        async def naked():
            try:
                return await raw_raise({})
            except Exception as e:
                langgraph_default_handler(e)  # 非 ToolInvocationError → raise
        await naked()
        check("S4 未包装异常会崩（现状对照）", False, "未抛异常")
    except ConnectionError:
        check("S4 未包装异常会崩（现状对照）", True)

    print("=== S5: ExceptionGroup 展开 → message 含最内层根因 ===")
    inner_exc = ConnectionError("Connect call failed ('192.168.10.136', 5081)")
    eg = BaseExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [inner_exc])
    async def grp(_): raise eg
    try:
        await shielded_ainvoke(grp, {})
        check("S5 抛 ToolInvocationError", False, "未抛")
    except ToolInvocationError as e:
        msg = str(e)
        tail = msg.split("error:")[1][:120] if "error:" in msg else msg
        check("S5 message 无 TaskGroup 外壳", "TaskGroup (1 sub-exception)" not in tail, msg[:150])
        check("S5 message 含最内层根因", "192.168.10.136" in msg and "Connect call failed" in msg, msg[:150])

    print("=== S6: kwargs 超长截断防错误消息撑爆上下文 ===")
    async def bigkw(_): raise ValueError("bad input")
    try:
        await shielded_ainvoke(bigkw, {"data": "B" * 5000})
        check("S6 抛 ToolInvocationError", False, "未抛")
    except ToolInvocationError as e:
        kw_str = str(e.tool_kwargs)
        check("S6 kwargs 已截断", "…(截断)" in kw_str and len(kw_str) < 600, f"len={len(kw_str)}")

    print(f"\n结果: {PASS} PASS / {FAIL} FAIL")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
