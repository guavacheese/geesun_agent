"""skill 来源标记（`.origin`）—— 单一实现。

背景（2026-10-23）
-----------------
Agent 自创 skill 的默认落点从全局 ``/skills/__agent__/`` 改到每个用户自己的
``/skills/__user_{id}__/``。改完之后同一个目录里同时存在两类东西：

- agent 在对话中自动创建/沉淀的
- 用户通过上传 API 手动传的

光看源码目录已经分不出来（都在 `__user_{id}__` 下），但侧栏要把它们分到
「Agent 自创」和「用户上传」两组。于是在每个 skill 目录内落一个标记文件
``.origin``，内容 ``agent`` / ``upload``。

为什么用 sidecar 而不是 SKILL.md 的 frontmatter
----------------------------------------------
frontmatter 由模型自己写，依赖它等于把「分组正确性」交给模型——模型漏写一次，
这个 skill 就会掉到错误的组里。sidecar 由后端在写盘/上传的成功路径落，确定性更高。

为什么单独一个模块
------------------
写入方有两处（``api/endpoints/chat.py`` 的 file_generated 钩子 = agent 写的、
``api/endpoints/skills.py`` 上传 API = 用户传的），读取方一处（``list_skills``）。
常量与读写实现只留一份，避免两个 endpoint 各写一套字面量后口径漂移。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

ORIGIN_MARKER = ".origin"
ORIGIN_AGENT = "agent"
ORIGIN_UPLOAD = "upload"


def write_origin(skill_dir: str, origin: str) -> None:
    """落/更新 skill 来源标记。best-effort：失败只告警，不影响主流程。

    缺这个标记不会让功能坏掉（``read_origin`` 兜底归 ``upload``），只是分组不准，
    所以不值得为它中断一次成功写入。
    """
    try:
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, ORIGIN_MARKER), "w", encoding="utf-8") as f:
            f.write(origin)
    except OSError as e:
        logger.warning("[skill] 写入 .origin 失败: %s (%s)", skill_dir, e)


def read_origin(skill_dir: str) -> str:
    """读来源标记。无标记的（历史数据）一律归 ``upload``，保持既有展示不变。"""
    try:
        with open(os.path.join(skill_dir, ORIGIN_MARKER), encoding="utf-8") as f:
            val = f.read().strip()
        return val or ORIGIN_UPLOAD
    except OSError:
        return ORIGIN_UPLOAD
