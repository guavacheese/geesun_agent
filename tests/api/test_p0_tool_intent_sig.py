"""P0 无进展检测指纹的回归测试（会话 GY24428:4f5ede6a，2026-10-17）。

═══ 事故经过 ═══
用户上传一份 **262 页扫描件 PDF**（康视达_CST2026手册.pdf，268MB）。模型连发 10 条
**各不相同**的 pymupdf 命令排查：读总页数 → 读第 1 页 → 读第 2 页 → 搜关键词 →
统计文本长度 vs 图片数 …。排查路径完全正确，且最后一条输出已经在判断"页面是图"
（下一步就该想到 OCR），**离成功一步之遥**。

但 P0 无进展检测把它掐了。判据是：

    chat.py:713  _tool_intent_sig(tool_name, result_str)
                 execute → f"{tool_name}:{result_str[:60]}"

而每次 pymupdf 执行输出的**第一行**恒为那句 98 字符的弃用警告：

    warning: The `fitz` API is deprecated and will be removed in future. ...

98 > 60 ⇒ 整个 60 字符取样窗口被这行噪声占满，真正的差异（``总页数: 262`` /
``第2页`` / ``包含光源的页面: [2]``）全在窗口之外 ⇒ 10 条命令去重后**只剩 1 种指纹**
⇒ ``_repeat_count`` 累加到 ``no_progress_repeat_threshold``(5) ⇒ **必然误报**
（不是概率问题）⇒ 注入收敛提示 → 仍不收敛 → ``ToolLoopAbortError`` 提前终止。

═══ 为什么必须锁在这一层 ═══
指纹是"判定依据"，判定动作（注入收敛提示 / 提前终止）**不可逆**。口径一旦退化，
后果不是"少检测一次"而是"掐死正在收敛的任务"。同一模式已发作两次，根因一致
（**指纹区分度退化 ⇒ 从概率误报变成必然误报**）：
    · aaf95335（2026-10-10）炸在**入参侧**：read_file 字段名契约错位 ⇒ key 恒为 ":0-0"
    · 4f5ede6a（2026-10-17）炸在**结果侧**：噪声占满取样窗口
两次都只修了当次暴露的那一侧 —— 本次把口径收敛到 `tool_call_signature` 单一实现，
并用 AST 断言钉住调用点，防止将来重构时又改回传结果文本。

运行（需在外层容器/venv，chat.py 依赖较重）：
    <venv>/python -m pytest tests/api/test_p0_tool_intent_sig.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path

from src.api.endpoints.chat import _tool_intent_sig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHAT_PY = PROJECT_ROOT / "src" / "api" / "endpoints" / "chat.py"

# 真实日志里每次 pymupdf 执行输出的固定首行（98 字符）
_FITZ_WARNING = (
    "warning: The `fitz` API is deprecated and will be removed in future. "
    "Use `import pymupdf` instead."
)

_REAL_COMMANDS = [
    'python3 -c "import fitz; d=fitz.open(\'a.pdf\'); print(%s)"' % expr
    for expr in (
        "d.page_count",
        "d[0].get_text()",
        "d[1].get_text()",
        "len(d[0].get_text())",
        "d[0].get_images()",
        "[i for i in range(d.page_count) if '光源' in d[i].get_text()]",
        "sum(len(d[i].get_text()) for i in range(10))",
        "d[0].get_text('text')[:200]",
        "d.metadata",
        "d[2].get_text()",
    )
]


def test_noise_prefix_must_still_yield_distinct_signatures():
    """回归锁：结果前缀完全相同（被噪声占满）时，10 条不同命令必须 10 种指纹。

    这条用例在旧实现下会**失败** —— 它就是本次事故的最小复现。
    """
    assert len(_FITZ_WARNING) > 60, "噪声行需长于旧 60 字符窗口才能复现该缺陷"
    assert len(_REAL_COMMANDS) == 10

    sigs = {
        _tool_intent_sig("execute", {"command": cmd}) for cmd in _REAL_COMMANDS
    }
    assert len(sigs) == 10, (
        f"10 条不同命令只算出 {len(sigs)} 种指纹（旧实现此处为 1）—— 4f5ede6a 复发"
    )


def test_repeat_window_never_reaches_threshold_for_distinct_commands():
    """端到端复刻 P0 计数逻辑：这批命令走一遍，_repeat_count 不得达到阈值。

    复刻的只是 chat.py 里那 5 行计数规则，**指纹用的是真实实现** —— 关键变量是真的。
    """
    threshold = 5  # = settings.no_progress_repeat_threshold 的默认值
    last_sig, repeat_count = None, 0
    for cmd in _REAL_COMMANDS:
        sig = _tool_intent_sig("execute", {"command": cmd})
        if sig is None:
            last_sig, repeat_count = None, 0
        elif sig == last_sig:
            repeat_count += 1
        else:
            last_sig, repeat_count = sig, 1
        assert not (repeat_count >= threshold), (
            f"第 {_REAL_COMMANDS.index(cmd) + 1} 次调用就触发了阈值 —— 误报"
        )
    assert repeat_count == 1


def test_identical_command_still_accumulates():
    """反向守门人：真·空转（同一条命令连发）仍必须累加，兜底能力不得被废掉。"""
    args = {"command": _REAL_COMMANDS[0]}
    sig = _tool_intent_sig("execute", args)
    assert sig is not None
    assert all(_tool_intent_sig("execute", args) == sig for _ in range(5))


def test_missing_args_returns_none():
    """参数未知必须返回 None（调用方据此重置计数、跳过判定）—— 宁漏报不误杀。

    误杀会掐断正在收敛的任务；漏报只是少一层兜底，图内中间件另有 args-hash
    与单工具调用频率上限两道防线。
    """
    assert _tool_intent_sig("execute", None) is None


def test_args_change_alone_moves_the_signature():
    """指纹只跟随参数：命令动一个字，指纹必须变。"""
    a = _tool_intent_sig("execute", {"command": "ls /reports"})
    b = _tool_intent_sig("execute", {"command": "ls /reports/"})
    assert a is not None and b is not None and a != b


def test_call_site_passes_args_not_result_text():
    """接线断言（AST，不启动服务）：`_tool_intent_sig` 的第二个实参不得是结果文本。

    沿用 tests/api/test_chat_wiring.py 的做法 —— 「改了逻辑但调用点没改回」这类
    回归，模块级单测全绿也发现不了。这里钉死：一旦有人把 content_str 传回去
    （即 4f5ede6a 的成因），本用例立即失败。
    """
    tree = ast.parse(CHAT_PY.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_tool_intent_sig"
    ]
    assert calls, "chat.py 里找不到 _tool_intent_sig 调用点（检测是否被摘掉了？）"
    for call in calls:
        assert len(call.args) >= 2, "_tool_intent_sig 调用点参数不足"
        passed = call.args[1]
        assert not (
            isinstance(passed, ast.Name) and passed.id == "content_str"
        ), (
            "_tool_intent_sig 的第二个实参又传回结果文本 content_str 了 —— "
            "这正是 4f5ede6a 误杀的成因，必须传该次调用的 args"
        )


def test_chat_imports_signature_from_core():
    """接线断言：指纹必须来自 core 的单一实现，不得在 chat.py 里另起一套。"""
    tree = ast.parse(CHAT_PY.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src.core.loop_detection":
            imported.update(alias.name for alias in node.names)
    assert "tool_call_signature" in imported, (
        "chat.py 未从 src.core.loop_detection 导入 tool_call_signature —— "
        "P0 与图内中间件的指纹口径可能已分叉"
    )
