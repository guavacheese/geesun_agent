"""AST 接线断言：LoopDetectionMiddleware 的新判据参数必须真的从 settings 接上。

为什么单独立一个文件（而不是塞进 tests/core/test_loop_detection.py）：
    core 那个文件测的是**中间件自身行为**（喂假 state 看它动不动），
    完全不管 `src/services/agent.py` 有没有把新参数传进去。
    而 2026-10-17 这次改动恰好是"逻辑在 loop_detection.py、旋钮在 config.py、
    接线在 agent.py"三处 —— 只要漏了接线，`tool_freq_ratio` 就永远停在
    构造器默认值，config.py 里那个可调项形同虚设，而 core 单测**全绿**。

    这不是假想：本项目已有同款先例（会话 GY24428:aaf95335，工具参数契约
    漏适配一层，静默从生产漏过去）。凡"改逻辑必须同时改调用点"的改动，
    一律配 AST 断言 —— 样板见 tests/api/test_chat_wiring.py。
"""

from __future__ import annotations

import ast
from pathlib import Path

AGENT_PY = Path(__file__).resolve().parents[2] / "src" / "services" / "agent.py"

#: 2026-10-17 新增的三个判据参数（对应 config.py 的同名字段）
_PATTERN_PARAMS = (
    "tool_freq_ratio",
    "min_window_fill",
    "alternating_min_run",
)

#: 原有参数：证明这次改动是**追加**而不是替换（替换会悄悄丢后向兼容路径）
_BASELINE_PARAMS = (
    "warn_threshold",
    "hard_limit",
    "tool_freq_warn",
    "tool_freq_hard_limit",
)


def _find_loop_middleware_kwargs() -> set[str]:
    """从 agent.py 里抓出 LoopDetectionMiddleware(...) 的所有关键字参数名。"""
    tree = ast.parse(AGENT_PY.read_text(encoding="utf-8"), filename=str(AGENT_PY))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "LoopDetectionMiddleware"):
            continue
        for kw in node.keywords:
            if kw.arg:
                found.add(kw.arg)
    return found


def test_pattern_params_are_wired_from_settings():
    """三个新判据参数必须在 agent.py 的 LoopDetectionMiddleware(...) 调用里出现。"""
    found = _find_loop_middleware_kwargs()
    missing = [p for p in _PATTERN_PARAMS if p not in found]
    assert not missing, (
        f"LoopDetectionMiddleware 缺少接线参数 {missing} —— "
        "config.py 里对应的阈值将永远停在构造器默认值，core 单测却仍全绿"
    )


def test_baseline_params_not_dropped():
    """原有四个参数不得在追加新参数时被顺手删掉（后向兼容路径会静默消失）。"""
    found = _find_loop_middleware_kwargs()
    missing = [p for p in _BASELINE_PARAMS if p not in found]
    assert not missing, f"原有参数被删：{missing}"


def test_new_params_read_from_settings_not_literals():
    """新参数的值必须来自 settings.*，不能写字面量（否则 config.py 白加）。"""
    src = AGENT_PY.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(AGENT_PY))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "LoopDetectionMiddleware"):
            continue
        for kw in node.keywords:
            if kw.arg not in _PATTERN_PARAMS:
                continue
            assert isinstance(kw.value, ast.Attribute), (
                f"{kw.arg} 传的不是 settings.* 而是 {ast.dump(kw.value)[:80]}"
            )
            assert isinstance(kw.value.value, ast.Name), f"{kw.arg} 的来源不是 settings"
            assert kw.value.value.id == "settings", f"{kw.arg} 的来源不是 settings"


def test_recursion_limit_is_not_hardcoded_literal():
    """chat.py 不得再把 recursion_limit 写成裸字面量（2026-10-17 配置化的全部意义）。"""
    chat_py = AGENT_PY.resolve().parents[1] / "api" / "endpoints" / "chat.py"
    src = chat_py.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(chat_py))

    literals: list[int] = []
    reads_settings = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value == "recursion_limit"):
                continue
            if isinstance(value, ast.Name):
                reads_settings = True
            elif isinstance(value, ast.Constant) and isinstance(value.value, int):
                literals.append(value.value)

    assert reads_settings, "chat.py 的 recursion_limit 不再从 settings 读（配置化被回退）"
    assert not literals, f"chat.py 仍有硬编码 recursion_limit 字面量：{literals}"
