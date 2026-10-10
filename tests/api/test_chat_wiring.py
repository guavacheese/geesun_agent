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
       注：本文件只断言**接线**；标记能不能真的从 updates 流出来，由
       tests/core/test_loop_detection_integration.py 用真实图验证。
    ③ 2026-10-10：断连兜底保存必须**脱离 SSE 取消作用域**（_spawn_detached_save），
       且失败/取消必须可见。会话 4863afff 事故：客户端断连后 store 里 0 行，服务端
       打完 `[DIAG] SSE 流中断` 后一条日志都没有 —— 因为 finally 里的
       `await _persist_session(...)` 在第一个 await 点抛 CancelledError，
       而 `except Exception` 抓不到 BaseException 子类，静默跳过。
       机制实测见 tests/spikes/sse_disconnect_repro.py（真 uvicorn + 真 RST 断开）。

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


# ─── ② 硬停止损：loop_forced_stop 必须被真正消费 ────────────────────────────

def _imports_from_chat(tree: ast.Module, module: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names |= {a.name for a in node.names}
    return names


def _has_forced_stop_break(tree: ast.Module) -> bool:
    """存在 ``if _loop_forced_stop is not None: ... break``（本轮真终止）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "_loop_forced_stop"
            and any(isinstance(op, ast.IsNot) for op in test.ops)
        ):
            continue
        if any(isinstance(n, ast.Break) for n in ast.walk(node)):
            return True
    return False


def test_loop_forced_stop_is_consumed():
    """chat.py 必须：导入常量 → 从 updates 捕获 → 命中即终止本轮。

    这三段缺任何一段，"硬停"就退化成"只是把 tool_calls 剥空"：
    M3 完成门随后按零产出注入自动继续轮，把循环重新推起来（a2719d7b 事故）。
    """
    tree = _tree()
    src = CHAT_PY.read_text(encoding="utf-8")

    # ① 用常量而非字面量（键改名时这里立刻报错，不会静默失联）
    assert "LOOP_FORCED_STOP_KEY" in _imports_from_chat(tree, "src.core.loop_detection"), (
        "chat.py 未从 src.core.loop_detection 导入 LOOP_FORCED_STOP_KEY"
    )
    # ② 捕获点：从 updates 的节点输出里取该键
    captured = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get"
        and n.args
        and isinstance(n.args[0], ast.Name)
        and n.args[0].id == "LOOP_FORCED_STOP_KEY"
        for n in ast.walk(tree)
    )
    assert captured, "chat.py 没有从 updates 事件里读取 loop_forced_stop（信号无人消费）"
    # ③ 终止点：命中即 break，不再走完成门自动继续轮
    assert _has_forced_stop_break(tree), (
        "chat.py 缺少 `if _loop_forced_stop is not None: ... break` —— "
        "硬停后仍会被 M3 完成门重新推回循环"
    )
    # ④ 用户可见：发一条 completion_blocked（code 用同一常量，前端通用渲染）
    assert "'type': 'completion_blocked'" in src.replace('"', "'")
    assert src.count("LOOP_FORCED_STOP_KEY") >= 3, (
        "LOOP_FORCED_STOP_KEY 使用点少于 3 处（导入/捕获/事件），接线可能不完整"
    )


class _BreakStripper(ast.NodeTransformer):
    """负向对照用：把硬停分支里的 break 摘掉（模拟"只记日志不终止"的退化实现）。"""

    def visit_If(self, node: ast.If):
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "_loop_forced_stop"
        ):
            node.body = [n for n in node.body if not isinstance(n, ast.Break)]
        return self.generic_visit(node)


def test_break_detector_is_sensitive():
    """负向对照：摘掉 break 后检测器必须报 False。

    存在的意义：证明 ``_has_forced_stop_break`` 检查的是**终止动作本身**，
    而不是"文件里出现过 _loop_forced_stop 字样"就算过 —— 否则实现退化成
    "只打一行 ERROR 日志"时测试依然全绿，等于空测。
    """
    tree = ast.parse(CHAT_PY.read_text(encoding="utf-8"), filename=str(CHAT_PY))
    assert _has_forced_stop_break(tree) is True  # 当前实现必须通过

    stripped = _BreakStripper().visit(ast.parse(CHAT_PY.read_text(encoding="utf-8")))
    ast.fix_missing_locations(stripped)
    assert _has_forced_stop_break(stripped) is False, (
        "摘掉 break 后检测器仍然报 True —— 该断言无法捕获「硬停不成终止」的回归"
    )


# ─── ③ 断连兜底保存必须脱离取消作用域 ───────────────────────────────────────

def _disconnect_branch(tree: ast.Module) -> ast.If | None:
    """定位断连兜底保存分支：``if not _saved:``。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)
            and test.operand.id == "_saved"
        ):
            return node
    return None


def _awaits_in(node: ast.AST) -> list[int]:
    """收集子树里所有 await 的行号。"""
    return [n.lineno for n in ast.walk(node) if isinstance(n, ast.Await)]


