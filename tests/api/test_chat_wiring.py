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

    ④ 2026-10-12（P1-b）：run 从 SSE 生成器里解耦出来之后，收尾动作整体搬进
       `_finish_run`，兜底分支的判据由 `_saved` 变为 `_finalized`（拆成
       `_persisted` / `_finalized` 两位，见 `_disconnect_branch` 的说明）。
       ③ 的两条断言（无 await / 必须走 detach）在迁移后**原样保留** —— 它们守的是
       同一个事故形态，与收尾被搬去哪一层无关。新增的两条守的是 P1-b 才有的顺序
       不变量：release 让位于持久化、落终态早于 `[DONE]`。
       SSE 帧格式与三个新端点的契约断言在 tests/api/test_run_endpoints.py。

    ⑤ 2026-10-12（P1-c D5）：setup 阶段预写本轮 user 消息。三条断言守的是**顺序与
       承重点**，都不是"看一眼代码像对的"：
         · 预写必须发生在 `turn_registry.acquire` **之后** —— 删会话端点的护栏查的
           就是这张表，先写库再登记会给删除请求留一个"删完之后又被写回来"的窗口；
         · 预写必须发生在 `run_registry.create` **之前** —— 先让数据可见、再让 run
           可见，否则刷新撞在中间会看到"有 run 在跑、但本轮提问不存在"；
         · 喂给图的 `HumanMessage` 必须显式带 `id=setup_user_message_id`。id 对不上
           时水印会退化成 by_length，压缩场景下**整轮 AI 回答静默丢失**
           （语义级证据在 tests/api/test_setup_user_message.py 的正/负向对照）。

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
    """定位断连兜底保存分支：``if not _finalized:``。

    P1-b 前这里是 ``if not _saved:``。改名不是修辞：``_saved`` 只有一个语义
    （"存过没有"），而迁移后收尾被拆成两段 ——  ``_persisted``（已落库）与
    ``_finalized``（终态 + publish_end 都已落地）。兜底分支要判的是后者：
    取消可能正好落在"已落库但还没落终态"之间，那种情况**不该**重复写库，
    但**必须**补完终态与 [DONE]，否则 run 行永远停在活跃态。
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)
            and test.operand.id == "_finalized"
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
        "找不到断连兜底保存分支（`if not _finalized:`）—— 结构已变，本组断言失效，"
        "请同步更新"
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


def _call_lines(node: ast.AST | None, attr: str) -> list[int]:
    """收集子树里所有 ``<anything>.<attr>(...)`` 调用的行号。"""
    if node is None:
        return []
    return [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == attr
    ]


def _finish_run_releases(tree: ast.Module) -> list[int]:
    """``_finish_run`` 体内对 ``turn_registry.release`` 的调用行号。"""
    return _call_lines(_func_def(tree, "_finish_run"), "release")


def _finally_calls(tree: ast.Module, func_name: str, callee: str) -> bool:
    """``<func_name>`` 的某个 try 的 **finally** 体里是否调用了 ``callee(...)``。"""
    func = _func_def(tree, func_name)
    if func is None:
        return False
    return any(
        isinstance(n, ast.Try)
        and any(
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == callee
            for stmt in n.finalbody
            for sub in ast.walk(stmt)
        )
        for n in ast.walk(func)
    )


def test_release_follows_persist_in_finish_run():
    """``turn_registry.release`` 必须让位于持久化（P1-b 后由 ``_finish_run`` 承载）。

    不变量（2026-10-10）：release 一定发生在写台账**之后** —— 否则用户此刻提交删除
    请求，可能趁持久化过程中插进来，把刚写的台账又删掉、留下孤儿行。

    P1-b 前它由「外层 event_stream 的 finally 里 ``if _detached_cleanup['task'] is
    None: release(...)`` 守卫」表达；P1-b 把收尾整体搬进 ``_finish_run`` 之后，改由
    **结构**表达：

      ① release 是 ``_finish_run`` 体内的调用；
      ② ``_finish_run`` 只被 ``_persist_session_and_release`` 的 **finally** 调用。

    ②的"晚于"是 Python 语言保证（finally 必在 try 体之后执行），所以两条同时成立
    即「release 晚于持久化」在**所有**路径（正常收尾 / 中断后的 detach 收尾）上都
    成立 —— 不依赖某一个调用点写得对。
    """
    tree = _tree()

    assert _finish_run_releases(tree), (
        "`_finish_run` 里没有 turn_registry.release(...) —— 删除护栏会提前放行"
    )
    assert _finally_calls(tree, "_persist_session_and_release", "_finish_run"), (
        "`_persist_session_and_release` 没有在 finally 里调 `_finish_run` —— "
        "收尾可能早于持久化执行（中断路径下甚至压根不执行）"
    )


def test_finish_run_orders_terminal_state_before_done():
    """落终态必须早于 ``publish_end``（= 前端看到的 ``data: [DONE]``）。

    顺序有语义：进程若在两者之间死掉，store 里会留下一条**活跃**的行；同一次 boot
    内 boot_id 相同，惰性孤儿校正兜不住它 —— 前端就会永远显示「任务进行中」。
    反过来（先 [DONE] 后落态）则会让前端在"已结束"之后仍查得到一个活跃 run。
    """
    finish = _func_def(_tree(), "_finish_run")
    assert finish is not None, "找不到 `_finish_run`"
    marks = _call_lines(finish, "mark")
    ends = _call_lines(finish, "publish_end")
    assert marks and ends, (
        f"`_finish_run` 缺少 mark / publish_end 调用（mark={marks}, publish_end={ends}）"
    )
    assert min(marks) < min(ends), (
        f"publish_end 排在落终态之前（mark={marks}, publish_end={ends}）—— "
        "进程若在两者之间死掉，会留下永远显示「进行中」的僵尸 run"
    )


class _ReleaseDropper(ast.NodeTransformer):
    """负向对照用：把 ``_finish_run`` 里的 release 摘掉（模拟"收尾忘了放行删除护栏"）。"""

    def visit_AsyncFunctionDef(self, node):
        if node.name == "_finish_run":
            node.body = [
                n for n in node.body
                if not (
                    isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Call)
                    and isinstance(n.value.func, ast.Attribute)
                    and n.value.func.attr == "release"
                )
            ]
        return self.generic_visit(node)


def test_release_detector_is_sensitive():
    """负向对照：摘掉 release 后检测器必须报空 —— 否则本组断言等于空测。"""
    assert _finish_run_releases(_tree())  # 当前实现必须通过

    stripped = _ReleaseDropper().visit(ast.parse(CHAT_PY.read_text(encoding="utf-8")))
    ast.fix_missing_locations(stripped)
    assert not _finish_run_releases(stripped), (
        "摘掉 release 后检测器仍然报有 —— 该断言无法捕获「收尾忘了放行删除护栏」的回归"
    )


class _ReAwaitInjector(ast.NodeTransformer):
    """负向对照用：把 detach 换回裸 await（模拟被重构改回去）。"""

    def visit_If(self, node: ast.If):
        test = node.test
        if (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)
            and test.operand.id == "_finalized"
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


# ─── ⑤ P1-c D5：setup 预写本轮 user 消息 ─────────────────────────────────────


def _callee_name(node: ast.Call) -> str | None:
    """调用的可读名：``f(...)`` → ``"f"``，``o.m(...)`` → ``"o.m"``，其余 None。"""
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f"{f.value.id}.{f.attr}"
    return None


class _CallsInScope(ast.NodeVisitor):
    """收集「当前函数体」里的调用，**不下钻嵌套函数/类**。

    必须排除嵌套：``chat()`` 里嵌着 ``_run_worker`` / ``_finish_run`` / ``_persist_session``
    一整套闭包，而 ``_run_worker`` 自己也调 ``turn_registry.acquire``。若把它们算进来，
    "顺序正确"会被那个更早出现的同名调用**蒙对** —— 检测器就失去意义了（P1-b 之前
    acquire 确实只在 worker 里，正是这条断言要防的退化）。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """不下钻（否则会收进嵌套闭包的调用）。"""

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """不下钻。"""

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """不下钻。"""

    def visit_Call(self, node: ast.Call) -> None:
        name = _callee_name(node)
        if name is not None:
            self.calls.append((node.lineno, name))
        self.generic_visit(node)


