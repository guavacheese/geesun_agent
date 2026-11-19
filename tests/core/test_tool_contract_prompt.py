"""防线 3 单测：工具契约知识必须真的进入模型的 system prompt。

背景（生产事故 GY24428:5f65f8df）：
  模型二次 write_file 同一路径失败后，把原因猜成"脚本太长"，
  改用 heredoc 内联 execute 绕过 → 撞上 4 分 16 秒无心跳。
  根因之一是 deepagents 0.6 的 WRITE_FILE_TOOL_DESCRIPTION 只有两句话
  （"Writes to a new file" / "Prefer to edit existing files"），
  既没说"已存在会失败"，也没说大 payload 的后果与出路。

0.7.24 迁移演进（2026-11-19 实测）：FilesystemBackend.write 从 reject-overwrite
改为 create-or-overwrite（backends/filesystem.py:489），上游工具描述也随之改为
"replaces it entirely if it does"。契约方向随之翻转：从"必须告知会失败"变为
"必须告知**直接覆盖、无保护兜底**（既有路径写错 = 原内容即刻丢失）"——动机不变：
模型不得依赖不存在的护栏，也不得依赖会随库版本漂移的工具描述细节。

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
    def test_states_write_file_overwrite_semantics(self):
        p = _prompt()
        assert "write_file" in p
        assert "覆盖" in p, (
            "必须告知 write_file 对既有路径直接覆盖的真实语义"
            "（0.7: create-or-overwrite，无'已存在则报错'兜底）"
        )
        assert "edit_file" in p, "未给出修改既有文件的优先出路（edit_file）"

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
    """反证：我们补充的独有知识上游描述仍不含 ⇒ 补充仍必要。

    0.7 起上游描述已自带 "replaces it entirely if it does"（覆盖语义），
    该项不再是差异点——但提示词仍写自己的版本，契约知识必须锚定在自有
    prompt 里，不随库版本漂移。真正仍缺的是大小策略与反猜错指令。
    """

    def test_upstream_description_is_insufficient(self):
        desc = _deepagents_write_file_description()
        if desc is None:
            # 环境拿不到 deepagents：不判失败，但必须说明跳过原因
            print("SKIP: 本机不可访问 deepagents，跳过反证")
            return
        assert "80 KB" not in desc and "80KB" not in desc, (
            "上游若已含大小策略则本条判据需重估"
        )
        assert "Command exited with code" not in desc


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
