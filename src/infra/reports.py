"""reports 虚拟目录的产出快照工具（设计文档 M3 完成门）。

从 chat.py 拆出：snapshot 逻辑只依赖标准库与路径参数，
便于脱离 fastapi 依赖做单元测试。
"""

import os


def snapshot_report_files(report_root: str, user_id: str, session_id: str) -> frozenset[str]:
    """扫描 report_root/{user}/{sid}/ 下的相对路径集合（文件 + 目录名）。

    完成门用法：agent 运行前后各取一次做差集，判定本轮是否有新交付物产出。
    目录不存在（本轮首个任务、尚未建目录）返回空集。

    Args:
        report_root: 报告根目录（settings.report_root）
        user_id: 用户 ID
        session_id: 会话 ID

    Returns:
        相对路径的 frozenset，如 frozenset({'a.txt', 'sub/b.go'})
    """
    base = os.path.join(report_root, user_id, session_id)
    if not os.path.isdir(base):
        return frozenset()
    result = set()
    for root, dirs, files in os.walk(base):
        rel_root = os.path.relpath(root, base)
        prefix = "" if rel_root == "." else rel_root
        for d in dirs:
            result.add(os.path.join(prefix, d).replace(os.sep, "/") if prefix else d)
        for f in files:
            result.add(os.path.join(prefix, f).replace(os.sep, "/") if prefix else f)
    return frozenset(result)