def _chat_endpoint_calls(tree: ast.Module) -> list[tuple[int, str]]:
    """``chat`` 端点**自身**函数体里的调用，按行号排序（不含嵌套闭包）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat":
            v = _CallsInScope()
            for stmt in node.body:
                v.visit(stmt)
            return sorted(v.calls)
    return []


def _index_of(calls: list[tuple[int, str]], name: str) -> int | None:
    for i, (_line, n) in enumerate(calls):
        if n == name:
            return i
    return None


def _acquire_precedes_prewrite(tree: ast.Module) -> bool:
    calls = _chat_endpoint_calls(tree)
    i_acq = _index_of(calls, "turn_registry.acquire")
    i_pre = _index_of(calls, "_write_setup_user_message")
    return i_acq is not None and i_pre is not None and i_acq < i_pre


def _prewrite_precedes_run_create(tree: ast.Module) -> bool:
    calls = _chat_endpoint_calls(tree)
    i_pre = _index_of(calls, "_write_setup_user_message")
    i_run = _index_of(calls, "run_registry.create")
    return i_pre is not None and i_run is not None and i_pre < i_run


def test_setup_prewrite_registers_turn_before_writing():
    """预写必须晚于台账登记。

    删会话端点的护栏（``turn_registry.is_active``）查的就是这张表。反过来先写库再登记，
    删除请求可以抢在预写之前完成，我们随后就把消息写进一个**已被删除**的会话 —— 而
    ``session_existed_at_start`` 守卫只能拦住"流尾再写"，拦不住 setup 这一次。
    """
    assert _acquire_precedes_prewrite(_tree()), (
        "chat 端点的 setup 结构里找不到「先 turn_registry.acquire 再 _write_setup_user_message」"
        "的顺序 —— 要么预写没接上，要么登记被挪到了预写之后（删除护栏出现窗口）"
    )


def test_setup_prewrite_precedes_run_registration():
    """预写必须先于 run 登记：先让数据可见，再让 run 可见。

    反过来的话，刷新恰好撞在两者之间会拿到「有 run 在跑、但本轮提问还不存在」的组合 ——
    前端会订阅到一个没有落点的 run（正文会拼到上一轮的 AI 上）。
    """
    assert _prewrite_precedes_run_create(_tree()), (
        "chat 端点里 _write_setup_user_message 没有排在 run_registry.create 之前"
    )


def test_graph_input_carries_explicit_message_id():
    """喂给图的 HumanMessage 必须显式带 ``id=setup_user_message_id``。

    这是 D5 的承重点：预写把水位的 ``last_stored_id`` 换成了"刚写的这条"，它必须能被
    ``_plan_message_append`` 在 checkpoint 的 history 里认出来；认不出来就退化成
    by_length，而压缩会把 history 变短 ⇒ 判"无新增" ⇒ 本轮 AI 回答静默丢失。
    实测依据与正/负向对照见 tests/api/test_setup_user_message.py。
    """
    found = False
    for node in ast.walk(_tree()):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "HumanMessage":
            continue
        for kw in node.keywords:
            if (
                kw.arg == "id"
                and isinstance(kw.value, ast.Name)
                and kw.value.id == "setup_user_message_id"
            ):
                found = True
    assert found, (
        "graph_input 的 HumanMessage 没有带 id=setup_user_message_id —— "
        "setup 预写的条目在流尾定位不到，压缩场景下本轮 AI 回答会静默丢失"
    )


class _AcquireDropper(ast.NodeTransformer):
    """负向对照用：摘掉**函数体顶层**的 ``turn_registry.acquire`` 语句。"""

    @staticmethod
    def _drop(node):
        node.body = [
            n for n in node.body
            if not (
                isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Attribute)
                and n.value.func.attr == "acquire"
            )
        ]
        return node

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        return self._drop(node)

    def visit_AsyncFunctionDef(self, node):
        self.generic_visit(node)
        return self._drop(node)


def test_acquire_order_detector_is_sensitive():
    """负向对照：摘掉 acquire 后检测器必须报 False —— 否则本组断言等于空测。"""
    assert _acquire_precedes_prewrite(_tree())  # 当前实现必须通过

    stripped = _AcquireDropper().visit(ast.parse(CHAT_PY.read_text(encoding="utf-8")))
    ast.fix_missing_locations(stripped)
    assert not _acquire_precedes_prewrite(stripped), (
        "摘掉 acquire 后检测器仍报「顺序正确」—— 该断言无法捕获删除护栏窗口的回归"
    )


class _IdKwargDropper(ast.NodeTransformer):
    """负向对照用：把 HumanMessage 的 ``id=`` 关键字参数摘掉（模拟改回 dict 形式）。"""

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "HumanMessage":
            node.keywords = [kw for kw in node.keywords if kw.arg != "id"]
        return self.generic_visit(node)


def test_message_id_detector_is_sensitive():
    """负向对照：去掉 ``id=`` 后检测器必须报缺失，否则本组断言等于空测。"""
    def _has_id_kwarg(tree: ast.Module) -> bool:
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id == "HumanMessage" and any(kw.arg == "id" for kw in node.keywords):
                return True
        return False

    assert _has_id_kwarg(_tree())

    stripped = _IdKwargDropper().visit(ast.parse(CHAT_PY.read_text(encoding="utf-8")))
    ast.fix_missing_locations(stripped)
    assert not _has_id_kwarg(stripped), "摘掉 id= 后检测器仍然报有 —— 本组断言等于空测"


# ─────────────────────────────────────────────────────────────────────────────
# 收尾标志必须无条件置位（★ 1.0.27 线上事故的回归锁，2026-10-12）
#
# 事故经过：`_worker_finalize_started["started"] = True` 原本写在
# `if not _finalized:` **内部**。而正常完成路径会设 `_finalized = True`
# （紧跟 `await _finish_run(run_id, STATUS_COMPLETED)`），于是整块被跳过、
# 标志永远是 False ⇒ `_on_worker_done` 一路走到兜底，兜底的 status 判据是
# `INTERRUPTED if task.cancelled() else FAILED`，正常完成的轮次落进 else 分支，
# 用 `_finish_run(FAILED)` 把刚落好的 COMPLETED 覆盖成 failed（`_finish_run`
# 刻意不幂等 —— 它无条件 `run_registry.mark`）。
#
# 线上证据（1.0.27 生产日志）：**正常读完 [DONE] 的一轮**（不断连、正文产出正常）
#   [RUN] 本轮已启动（脱离连接）: run=999437635bf54eb0 …
#   [RUN] worker 未进收尾机制，执行兜底收尾（本轮尚无产出需落库）: status=failed
# 且该 run 无「worker 异常退出」也无「worker 被取消」两条日志中的任何一条 ⇒
# worker 是正常返回的。断连场景（cf0cc926…）同样复现。
#
# 为什么必须用 AST 而不是行为测试：`_run_worker` / `_on_worker_done` 都是 `chat()`
# 端点内的闭包，要行为级复现就得把整个端点连同 store / bridge / checkpointer 一起
# 拉起来。而本缺陷是纯粹的**结构**问题（置位在不在 if 里），AST 断言恰好精确且廉价。
# ─────────────────────────────────────────────────────────────────────────────


def _is_finalize_flag_assign(stmt: ast.stmt) -> bool:
    """``_worker_finalize_started["started"] = True`` 这一条赋值语句。"""
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return False
    t = stmt.targets[0]
    return (
        isinstance(t, ast.Subscript)
        and isinstance(t.value, ast.Name)
        and t.value.id == "_worker_finalize_started"
        and isinstance(t.slice, ast.Constant)
        and t.slice.value == "started"
    )


def _is_not_finalized_test(test: ast.expr) -> bool:
    """``not _finalized``（``if not _finalized:`` 的判据）。"""
    return (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Name)
        and test.operand.id == "_finalized"
    )


def _finalize_flag_layout(tree: ast.Module):
    """在 ``_run_worker`` 的 finally 里定位两个语句的下标。

    返回 ``(idx_flag, idx_if)``：
      · ``idx_flag`` —— 置位语句在 ``finalbody`` 中的下标；``None`` 表示它
        **不是** finally 的直接子节点（即被包在某个 if 里，或压根没有）。
      · ``idx_if`` —— ``if not _finalized:`` 在 ``finalbody`` 中的下标。
    找不到 ``_run_worker`` 或无 finally 时返回 ``None``。
    """
    fn = _func_def(tree, "_run_worker")
    if fn is None:
        return None
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Try) and node.finalbody):
            continue
        idx_flag = idx_if = None
        for i, stmt in enumerate(node.finalbody):
            if _is_finalize_flag_assign(stmt):
                idx_flag = i
            if isinstance(stmt, ast.If) and _is_not_finalized_test(stmt.test):
                idx_if = i
        if idx_flag is not None or idx_if is not None:
            return (idx_flag, idx_if)
    return None


def test_worker_finalize_flag_is_set_unconditionally():
    """★ 正常完成路径也必须置位收尾标志（否则 done-callback 覆盖终态）。"""
    layout = _finalize_flag_layout(_tree())
    assert layout is not None, "未在 `_run_worker` 的 finally 里找到收尾标志 / `if not _finalized:`"
    idx_flag, idx_if = layout
    assert idx_flag is not None, (
        "finally 里没有 `_worker_finalize_started['started'] = True` 的**无条件**赋值 —— "
        "它被放进了 `if not _finalized:` 内部，正常完成路径不会置位，"
        "done-callback 会用 `_finish_run(FAILED)` 覆盖 COMPLETED（1.0.27 线上事故原形）"
    )
    assert idx_if is None or idx_flag < idx_if, (
        "置位必须**先于** `if not _finalized:` 出现在 finally 体内（且在其外）"
    )


class _FinalizeFlagNester(ast.NodeTransformer):
    """负向对照：把置位语句包进 ``if True:`` —— 复现 1.0.27 的缺陷形态。"""

    def visit_Try(self, node: ast.Try):
        self.generic_visit(node)
        if not node.finalbody:
            return node
        new_body, nested = [], False
        for stmt in node.finalbody:
            if not nested and _is_finalize_flag_assign(stmt):
                new_body.append(ast.If(test=ast.Constant(value=True), body=[stmt], orelse=[]))
                nested = True
            else:
                new_body.append(stmt)
        node.finalbody = new_body
        return node


def test_finalize_flag_detector_is_sensitive():
    """负向对照：把置位包进 if 后检测器必须报「不是无条件」—— 否则本组断言等于空测。"""
    assert _finalize_flag_layout(_tree())[0] is not None  # 当前实现必须通过

    stripped = _FinalizeFlagNester().visit(ast.parse(CHAT_PY.read_text(encoding="utf-8")))
    ast.fix_missing_locations(stripped)
    layout = _finalize_flag_layout(stripped)
    assert layout is None or layout[0] is None, (
        "把置位包进 if 之后，检测器仍报「无条件置位」—— 该断言无法捕获本次回归"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ⑥ run 必须真的转 running（`started_at` 的唯一来源，2026-10-13）
#
# `run_registry.mark(..., STATUS_RUNNING)` 是 `started_at` 的**唯一**写入点（其实现里
# 只有 `status == running 且 started_at is None` 才写）。端点里的 `persist` 落的是
# pending，所以 chat.py 若不在 worker 进入执行段时调这一次 mark，这一列就**恒为 null**。
#
# 前端两处消费它，方向相反：
#   · `ChatArea.tsx:350`  `active.started_at ?? active.created_at` —— 有兜底，且注释
#     写着「PENDING 阶段还是 null（run_registry 只在转 RUNNING 时才写）」；
#   · `ChatArea.tsx:201`  直接 `active.started_at ?? null` —— **没有** created_at
#     兜底，刷新后恢复的提示条靠它显示「已进行 X 分钟」，不写就退化成不显示时长。
#
# 为什么用 AST：这是**接线**问题（"这一列有没有人写"）；模块级行为由
# tests/core/test_run_registry.py::test_mark_records_timestamps_once_and_is_idempotent
# 覆盖 —— 与本文件其余断言分工一致。
# ─────────────────────────────────────────────────────────────────────────────


def _is_status_running_arg(node: ast.expr) -> bool:
    """``run_registry.STATUS_RUNNING`` 这个属性访问。"""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "STATUS_RUNNING"
        and isinstance(node.value, ast.Name)
        and node.value.id == "run_registry"
    )


def _mark_running_calls(node: ast.AST | None) -> list[int]:
    """收集 ``run_registry.mark(..., run_registry.STATUS_RUNNING)`` 的行号。

    位置参数与 ``status=`` 关键字两种写法都认；状态用**常量**而非字面量 `"running"`
    （与 ② 的 `LOOP_FORCED_STOP_KEY` 同思路：常量改名时这里立刻报错，不会静默失联）。
    """
    if node is None:
        return []
    hits: list[int] = []
    for n in ast.walk(node):
        if not (isinstance(n, ast.Call) and _callee_name(n) == "run_registry.mark"):
            continue
        by_pos = any(_is_status_running_arg(a) for a in n.args)
        by_kw = any(
            kw.arg == "status" and _is_status_running_arg(kw.value) for kw in n.keywords
        )
        if by_pos or by_kw:
            hits.append(n.lineno)
    return hits


def test_worker_marks_run_running():
    """★ ``_run_worker`` 必须把 run 转 running —— 否则 ``started_at`` 恒为 null。"""
    worker = _func_def(_tree(), "_run_worker")
    assert worker is not None, "找不到 `_run_worker`"
    assert _mark_running_calls(worker), (
        "`_run_worker` 里没有 `run_registry.mark(store, run_id, run_registry.STATUS_RUNNING)` "
        "—— run 行会直跳 pending → 终态：`started_at` 恒为 null（刷新后恢复的提示条拿不到"
        "时长，ChatArea.tsx:201 那条路径没有 created_at 兜底），且 ACTIVE_STATUSES 里的 "
        "running 变成无人使用的死状态"
    )


def test_running_mark_precedes_terminal_mark():
    """转 running 必须早于任何终态写入。

    顺序反了会**覆盖终态**：``run_registry.mark`` 刻意不幂等（无条件写 status 并
    persist），一个排在 ``_finish_run`` 之后的 running 写入会把刚落好的
    completed / failed / interrupted 改回 running —— run 行永远停在活跃态，
    前端刷新后一直显示「任务进行中」。
    """
    worker = _func_def(_tree(), "_run_worker")
    assert worker is not None
    runs = _mark_running_calls(worker)
    terminals = _calls_named(worker, "_finish_run")
    assert runs and terminals, (
        f"`_run_worker` 缺少转 running（{runs}）或终态收尾（{terminals}）调用"
    )
    assert min(runs) < min(terminals), (
        f"转 running（行 {runs}）不早于 `_finish_run`（行 {terminals}）—— "
        "终态会被改回 running（mark 不幂等）"
    )


class _RunningMarkDropper(ast.NodeTransformer):
    """负向对照：摘掉转 running 的那次 mark（模拟"run 直跳终态"的退化实现）。

    它嵌在 ``try`` 体内的第一句，所以必须重写 ``visit_Try`` ——
    ``visit_AsyncFunctionDef`` 只看函数体顶层语句，摘不到它。
    """

    @staticmethod
    def _is_running_mark_stmt(stmt: ast.stmt) -> bool:
        return isinstance(stmt, ast.Expr) and bool(_mark_running_calls(stmt))

    def visit_Try(self, node: ast.Try):
        self.generic_visit(node)
        node.body = [n for n in node.body if not self._is_running_mark_stmt(n)]
        return node


def test_running_mark_detector_is_sensitive():
    """负向对照：摘掉转 running 后检测器必须报空 —— 否则本组断言等于空测。"""
    assert _mark_running_calls(_func_def(_tree(), "_run_worker"))  # 当前实现必须通过

    stripped = _RunningMarkDropper().visit(ast.parse(CHAT_PY.read_text(encoding="utf-8")))
    ast.fix_missing_locations(stripped)
    assert not _mark_running_calls(_func_def(stripped, "_run_worker")), (
        "摘掉转 running 的 mark 后检测器仍然报有 —— 该断言无法捕获「run 直跳终态」的退化"
    )
