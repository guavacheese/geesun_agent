"""spike: _MCPErrorShieldTool 错误防护逻辑验证（2026-09-09）。

背景：paddleocr_vl 服务端 isError → langchain_mcp_adapters raise ToolException
→ langgraph ToolNode._default_handle_tool_errors 只放行 ToolInvocationError，
其余异常一律 raise → 整轮 panic（tool_node.py:383-393 + 当日实证）。
修复（src/core/mcp.py）：通用 shield 包装捕获一切异常，归一化为友好中文
错误文本作为正常工具结果返回给模型自纠——不 re-raise、不崩流。

本 spike 因 mcp.py import langchain（Windows python 无此依赖），独立复刻
被验证逻辑（_first_exception / _mcp_error_text / shield try-except 结构），
与 mcp.py 实现保持逐字一致；mcp.py 本身另做 py_compile + 端到端复测。
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


# ── 复刻 mcp.py（逐字一致）─────────────────────────────────────────────
def _first_exception(exc: BaseException) -> BaseException | None:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _mcp_error_text(tool_name: str, exc: BaseException, max_len: int = 600) -> str:
    inner = _first_exception(exc)
    if inner is not None:
        detail = f"{type(inner).__name__}: {inner}"
    else:
        detail = f"{type(exc).__name__}: {exc}"
    if len(detail) > max_len:
        detail = detail[:max_len] + "…(已截断)"
    return (
        f"[工具 {tool_name} 调用失败] {detail}。"
        "请检查参数是否指向该服务可达的绝对路径/URL/Base64，或换一种输入方式；"
        "不要原样重试同一次调用，可改用其他工具完成同样目标。"
    )


async def shielded_ainvoke(inner, input_):
    """复刻 _MCPErrorShieldTool.ainvoke 的 try-except 结构。"""
    try:
        return await inner(input_)
    except Exception as e:  # noqa: BLE001
        return _mcp_error_text("paddleocr_vl", e)


class ToolException(Exception):
    """模拟 langchain_mcp_adapters 对服务端 isError 转出的 ToolException。"""


# ── 用例 ────────────────────────────────────────────────────────────────
async def main():
    print("=== S1: 成功路径原样返回 ===")
    async def ok(_): return "识别结果: 立项审批表"
    r = await shielded_ainvoke(ok, {"input_data": "/x.pdf"})
    check("S1 成功返回不包装", r == "识别结果: 立项审批表", f"r={r!r}")

    print("=== S2: 普通异常 → 文本含工具名/类型/消息，不 raise ===")
    async def boom(_): raise RuntimeError("connection refused: 5081")
    r = await shielded_ainvoke(boom, {})
    check("S2 含工具名", "paddleocr_vl" in r, r)
    check("S2 含异常类型", "RuntimeError" in r, r)
    check("S2 含根因消息", "connection refused" in r, r)
    check("S2 含自纠指引", "不要原样重试" in r and "改用其他工具" in r, r)

    print("=== S3: ToolException（MCP isError 语义）→ 回灌原文不崩 ===")
    async def mcp_err(_): raise ToolException("Error calling tool 'paddleocr_vl'")
    r = await shielded_ainvoke(mcp_err, {"input_data": "/home/user/a.pdf"})
    check("S3 不 raise 且含原文", "Error calling tool 'paddleocr_vl'" in r, r)
    check("S3 类型标注 ToolException", "ToolException" in r, r)

    print("=== S4: ExceptionGroup 嵌套 → 展开到最内层根因 ===")
    inner_exc = ConnectionError("Connect call failed ('192.168.10.136', 5081)")
    eg = BaseExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [inner_exc])
    async def grp(_): raise eg
    r = await shielded_ainvoke(grp, {})
    check("S4 不含 TaskGroup 外壳文本", "TaskGroup" not in r.split("。")[0], r)
    check("S4 含最内层根因", "192.168.10.136" in r and "ConnectionError" in r, r)

    print("=== S5: 超长错误截断 ≤ 600+后缀 ===")
    async def long_err(_): raise ValueError("x" * 5000)
    r = await shielded_ainvoke(long_err, {})
    check("S5 截断", len(r) < 700 and "已截断" in r, f"len={len(r)}")

    print("=== S6: 文本长度上限内完整可读 ===")
    async def normal(_): raise PermissionError("no permission")
    r = await shielded_ainvoke(normal, {})
    check("S6 可读", r.startswith("[工具 paddleocr_vl 调用失败] PermissionError: no permission"), r)

    print(f"\n结果: {PASS} PASS / {FAIL} FAIL")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
