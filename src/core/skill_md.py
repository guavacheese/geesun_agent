"""SKILL.md frontmatter 校验 —— 单一实现。

为什么单独一个模块（2026-10-23 抽出）
------------------------------------
这套校验原来只存在于 ``src/api/endpoints/skills.py``（上传 API 专用）。但 agent
通过 ``write_file`` 直写 skill 目录这条路**完全不校验**，而 deepagents 扫描端遇到
坏 frontmatter 只会静默 skip（见下），结果是"写成功了、用户以为建好了、实际永远
不会被加载"。

把用户自创 skill 的默认落点从全局 ``__skills/__agent__/`` 改到
``/skills/__user_{id}__/`` 之后，这条路变成唯一入口，必须补上校验。
分层上 ``services/`` 不能反向 import ``api/``，故校验本体落在 ``core/``，
上传 API 与本模块共同复用。

解析器一致性（不能换成正则逐行解析）
----------------------------------
必须用 ``yaml.safe_load``，与 deepagents 扫描端同一解析器。2026-08-19 实证：
旧版正则放过了 ``description: ... Triggers: "..."``（ASCII 冒号+空格），
skill 上传成功但永远无法被加载，静默 skip。
"""

from __future__ import annotations

import yaml


def validate_skill_md(content: str) -> dict:
    """校验 SKILL.md 的 YAML frontmatter，返回解析结果。

    要求：
    - 开头必须有两行 ``---`` 包裹的 YAML 块
    - YAML 语法必须可被 ``yaml.safe_load`` 解析
    - 必须包含 name 和 description 字段

    Returns:
        ``{"valid": bool, "name": str|None, "description": str|None, "error": str|None}``
    """
    content_clean = content.lstrip("\ufeff").lstrip()  # 去掉 BOM 和前导空白

    if not content_clean.startswith("---"):
        return {"valid": False, "name": None, "description": None,
                "error": "SKILL.md 必须以 --- 开头（YAML frontmatter）"}

    # 找到第二个 ---
    second_dash = content_clean.find("---", 3)
    if second_dash == -1:
        return {"valid": False, "name": None, "description": None,
                "error": "SKILL.md 的 YAML frontmatter 缺少闭合 ---"}

    yaml_block = content_clean[3:second_dash].strip()

    # 语法级解析（与 deepagents 扫描端一致）；YAMLError 透出具体行/列信息
    try:
        data = yaml.safe_load(yaml_block)
    except yaml.YAMLError as e:
        return {"valid": False, "name": None, "description": None,
                "error": f"frontmatter YAML 语法错误: {e}"}

    if not isinstance(data, dict):
        return {"valid": False, "name": None, "description": None,
                "error": "frontmatter YAML 解析结果不是键值映射（空块或列表等）"}

    name = str(data.get("name", "")).strip()
    description = str(data.get("description", "")).strip()

    errors = []
    if not name:
        errors.append("frontmatter 缺少 'name' 字段")
    if not description:
        errors.append("frontmatter 缺少 'description' 字段")

    if errors:
        return {"valid": False, "name": name, "description": description,
                "error": "; ".join(errors)}

    return {"valid": True, "name": name, "description": description, "error": None}
