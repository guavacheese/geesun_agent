"""防线 3 单测：工具契约知识必须真的进入模型的 system prompt。

背景（生产事故 GY24428:5f65f8df）：
  模型二次 write_file 同一路径失败后，把原因猜成"脚本太长"，
  改用 heredoc 内联 execute 绕过 → 撞上 4 分 16 秒无心跳。
  根因之一是 deepagents 的 WRITE_FILE_TOOL_DESCRIPTION 只有两句话
  （"Writes to a new file" / "Prefer to edit existing files"），
  既没说"已存在会失败"，也没说大 payload 的后果与出路。

判据不复读常量字符串，而是：
  1. 用 ast 从**真实源码**取出 PLC_AUDITOR_SYSTEM_PROMPT 的字面量；
  2. 断言它同时包含 write_file 的两条契约与一条反猜错指令；
  3. 反证：把 deepagents 的原始描述也取出来，证明它确实不含这些知识
     —— 否则这条测试证明不了"我们补了它"。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _literal_assignment(path: Path, name: str) -> str:
    """用 ast 从源码取出模块级字符串常量（不复刻、不 import，避免依赖运行时）。

    注意必须取 ``node.value.value``：直接 ``str(node.value)`` 会得到 AST 节点
    的 repr（形如 ``<ast.Constant object at 0x...>``），判据会**假 FAIL**。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign) else ([node.target] if isinstance(node, ast.AnnAssign) else [])
        )
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name:
                assert isinstance(node.value, ast.Constant), f"{name} 不是字面量常量"
                assert isinstance(node.value.value, str), f"{name} 的值不是字符串"
                return node.value.value
    raise AssertionError(f"{path} 中找不到常量 {name}")


def _prompt() -> str:
    return _literal_assignment(REPO / "src/core/prompts/plc_auditor.py", "PLC_AUDITOR_SYSTEM_PROMPT")


def _deepagents_write_file_description() -> str | None:
    """取 deepagents 自带的 WRITE_FILE_TOOL_DESCRIPTION（若本机可访问）。"""
    try:
        from deepagents.middleware.filesystem import WRITE_FILE_TOOL_DESCRIPTION
    except Exception:  # noqa: BLE001 - 拿不到就跳过反证，不影响主判据
        return None
    return str(WRITE_FILE_TOOL_DESCRIPTION)


class TestWriteContractInSystemPrompt:
    def test_states_write_file_cannot_overwrite(self):
        p = _prompt()
        assert "write_file" in p
        assert "File already exists" in p, "未告知 write_file 已存在会失败"
        assert "edit_file" in p, "未给出正确出路（edit_file）"

    def test_states_size_policy(self):
        p = _prompt()
        assert "80 KB" in p or "80KB" in p, "未给出单次写入大小上限"
        assert "edit_file" in p, "未给出分段/增量出路"

    def test_warns_against_guessing_exit_code_reason(self):
        """针对本次事故的直接对策：别凭退出码猜原因。"""
        p = _prompt()
        assert "Command exited with code" in p, "未解释非零退出的含义"
        assert "stdout" in p, "未指明失败原因在 stdout 里"
        assert "脚本太长" in p, "未针对模型的实际误判给出反例"


class TestOriginalDescriptionLackedThis:
    """反证：deepagents 原描述确实不含这些知识 ⇒ 本次补充是必要的。"""

    def test_upstream_description_is_insufficient(self):
        desc = _deepagents_write_file_description()
        if desc is None:
            # 环境拿不到 deepagents：不判失败，但必须说明跳过原因
            print("SKIP: 本机不可访问 deepagents，跳过反证")
            return
        assert "File already exists" not in desc, "上游若已含该知识则本条判据需重估"
        assert "80 KB" not in desc and "80KB" not in desc


class TestPromptStillHoldsOriginalContent:
    """回归：新增段落不得挤掉原有内容。"""

    def test_original_sections_intact(self):
        p = _prompt()
        for frag in (
            "PLC Code Review Engineer",
            "文件读取策略",
            "关键工具",
            "核心原则",
            "Skills 使用规则",
            "plc-code-auditor",
        ):
            assert frag in p, f"原有内容丢失: {frag}"
