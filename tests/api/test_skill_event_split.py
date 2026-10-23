"""skill 事件分流：`skill_created` ≠ `file_generated`（2026-10-23）。

守两个**已被真实事故验证过**的失败形态：

① 分类漏一半。skill 有两个落点（`/skills/__user_{uid}__/` 与 `/skills/__agent__/`），
   历史上各漏过一次：2026-08-11 只认 `__agent__` → 新写的 skill 不出卡；
   2026-10-23 只认 `__user_*__` → 存量 skill 不出卡。两次症状都是前端红框
   「文件不可用」。所以分类判定抽成纯函数 `_classify_skill_path` 并在这里逐个点名。

② 事件走错通道。skill 文件曾以 `file_generated` 下发，前端据此按
   `/api/v1/files/{uid}/{sid}/{name}` 做 HEAD 探测——skill 物理不在那个目录，必然
   404（实测会话 bf6874b2 的 `17:40:07 HEAD .../SKILL.md 404`），且会随 history
   落进 `generated_files`，刷新后照样复现。现在 skill 走独立事件 `skill_created`，
   前端只据此刷新技能面板。

②的判据用 AST 静态断言（本仓既有做法，见 tests/api/test_chat_wiring.py）：那一段
代码内联在一个 ~3500 行的 SSE 生成器里，无法直接调用；但"接线对不对"可以用 AST
精确钉住——skill 分支必须 ① 发 `skill_created`、② 绝不发 `file_generated`、
③ 以 `continue` 收尾（否则会掉进下面的 file_generated 兜底）。
"""

import ast
import os

import pytest

import src.api.endpoints.chat as chat_mod
from src.api.endpoints.chat import (
    SKILL_KIND_NONE,
    SKILL_KIND_SHARED,
    SKILL_KIND_USER,
    _classify_skill_path,
    _skill_disk_root,
)

CHAT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src",
    "api",
    "endpoints",
    "chat.py",
)


# ────────────────────────── ① 分类（真函数） ──────────────────────────


@pytest.mark.parametrize(
    "path,expected_kind,expected_rel",
    [
        # 默认落点：agent 自创 skill 写在用户自己目录（含中文/空格）
        ("/skills/__user_GY24428__/读后感撰写/SKILL.md", SKILL_KIND_USER, "读后感撰写/SKILL.md"),
        ("/skills/__user_GY24428__/PLC 报警数据导出/scripts/a.py", SKILL_KIND_USER, "PLC 报警数据导出/scripts/a.py"),
        # 历史共享层（已冻结，存量仍需识别，否则前端红框）
        ("/skills/__agent__/PLC 报警数据导出/SKILL.md", SKILL_KIND_SHARED, "PLC 报警数据导出/SKILL.md"),
        # 报告 / 其它路径不能被误判成 skill
        ("/reports/GY24428/s1/a.xlsx", SKILL_KIND_NONE, ""),
        ("/workspace/notes.md", SKILL_KIND_NONE, ""),
        ("", SKILL_KIND_NONE, ""),
        (None, SKILL_KIND_NONE, ""),
    ],
)
def test_classify_skill_path(path, expected_kind, expected_rel):
    assert _classify_skill_path(path, "GY24428") == (expected_kind, expected_rel)


def test_classify_is_user_scoped():
    """别人的用户目录不能被当成本人的（否则会拿不到磁盘大小 → 事件降级）。"""
    kind, _ = _classify_skill_path("/skills/__user_OTHER__/x/SKILL.md", "GY24428")
    assert kind == SKILL_KIND_NONE


def test_classify_skills_root_loose_file_is_still_user_skill():
    """直接躺在技能根下的散文件：分类仍是 user 层，但拿不到 skill_name（见调用方）。"""
    kind, rel = _classify_skill_path("/skills/__user_GY24428__/loose.md", "GY24428")
    assert kind == SKILL_KIND_USER
    assert "/" not in rel  # 调用方据此判断"这不是 skill，不发事件"


def test_skill_disk_root_matches_classification():
    """磁盘根与分类必须一一对应——两处各改一半就是上一轮 404 的成因。"""
    user_root = _skill_disk_root("GY24428", True)
    shared_root = _skill_disk_root("GY24428", False)
    assert user_root.endswith(os.path.join("skills", "__user_GY24428__"))
    assert shared_root.endswith(os.path.join("skills", "__agent__"))


# ────────────────────────── ② 事件分流（AST 接线） ──────────────────────────