def _calls_named(node: ast.AST, name: str) -> list[int]:
    """收集子树里所有以 <name>(...) 形式调用的行号。"""
    return [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def _func_def(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _handlers_catching(tree: ast.Module, func_name: str, exc_attr: str) -> bool:
    """函数体内是否存在 ``except asyncio.<exc_attr>`` 分支。"""
    func = _func_def(tree, func_name)
    if func is None:
        return False
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            t = handler.type
            if (
                isinstance(t, ast.Attribute)
                and t.attr == exc_attr
                and isinstance(t.value, ast.Name)
                and t.value.id == "asyncio"
            ):
                return True
    return False


def test_disconnect_persist_has_no_await():
    """断连兜底分支内**不得有任何 await**。

    断连时本 task 已处于取消中，第一个 await 即抛 CancelledError —— 保存整段蒸发
    （4863afff 事故：store 0 行 + 两条日志都没有）。
    """
    branch = _disconnect_branch(_tree())
    assert branch is not None, (
        "找不到断连兜底保存分支（`if not _saved:`）—— 结构已变，本组断言失效，请同步更新"
    )
    awaited = _awaits_in(branch)
    assert not awaited, (
        f"断连兜底保存分支内仍有 await（行 {awaited}）。断连时 task 已在取消中，"
        "await 会在第一个 await 点抛 CancelledError 导致保存静默丢失；"
        "必须改走 _spawn_detached_save 脱离取消作用域。"
    )


def test_disconnect_persist_is_detached():
    """断连兜底保存必须经 ``_spawn_detached_save`` 承载。"""
    branch = _disconnect_branch(_tree())
    assert branch is not None
    spawns = _calls_named(branch, "_spawn_detached_save")
    assert spawns, (
        "断连兜底保存没有走 _spawn_detached_save —— 保存协程仍在取消作用域内"
    )


def test_detached_save_registers_result_callback():
    """``_spawn_detached_save`` 必须注册结果回调，否则失败又变静默。"""
    func = _func_def(_tree(), "_spawn_detached_save")
    assert func is not None, "缺少 _spawn_detached_save 定义"
    assert _func_def(_tree(), "_on_detached_save_done") is not None, (
        "缺少 _on_detached_save_done（结果侧日志的唯一出口）"
    )
    registered = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "add_done_callback"
        and any(
            isinstance(a, ast.Name) and a.id == "_on_detached_save_done"
            for a in n.args
        )
        for n in ast.walk(func)
    )
    assert registered, (
        "_spawn_detached_save 未把 _on_detached_save_done 注册为 done_callback —— "
        "后台保存失败将再次静默（正是本次事故的形态）"
    )


def test_persist_session_logs_cancellation():
    """``_persist_session`` 必须显式捕获 CancelledError。

    CancelledError 继承 BaseException，只有 ``except Exception`` 时抓不到它 ——
    这就是"保存被取消完全静默"的直接原因。
    """
    assert _handlers_catching(_tree(), "_persist_session", "CancelledError"), (
        "_persist_session 没有 `except asyncio.CancelledError` 分支：保存被取消时"
        "不会留下任何日志（4863afff 现场既无「保存完成」也无「保存失败」）。"
    )


def test_release_waits_for_detached_save():
    """``turn_registry.release`` 必须让位于 detach 出来的保存任务。

    既有不变量是「release 一定发生在写台账之后」，否则删除请求可能趁持久化过程中
    插进来。保存被 detach 后若仍在外层 finally 无条件 release，这个不变量就破了。
    """
    tree = _tree()
    src = CHAT_PY.read_text(encoding="utf-8")
    guarded = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Subscript)
        and any(isinstance(op, ast.Is) for op in node.test.ops)
        and any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "release"
            for n in ast.walk(node)
        )
        for node in ast.walk(tree)
    )
    assert guarded, (
        "外层 event_stream 的 finally 里缺少 `if _detached_cleanup['task'] is None: "
        "turn_registry.release(...)` —— 断连路径下 release 会先于写台账执行"
    )
    assert "_persist_session_and_release" in src, (
        "缺少 _persist_session_and_release：release 没有与保存绑在同一个后台任务里"
    )


class _ReAwaitInjector(ast.NodeTransformer):
    """负向对照用：把 detach 换回裸 await（模拟被重构改回去）。"""

    def visit_If(self, node: ast.If):
        test = node.test
        if (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)
            and test.operand.id == "_saved"
        ):
            await_expr = ast.Expr(
                value=ast.Await(
                    value=ast.Call(
                        func=ast.Name(id="_persist_session", ctx=ast.Load()),
                        args=[],
                        keywords=[],
                    )
                )
            )
            node.body = [await_expr, *node.body]
        return self.generic_visit(node)


def test_disconnect_detached_detector_is_sensitive():
    """负向对照：塞回裸 await 后检测器必须报出来（否则本组断言等于空测）。"""
    branch = _disconnect_branch(_tree())
    assert branch is not None and not _awaits_in(branch)  # 当前实现：无 await

    polluted = _ReAwaitInjector().visit(ast.parse(CHAT_PY.read_text(encoding="utf-8")))
    ast.fix_missing_locations(polluted)
    assert _awaits_in(_disconnect_branch(polluted)), (
        "注入 await 后检测器仍然报「无 await」—— 该断言无法捕获「保存改回裸 await」的回归"
    )
