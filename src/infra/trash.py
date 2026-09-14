"""会话文件的两阶段删除（rename-to-trash）与超期清扫（2026-09-14）。

背景：文件系统与 DB 没有共同事务，无法做到跨资源原子删除。能做到的最强
一致是"失败零副作用"——把"删文件"拆成可逆的预备步骤：

  ① move_to_trash：同文件系统内 os.rename 到 <root>/.trash/<sid>_<毫秒>，
     原子且瞬间；任一步失败 → move_back 全部还原，删会话整体零副作用
  ② 调用方在事务提交成功后 remove_trash；失败则 move_back 还原（附件无损）
  ③ 兜底：若 ② 的清理失败或进程在提交后崩溃，会留下 .trash 条目——
     sweep_trash 按 TTL 定期清掉（不可见，无数据风险）

**TTL 判定只用目录名里的毫秒时间戳，不能用 mtime/ctime**：os.rename 保留
原目录的 mtime，一个"内容很旧、刚被移进 trash 等事务提交"的目录会立刻
满足 TTL 而被清掉——此时若事务失败要还原就没有可还原的东西了，等于把
数据丢失风险从后门放回来。目录名的时间戳就是 move 时刻，是唯一可靠依据；
解析不出时间戳的条目一律跳过并告警（不猜、不删）。

安全约束：只遍历 <root>/.trash 的一级子项；不跟随符号链接（遇到链接只
unlink 链接本身）；FileNotFoundError 容忍（与删除/还原并发）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time

logger = logging.getLogger(__name__)

TRASH_DIRNAME = ".trash"
# <session_id>_<13 位毫秒时间戳>；session_id 为 uuid/十六进制，不含下划线
_TRASH_NAME_RE = re.compile(r"^(?P<sid>.+)_(?P<ts>\d{13})$")


def trash_root(root: str) -> str:
    """<root>/.trash 路径。"""
    return os.path.join(root, TRASH_DIRNAME)


def move_to_trash(root: str, user_id: str, session_id: str) -> tuple[str, str] | None:
    """把会话目录移进 trash。返回 (原目录, trash 目录)；目录不存在返回 None。

    只在目标确实存在时 rename，调用方需保证 root 下 <user>/<sid> 是会话目录
    （user_id/session_id 均来自已鉴权上下文或 UUID，非自由文本）。
    """
    session_dir = os.path.join(root, user_id, session_id)
    if not os.path.isdir(session_dir):
        return None
    trash_path = os.path.join(
        trash_root(root), f"{session_id}_{int(time.time() * 1000)}"
    )
    os.makedirs(os.path.dirname(trash_path), exist_ok=True)
    os.rename(session_dir, trash_path)  # 同文件系统内原子；跨设备会抛 OSError
    return session_dir, trash_path


def move_back(pairs: list[tuple[str, str]]) -> int:
    """把 trash 条目还原回原路径（失败路径用）。返回还原失败的数量。

    整批还原而不是逐个失败就放弃：还原本身几乎不会失败（同文件系统 rename），
    真失败说明磁盘/权限异常，此时必须留下 error 级日志供人工处理。
    """
    failed = 0
    for orig, trash in pairs:
        try:
            os.rename(trash, orig)
        except FileNotFoundError:
            logger.error("还原失败：trash 条目不存在（数据可能已被清扫）: %s", trash)
            failed += 1
        except OSError as e:
            logger.error("还原会话文件失败，需人工处理: %s -> %s: %s", trash, orig, e)
            failed += 1
    return failed


def remove_trash(trash_path: str) -> bool:
    """事务提交成功后物理删除 trash 条目。失败仅告警（sweep_trash 兜底）。"""
    try:
        if os.path.isdir(trash_path) and not os.path.islink(trash_path):
            shutil.rmtree(trash_path)
        else:
            os.unlink(trash_path)
        return True
    except FileNotFoundError:
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("清理 .trash 失败（非关键，可被 TTL 清扫兜底）: %s: %s", trash_path, e)
        return False


def sweep_trash(root: str, ttl_seconds: int) -> dict[str, int]:
    """清扫 <root>/.trash 下超期条目。返回 {"scanned","removed","skipped"}。

    同步阻塞函数（rmtree 是阻塞 IO），调用方须用 asyncio.to_thread 包住，
    避免大目录删除卡住事件循环。
    """
    stats = {"scanned": 0, "removed": 0, "skipped": 0}
    base = trash_root(root)
    if not os.path.isdir(base) or os.path.islink(base):
        return stats
    now_ms = int(time.time() * 1000)
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return stats
    for name in names:
        stats["scanned"] += 1
        path = os.path.join(base, name)
        m = _TRASH_NAME_RE.match(name)
        if not m:
            stats["skipped"] += 1
            logger.warning("[trash] 条目名不含时间戳，跳过不删（需人工确认）: %s", path)
            continue
        if os.path.islink(path):  # 只删链接本身，绝不跟随（防被指向的目录被删）
            try:
                os.unlink(path)
                stats["removed"] += 1
            except OSError as e:
                stats["skipped"] += 1
                logger.warning("[trash] 符号链接删除失败: %s: %s", path, e)
            continue
        age_ms = now_ms - int(m.group("ts"))
        if age_ms < ttl_seconds * 1000:
            continue  # 未超期：可能正等待事务提交/还原，必须留
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            stats["removed"] += 1
            logger.info("[trash] 已清扫超期条目: %s (age=%.1fh)", path, age_ms / 3_600_000)
        except FileNotFoundError:
            pass  # 与本函数并发（被别的路径还原/删除）——正常
        except Exception as e:  # noqa: BLE001
            stats["skipped"] += 1
            logger.warning("[trash] 清扫失败（保留待下轮）: %s: %s", path, e)
    return stats