def _parse(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


def _dict_keys(call: ast.Call) -> set[str]:
    """取 `await _pub({...})` 里 dict 字面量的 key 集合。"""
    for arg in call.args:
        if isinstance(arg, ast.Dict):
            return {
                k.value
                for k in arg.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    return set()


def _find_pub_calls(node: ast.AST) -> list[ast.Call]:
    """找出所有 `await _pub(...)` 调用（含嵌套）。"""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "_pub":
            out.append(sub)
    return out


def _event_types_in(node: ast.AST) -> set[str]:
    """节点内所有 `_pub({...'type': '<值>'...})` 声明的 type 值。"""
    types = set()
    for call in _find_pub_calls(node):
        for arg in call.args:
            if not isinstance(arg, ast.Dict):
                continue
            for k, v in zip(arg.keys, arg.values):
                if (
                    isinstance(k, ast.Constant)
                    and k.value == "type"
                    and isinstance(v, ast.Constant)
                    and isinstance(v.value, str)
                ):
                    types.add(v.value)
    return types


def _skill_branches(tree: ast.AST) -> list[ast.If]:
    """定位 `if is_skill_path:` 分支（skill 事件分流的唯一落点）。"""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "is_skill_path"
    ]


def _check_skill_branch(branch: ast.If) -> list[str]:
    """校验一个 `if is_skill_path:` 分支是否满足三条不变量，返回违反项说明。"""
    problems = []

    types = _event_types_in(branch)
    if "skill_created" not in types:
        problems.append("分支内未下发 skill_created")
    if "file_generated" in types:
        problems.append("分支内仍下发 file_generated（必然 404 红框）")

    # 必须以 continue 收尾：否则会继续往下走到 file_generated 的兜底 emit
    if not branch.body or not isinstance(branch.body[-1], ast.Continue):
        problems.append("分支未以 continue 收尾（会掉进 file_generated 兜底）")

    # skill_created 必须带 origin，前端据此区分"我的"与"共享历史"
    for call in _find_pub_calls(branch):
        if "skill_created" in _dict_keys(call) or _event_types_in(call) == {"skill_created"}:
            keys = _dict_keys(call)
            if not {"skill_created"} & keys:
                continue
            missing = {"skill_name", "file_path", "origin"} - keys
            if missing:
                problems.append(f"skill_created 缺字段: {sorted(missing)}")
    return problems


def test_skill_branch_splits_events():
    """真文件：skill 分支必须发 skill_created、不发 file_generated、以 continue 收尾。"""
    branches = _skill_branches(_parse(CHAT_PY))
    assert len(branches) == 1, f"预期恰好 1 处 skill 分流分支，实际 {len(branches)}"
    problems = _check_skill_branch(branches[0])
    assert not problems, " | ".join(problems)


def test_checker_can_actually_fail():
    """负向对照：证明上面的检查器**有能力失败**（否则它是恒真断言）。

    构造一个"看起来对、实际错"的分支：发了 skill_created，但没 continue、
    且还顺手发了 file_generated —— 正是 2026-10-23 之前的形态。
    """
    src = """
async def f(is_skill_path):
    if is_skill_path:
        await _pub({'type': 'skill_created', 'skill_name': 'a', 'file_path': 'p', 'origin': 'agent'})
        await _pub({'type': 'file_generated', 'file_name': 'SKILL.md'})
"""
    branches = _skill_branches(ast.parse(src))
    assert len(branches) == 1
    problems = _check_skill_branch(branches[0])
    assert problems, "检查器对错误形态未报警——断言已失效"
    assert any("file_generated" in p for p in problems)
    assert any("continue" in p for p in problems)


def test_skill_branch_still_feeds_progress_counters():
    """skill 沉淀也要算"真实进展"，否则 P0 无进展判据会把它误判成空转。"""
    branch = _skill_branches(_parse(CHAT_PY))[0]
    names = {n.id for n in ast.walk(branch) if isinstance(n, ast.Name)}
    for counter in ("_files_in_window", "_stagnation_calls", "_progress_exempt_count"):
        assert counter in names, f"skill 分支丢失进展计数 {counter}"


def test_classify_helper_is_used_by_chat_py():
    """接线断言：chat.py 必须真的调用分类函数（而不是自己内联一份又走偏）。"""
    tree = _parse(CHAT_PY)
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_classify_skill_path" in called
    assert "_skill_disk_root" in called


def _user_prefix_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """找出 chat.py 里所有 `f"/skills/__user_{...}__/"` 形态的**前缀字面量**。

    返回 [(行号, 把占位符替换成 u1 之后的字符串)]。
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        # 拼回"静态骨架"：常量取原值，插值位用占位符
        skeleton = "".join(
            v.value
            if isinstance(v, ast.Constant)
            else "{uid}"
            for v in node.values
        )
        if skeleton == "/skills/__user_{uid}__/":
            out.append((getattr(node, "lineno", -1), skeleton.replace("{uid}", "u1")))
    return out


def test_user_prefix_literals_agree_with_classifier():
    """chat.py 里所有用户技能前缀字面量都必须与 `_classify_skill_path` 一致。

    为什么查一致性而不是"禁止重复"：前缀在 chat.py 有正当的第三处用途——构建
    `skill_sources` 告诉 agent 去哪里扫 skill（约 1713 行）。它与事件分类必须指向
    **同一个目录**，否则 agent 写到 A 处、分类只认 B 处 → 前端永远不出卡（历史两次
    404 红框的同一根因）。所以这里逐个验证"每个字面量都能被分类函数认出来"。
    """
    literals = _user_prefix_literals(_parse(CHAT_PY))
    assert literals, "chat.py 里找不到用户技能前缀字面量，测试前提已变，请复核"

    for lineno, prefix in literals:
        kind, rel = _classify_skill_path(prefix + "探测技能/SKILL.md", "u1")
        assert kind == SKILL_KIND_USER, (
            f"chat.py:{lineno} 的前缀 {prefix!r} 未被分类函数识别（kind={kind}）——"
            "两处前缀已漂移"
        )
        assert rel == "探测技能/SKILL.md"

