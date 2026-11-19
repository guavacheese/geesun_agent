"""skill 写失败透出（2026-11-19）。

守一个已被真实事故验证的失败形态：

`__user_GY18008__` 被 root 旁路操作建成 root:root 后，agent 的 write_file 在
其下 mkdir 新技能目录连续 EACCES（会话 b89f416a，2026-11-19 20:03）。当时失败
只作为工具结果文本回给模型，**日志与前端零感知**——模型把 SKILL.md 降级写进
/reports 当普通交付物，"我的·Agent 创建"组为空，用户被要求自己手动上传，
排障只能靠翻 store 消息还原。现在这类失败必须：
  ① 落一条 ERROR 日志（带技能名/工具名/错误摘录）；
  ② 下发 `skill_write_failed` 事件（前端出黄色警示，不再静默）；
  ③ `continue` 收尾——失败结果不是交付物，绝不能掉进 file_generated 兜底
     出幽灵卡片，也不得进 skill_created/进展计数（写失败不是进展）。

判据函数 `_detect_skill_write_failure` 是纯函数直接测；③的接线用 AST 静态断言
（内联在 ~3500 行 SSE 生成器里无法直接调用，同 test_skill_event_split.py 的做法）。
"""

import ast
import os

import pytest

from src.api.endpoints.chat import (
    _detect_skill_write_failure,
)

CHAT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src",
    "api",
    "endpoints",
    "chat.py",
)

# 2026-11-19 GY18008/b89f416a 生产 store 消息 00000082 的**原文**（一字未改）。
# 注意报错里虚拟路径前缀被 backend 截掉（'/1122部件齐套生成/SKILL.md'），
# 只有落盘路径带完整技能根——这正是通道②存在的原因，别"顺手"改成规整形态。
REAL_PROD_ERROR = (
    "Error writing file '/1122部件齐套生成/SKILL.md': "
    "[Errno 13] Permission denied: "
    "'/data/agent/skills/__user_GY18008__/1122部件齐套生成'"
)


# ────────────────────────── ① 判据（真函数） ──────────────────────────


def test_detects_real_production_failure():
    """真实事故文案必须命中，且抠出的技能名要能定位到技能目录。"""
    got = _detect_skill_write_failure(
        "write_file", REAL_PROD_ERROR, True, "GY18008", None
    )
    assert got == "1122部件齐套生成"


def test_detects_via_virtual_path_channel():
    """结果里能解析出完整虚拟路径时（通道①）也要命中。"""
    got = _detect_skill_write_failure(
        "write_file",
        "Error writing file: disk quota exceeded",
        True,
        "GY24428",
        "/skills/__user_GY24428__/气缸数据清洗/SKILL.md",
    )
    assert got == "气缸数据清洗"


def test_success_is_not_failure():
    assert _detect_skill_write_failure("write_file", "Updated file /skills/__user_A__/x/SKILL.md", False, "A", "/skills/__user_A__/x/SKILL.md") is None


def test_non_write_tools_never_marked():
    """read/ls 等只读工具即便结果文本里带技能根（比如 ls 输出）也不算写失败
    ——同 `_SKILL_WRITE_TOOLS` 对 `.origin` 标记的护栏理由，别把看一眼当写坏。"""
    out = "total 8\ndrwxr-xr-x 2 root root 4096 __user_GY18008__/x/"
    assert _detect_skill_write_failure("execute", out, True, "GY18008", None) is None
    assert _detect_skill_write_failure("read_file", out, True, "GY18008", None) is None


def test_other_users_skill_root_not_matched():
    """别人的技能根报错不能标成本人的失败（否则 A 的会话为 B 的事故弹警示）。"""
    assert (
        _detect_skill_write_failure(
            "write_file", REAL_PROD_ERROR, True, "GY24428", None
        )
        is None
    )


def test_non_error_non_failure_text():
    assert _detect_skill_write_failure("write_file", "Updated file", False, "A", None) is None
    assert _detect_skill_write_failure("write_file", "nothing relevant", True, "A", None) is None


# ────────────────────────── ② 接线（AST 静态断言） ──────────────────────────


def _parse(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


def _failure_branch(tree: ast.Module) -> ast.If:
    """定位 `if _failed_skill:` 分支（透出逻辑唯一落点，必须恰好一个）。"""
    hits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "_failed_skill"
    ]
    assert len(hits) == 1, f"`if _failed_skill:` 分支应恰好 1 个，实际 {len(hits)}"
    return hits[0]


def test_failed_skill_comes_from_detector():
    """`_failed_skill` 必须由 `_detect_skill_write_failure` 赋值——
    判据改动只允许发生在纯函数里（可单测），不许在生成器内联判断。"""
    tree = _parse(CHAT_PY)
    assigns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_failed_skill" for t in node.targets
        )
    ]
    assert len(assigns) == 1
    call = assigns[0].value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    assert call.func.id == "_detect_skill_write_failure"


def test_branch_emits_event_and_error_log_and_continues():
    branch = _failure_branch(_parse(CHAT_PY))

    # ① ERROR 级日志（不是 warning——这是需要人修盘的事故级信号）
    has_logger_error = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "error"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "logger"
        for n in ast.walk(branch)
    )
    assert has_logger_error, "失败分支必须打 logger.error（事故级信号）"

    # ② 恰一个 `_pub({'type': 'skill_write_failed', ...})`，字段齐
    pub_type_values = set()
    for sub in ast.walk(branch):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "_pub":
            for arg in sub.args:
                if isinstance(arg, ast.Dict):
                    for k, v in zip(arg.keys, arg.values):
                        if (
                            isinstance(k, ast.Constant)
                            and k.value == "type"
                            and isinstance(v, ast.Constant)
                        ):
                            pub_type_values.add(v.value)
    assert pub_type_values == {"skill_write_failed"}

    # ③ `continue` 收尾：失败分支绝不能掉进下面的 file_generated 兜底
    assert any(isinstance(s, ast.Continue) for s in branch.body)


def test_failure_branch_precedes_skill_created_branch():
    """透出必须接在成功分支**之前**：报错结果同样能解析出路径（B① 放开了解析），
    接在后面就可能先被成功分支的护栏吞掉。用行号比较钉住顺序。"""
    tree = _parse(CHAT_PY)
    fail = _failure_branch(tree)
    ok_branches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "is_skill_path"
    ]
    assert ok_branches, "`if is_skill_path:` 成功分支不见了"
    assert fail.lineno < min(n.lineno for n in ok_branches)
