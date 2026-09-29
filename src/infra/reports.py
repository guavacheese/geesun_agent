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


def session_file_roots() -> list[str]:
    """会话文件的磁盘根目录候选（按优先级）。

    回退项存在的理由：`report_root` 若配置为 `/data/myapp/reports`，而实际文件落在
    `{agent_workspace}/data/reports`（容器/本地 WSL 路径差异），只查前者会 404。

    2026-09-29 抽出：原先这段目录清单只存在于 `api/endpoints/files.py::_search_file`，
    而 sessions.py 的"老数据补全"需要同款判定（只补磁盘真实存在的文件，见下），
    两处各写一份必然走偏，故统一到这里。settings 延迟导入以保持本模块可脱离
    fastapi/pydantic-settings 单独做单测（见 tests/infra/test_reports.py）。
    """
    from src.core.config import settings

    roots = [settings.report_root, settings.upload_root]
    try:
        for sub in ("reports", "uploads"):
            ws = os.path.join(settings.agent_workspace, "data", sub)
            if all(os.path.normpath(ws) != os.path.normpath(r) for r in roots):
                roots.append(ws)
    except Exception:  # noqa: BLE001 — 兜底目录拿不到不影响主路径
        pass
    return roots


def find_session_file(
    user_id: str,
    session_id: str,
    rel_path: str,
    roots: list[str] | None = None,
) -> str | None:
    """在候选根目录中定位会话文件的**真实磁盘路径**；找不到返回 None。

    `rel_path` 是相对会话目录的路径（如 `报告.md`、`sub/dir/a.json`）。

    安全：拒绝绝对路径与 `..`，并要求归一化后的候选路径严格落在
    `{root}/{user}/{sid}` 之下（用 `base + os.sep` 前缀判定，避免
    `/…/sid-other/` 这类"前缀相同但不同目录"的越界）。
    """
    if not rel_path or rel_path.startswith("/") or ".." in rel_path.split("/"):
        return None
    for root in roots if roots is not None else session_file_roots():
        base = os.path.normpath(os.path.join(root, user_id, session_id))
        candidate = os.path.normpath(os.path.join(base, rel_path))
        if candidate != base and not candidate.startswith(base + os.sep):
            continue
        if os.path.isfile(candidate):
            return candidate
    return None
