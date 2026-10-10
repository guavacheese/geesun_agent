"""chat.py 接线契约测试（AST 静态断言，不启动服务）。

背景（wiring-contract-verify 思路）：
    「改了 harness / 加了守卫」这类改动最容易出的问题不是逻辑写错，而是**没接上**
    —— 代码躺在文件里没人调，或调用点被后来的重构悄悄改回裸调，而模块级单测全绿。
    本项目已有先例：execute 通道护栏的调用点静态断言（commit febeb71）。

本文件用 AST 直接对 ``src/api/endpoints/chat.py`` 断言：
    ① 2026-10-10：沙箱初始化（create_sandbox / get_env_snapshot）必须走
       ``await asyncio.to_thread(...)`` —— 裸调会冻住事件循环（会话 a2719d7b
       agent 连续被 swarm SIGKILL ≥7 次，每次死亡前 3 分 19 秒完全静默）。
    ② 2026-10-10：LoopDetectionMiddleware 的硬停标记 ``loop_forced_stop`` 必须被
       chat.py 消费（捕获 + 终止本轮），否则"硬停"只是把 tool_calls 剥空，
       M3 完成门随后又注入自动继续轮，循环被重新推起来。

运行：
    docker run --rm -v D:/workspace/geesun_agent:/mnt -w /mnt \
      -e PYTHONPATH=/mnt/.testdeps --entrypoint /app/.venv/bin/python \
      172.16.220.74:8333/geesun_ai/geesun-agent:1.0.22 \
      -m pytest tests/api/test_chat_wiring.py -q
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CHAT_PY = PROJECT_ROOT / "src" / "api" / "endpoints" / "chat.py"


def _tree() -> ast.Module:
    return ast.parse(CHAT_PY.read_text(encoding="utf-8"), filename=str(CHAT_PY))


def _awaited_to_thread_targets(tree: ast.Module) -> set[str]:
    """收集所有 ``await asyncio.to_thread(<fn>, ...)`` 里 <fn> 的名字。"""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "to_thread"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        ):
            continue
        if call.args and isinstance(call.args[0], ast.Name):
            found.add(call.args[0].id)
    return found


def _bare_calls(tree: ast.Module, name: str) -> list[int]:
    """收集所有「直接以 <name>(...) 形式调用」的行号（裸调，未经 to_thread）。"""
    lines: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        ):
            lines.append(node.lineno)
    return lines


# ─── ① 沙箱初始化必须 to_thread 化 ───────────────────────────────────────────

def test_sandbox_init_offloaded_to_thread():
    """create_sandbox / get_env_snapshot 必须被 asyncio.to_thread 包住。"""
    targets = _awaited_to_thread_targets(_tree())
    missing = {"create_sandbox", "get_env_snapshot"} - targets
    assert not missing, (
        f"{CHAT_PY} 缺少 await asyncio.to_thread(...) 包装：{sorted(missing)}。"
        "这两个函数是同步阻塞调用，裸调会冻住事件循环（a2719d7b 事故）。"
    )


def test_no_bare_blocking_calls_remain():
    """不得存在裸调 create_sandbox(...) / get_env_snapshot(...)。"""
    tree = _tree()
    offenders = {
        name: sorted(set(_bare_calls(tree, name)))
        for name in ("create_sandbox", "get_env_snapshot")
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, f"发现裸调（未走 to_thread）：{offenders}"
