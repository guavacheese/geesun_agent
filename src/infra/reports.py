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


def session_report_roots() -> list[str]:
    """会话**产物**（reports）的磁盘根目录候选——`session_file_roots` 的收窄版。

    产物清单端点（files.py list）只列交付物：用户上传的输入文件（uploads）
    按「输入只读、输出可操作」原则不进产物面板（前端 UserAttachmentChip 同款
    设计哲学）。根目录清单与下载端点保持同源，只是不含 uploads 两个根——
    否则"能列出但下载逻辑不同源"就会演变成两套事实。
    """
    from src.core.config import settings

    roots = [settings.report_root]
    try:
        ws = os.path.join(settings.agent_workspace, "data", "reports")
        if all(os.path.normpath(ws) != os.path.normpath(r) for r in roots):
            roots.append(ws)
    except Exception:  # noqa: BLE001 — 兜底目录拿不到不影响主路径
        pass
    return roots


def list_session_reports(
    user_id: str,
    session_id: str,
    roots: list[str] | None = None,
) -> list[dict]:
    """列出会话报告目录下的全部产物文件（跨候选根按优先级去重）。

    用途：`GET /api/v1/files/{user}/{sid}` 产物清单——前端「会话产物」面板的数据源
    （2026-10-14 方案 C：卡片是"轮次级+写事件驱动"，用户需要一个不依赖本轮
    有没有写文件的、始终可看的全量产物入口）。

    安全（与 find_session_file 同级的两道防御）：
      - user_id / session_id 含路径分隔符或 `..` → 返回空（它们来自 URL 路径参数，
        不校验就可能被 `/files/u/../../etc` 这类构造越出报告根）；
      - 遍历得到的都是 base 之下的相对路径，天然不含穿越段。

    去重：同一相对路径可能同时落在 report_root 与 workspace 兜底根（历史路径
    漂移期的双写残留），先命中的根优先（= find_session_file 的查找顺序）。

    Returns:
        [{"rel_path": str, "size": int, "mtime": float}]，按 mtime 倒序（最新在前）。
        目录不存在/为空 → []。
    """
    if (
        not user_id
        or not session_id
        or "/" in user_id
        or "/" in session_id
        or "\\" in user_id
        or "\\" in session_id
        or ".." in (user_id, session_id)
    ):
        return []
    seen: dict[str, dict] = {}
    for root in roots if roots is not None else session_report_roots():
        base = os.path.normpath(os.path.join(root, user_id, session_id))
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                if rel in seen:
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue  # 遍历与 stat 之间文件被移走：跳过比炸掉清单安全
                seen[rel] = {"rel_path": rel, "size": st.st_size, "mtime": st.st_mtime}
    return sorted(seen.values(), key=lambda it: it["mtime"], reverse=True)


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
