import base64
import logging
import os
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from src.api.deps import get_store, get_current_user
from src.core.config import settings
from src.infra.reports import snapshot_report_files

logger = logging.getLogger(__name__)

router = APIRouter()


# 与 chat.py 的 _infer_file_type 保持一致，用于兼容老消息
_FILE_TYPE_BY_EXT = {
    "md": "text", "txt": "text", "log": "text",
    "py": "code", "js": "code", "ts": "code", "tsx": "code",
    "css": "code", "html": "code", "json": "code",
    "yaml": "code", "yml": "code", "sh": "code",
    "java": "code", "go": "code", "rs": "code",
    "c": "code", "cpp": "code", "h": "code",
    "png": "image", "jpg": "image", "jpeg": "image",
    "gif": "image", "svg": "image", "webp": "image",
    "bmp": "image", "ico": "image",
    "pdf": "pdf",
    "xlsx": "spreadsheet", "xls": "spreadsheet", "csv": "spreadsheet",
    "zip": "archive", "tar": "archive", "gz": "archive",
    "7z": "archive", "rar": "archive",
}


def _infer_file_type(file_name: str) -> str:
    ext = file_name.split(".")[-1].lower() if "." in file_name else ""
    return _FILE_TYPE_BY_EXT.get(ext, "other")


# ─── 会话 CRUD ───
# 存储结构：
#   namespace ("sessions", user_id) → key: session_id → value: {title, created_at, updated_at, message_count}
#   namespace ("messages", user_id, session_id) → key: "messages" → value: [{role, content, ...}]


def _session_namespace(user_id: str) -> tuple:
    return ("sessions", user_id)


def _ns_text(namespace: tuple) -> str:
    """命名空间元组 → store 表里的 prefix 文本（与 langgraph 的约定一致）。"""
    return ".".join(namespace)


# ─── 会话列表读取：稳定 keyset 游标 ───
# 不用 BaseStore.asearch 做列表/翻页，原因（langgraph-checkpoint-postgres 3.1.0 实测）：
#   ① 它的排序键是 store.updated_at 单列、**非唯一**，PK (prefix,key) 不参与 → 无全序；
#   ② namespace 条件只有 `prefix LIKE %s`，越权过滤只能放应用层，而 OFFSET 的偏移量
#      是数据库端算的 → LIKE + 应用层过滤的组合在结构上无法分页；
#   ③ limit 默认 10，不显式传就静默截断。
# 改走 store.asearch_keyset（`prefix = %s` 精确 + 元组游标 + 全序），三条一次性消掉。
# 排序键是**业务字段** value.updated_at，与下面的置顶排序一致（pin 不改 updated_at）。
_SESSION_PAGE_SIZE = 200   # 逐页拉取的单页条数（取全模式下内部循环用它）
_SESSION_MAX_PAGES = 50    # 取全模式的防呆上限（50×200=10000 条），触顶打 error 而非静默截断
_SESSION_PAGE_LIMIT = 200  # HTTP 分页模式允许的最大 limit
# 历史遗留的手工索引 key：它不是会话条目，读取时要跳过。
_LEGACY_INDEX_KEY = "__index__"

# 游标分隔符：unit separator，不会出现在 session_id（uuid 前 8 位）或 ISO 时间串里
_CURSOR_SEP = "\x1f"


def _encode_cursor(updated_at: str, session_id: str) -> str:
    raw = f"{updated_at}{_CURSOR_SEP}{session_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    """游标是不透明串；解不开就是非法输入，直接 400（不能当成"从头发"）。"""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        updated_at, session_id = raw.split(_CURSOR_SEP, 1)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"cursor 非法: {e}") from e
    if not session_id:
        raise HTTPException(status_code=400, detail="cursor 非法: session_id 为空")
    return updated_at, session_id


def _to_session_row(key: str, value) -> dict | None:
    """store 条目 → 会话行；不可用的条目返回 None（跳过）。"""
    if key == _LEGACY_INDEX_KEY:
        return None
    if not isinstance(value, dict):
        logger.warning("会话 %s 的元数据不是 dict，已跳过: %r", key, type(value))
        return None
    return {"session_id": key, **value}


async def _alist_sessions(
    store,
    prefix: str,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    pinned: bool | None = None,
) -> tuple[list[dict], str | None]:
    """按 prefix 列出会话条目，返回 (行列表, next_cursor)。

    limit=None → **取全**：内部按 `_SESSION_PAGE_SIZE` 逐页推进游标直到取完。
                 不再有旧实现的「1000 条静默截断」，触达防呆上限会打 error。
    limit=N    → 取一页，返回下一页游标（None 表示已到底）。
    cursor     → 上一页末条的游标串（仅分页模式使用）。
    pinned     → True/False 只取置顶/非置顶；None 不筛。
    """
    ts_key: tuple[str, str] | None = _decode_cursor(cursor) if cursor else None
    rows: list[dict] = []

    if limit is not None:
        batch = await store.asearch_keyset(
            prefix, limit=limit, cursor=ts_key, pinned=pinned
        )
        for key, value in batch:
            row = _to_session_row(key, value)
            if row is not None:
                rows.append(row)
        next_cursor = (
            _encode_cursor(*_cursor_of(batch)) if len(batch) == limit else None
        )
        return rows, next_cursor

    # ── 取全模式 ──
    for _ in range(_SESSION_MAX_PAGES):
        batch = await store.asearch_keyset(
            prefix, limit=_SESSION_PAGE_SIZE, cursor=ts_key, pinned=pinned
        )
        if not batch:
            break
        for key, value in batch:
            row = _to_session_row(key, value)
            if row is not None:
                rows.append(row)

        new_ts_key = _cursor_of(batch)
        if ts_key is not None and new_ts_key >= ts_key:
            # 游标没有严格递减 → 再循环就是死循环，必须停（说明排序键有脏数据）
            logger.error(
                "会话游标未推进，提前停止: prefix=%s %s → %s", prefix, ts_key, new_ts_key
            )
            break
        ts_key = new_ts_key

        if len(batch) < _SESSION_PAGE_SIZE:
            break
    else:
        # for 正常跑完 = 每页都是满页且已达上限 → 结果不完整，必须显式告警
        logger.error(
            "会话列表取全达到防呆上限 %d 页（%d 条），结果可能不完整: prefix=%s",
            _SESSION_MAX_PAGES, _SESSION_MAX_PAGES * _SESSION_PAGE_SIZE, prefix,
        )

    return rows, None


def _cursor_of(batch: list[tuple[str, dict]]) -> tuple[str, str]:
    """取一页末条的 (updated_at, key) 作为下一页游标。

    必须与 SQL 的排序键表达式一致：`COALESCE(value->>'updated_at','')` ——
    缺 updated_at 的脏条目在 SQL 里按 '' 排到末尾，游标也要用 ''。
    """
    key, value = batch[-1]
    updated_at = value.get("updated_at", "") if isinstance(value, dict) else ""
    return str(updated_at or ""), key


def _messages_namespace(user_id: str, session_id: str) -> tuple:
    return ("messages", user_id, session_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CreateSessionRequest(BaseModel):
    title: str = "新会话"


class UpdateSessionRequest(BaseModel):
    title: str


# ─── 列表 ───


@router.get("/sessions")
async def list_sessions(
    limit: int | None = Query(
        None, ge=1, le=_SESSION_PAGE_LIMIT,
        description="分页模式：本页条数。不传则返回全部（现有前端行为）。",
    ),
    cursor: str | None = Query(
        None, description="分页模式：上一页返回的 next_cursor，不透明串。",
    ),
    store=Depends(get_store),
    current_user: dict = Depends(get_current_user),
):
    """获取当前用户的会话列表。

    两种模式：
    - **不传 limit（默认，现有前端走这条）**：返回全部会话，按 updated_at 倒序、
      pinned 置顶。走 keyset 游标逐页取全，不再有旧实现的 1000 条静默截断。
    - **传 limit**：返回一页非置顶会话 + 全量置顶会话 + next_cursor。
      置顶单独返回是必须的——置顶是应用层的第二段排序，若留在页内，
      置顶项会散落各页、且 pin/unpin 会打乱游标语义。
      （前端尚未接入此模式；接入时用它实现"加载更多"。）
    """
    user_id = current_user["user_id"]
    prefix = _ns_text(_session_namespace(user_id))

    if limit is None:
        try:
            sessions, _ = await _alist_sessions(store, prefix)
        except Exception as e:
            logger.error("遍历会话失败: %s", e, exc_info=True)
            sessions = []

        # 先按更新时间倒序，再稳定排序让 pinned 置顶（同一组内保持倒序）
        sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        sessions.sort(key=lambda s: not s.get("pinned", False))
        return {"sessions": sessions}

    # ── 分页模式 ──
    # 这里不能让异常退化成空页：调用方会把空页当成"已到底"，从而静默丢数据。
    try:
        page, next_cursor = await _alist_sessions(
            store, prefix, limit=limit, cursor=cursor, pinned=False
        )
        pinned_sessions, _ = await _alist_sessions(store, prefix, pinned=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("分页遍历会话失败: %s", e, exc_info=True)
        raise HTTPException(status_code=503, detail="会话列表暂时不可用，请重试") from e

    pinned_sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return {
        "sessions": page,
        "pinned_sessions": pinned_sessions,
        "next_cursor": next_cursor,
    }


# ─── 创建 ───


@router.post("/sessions")
async def create_session(
    body: CreateSessionRequest,
    store=Depends(get_store),
    current_user: dict = Depends(get_current_user),
):
    """
    创建新会话。
    自动生成 session_id（基于时间戳），返回创建的会话信息。
    """
    user_id = current_user["user_id"]
    from uuid import uuid4

    session_id = str(uuid4())[:8]
    namespace = _session_namespace(user_id)
    now = _now()

    session_data = {
        "title": body.title,
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
    }

    # 会话条目本身即为唯一数据源：列表接口按 namespace 前缀直接检索，
    # 不再需要维护 __index__ 索引（旧索引写入已移除，见 _alist_sessions 注释）。
    await store.aput(namespace, session_id, session_data)

    return {
        "session_id": session_id,
        **session_data,
    }


# ─── 重命名 ───


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str,
    body: UpdateSessionRequest,
    store=Depends(get_store),
    current_user: dict = Depends(get_current_user),
):
    """重命名会话。"""
    user_id = current_user["user_id"]
    namespace = _session_namespace(user_id)

    item = await store.aget(namespace, session_id)
    if item is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    data = item.value
    data["title"] = body.title
    data["updated_at"] = _now()

    await store.aput(namespace, session_id, data)

    return {"session_id": session_id, **data}


# ─── Pin / Unpin ───


class PinRequest(BaseModel):
    pinned: bool


@router.patch("/sessions/{session_id}/pin")
async def pin_session(
    session_id: str,
    store=Depends(get_store),
    current_user: dict = Depends(get_current_user),
):
    """Pin 会话。"""
    user_id = current_user["user_id"]
    namespace = _session_namespace(user_id)

    item = await store.aget(namespace, session_id)
    if item is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    data = item.value
    data["pinned"] = True
    data["pinned_at"] = _now()

    await store.aput(namespace, session_id, data)
    return {"session_id": session_id, **data}


@router.patch("/sessions/{session_id}/unpin")
async def unpin_session(
    session_id: str,
    store=Depends(get_store),
    current_user: dict = Depends(get_current_user),
):
    """Unpin 会话。"""
    user_id = current_user["user_id"]
    namespace = _session_namespace(user_id)

    item = await store.aget(namespace, session_id)
    if item is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    data = item.value
    data["pinned"] = False

    await store.aput(namespace, session_id, data)
    return {"session_id": session_id, **data}


# ─── 删除 ───


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    store=Depends(get_store),
    current_user: dict = Depends(get_current_user),
):
    """删除会话及其消息。"""
    user_id = current_user["user_id"]
    namespace = _session_namespace(user_id)

    item = await store.aget(namespace, session_id)
    if item is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 删除会话元数据（aput(None) 在 langgraph 内部走 DELETE）
    await store.aput(namespace, session_id, None)

    # 删除消息
    try:
        msg_namespace = _messages_namespace(user_id, session_id)
        await store.aput(msg_namespace, "messages", None)
    except Exception as e:
        logger.error("删除会话 %s 的消息失败: %s", session_id, e, exc_info=True)

    # 清理磁盘文件（非关键，失败不影响会话删除）
    try:
        for root in [settings.report_root, settings.upload_root]:
            session_dir = os.path.join(root, user_id, session_id)
            if os.path.isdir(session_dir):
                shutil.rmtree(session_dir)
                logger.info("已清理会话文件: user=%s, session=%s, dir=%s", user_id, session_id, session_dir)
    except Exception as e:
        logger.warning("清理会话文件失败（非关键错误）: %s", e)

    return {"deleted": True, "session_id": session_id}


# ─── 获取消息 ───


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    store=Depends(get_store),
    current_user: dict = Depends(get_current_user),
):
    """获取某会话的所有消息。

    兼容老数据：AI 消息没有 generated_files 时，扫描 content 自动
    补 /uploads/.../file.ext 或 /reports/.../file.ext 路径的文件信息，
    保证历史消息刷新后仍能看到文件卡片。
    """
    import re
    user_id = current_user["user_id"]
    msg_namespace = _messages_namespace(user_id, session_id)

    try:
        item = await store.aget(msg_namespace, "messages")
        msg_data = item.value if item else {}
        messages = msg_data.get("items", []) if isinstance(msg_data, dict) else []
    except Exception as e:
        logger.error("获取会话消息失败: session_id=%s, error=%s", session_id, e, exc_info=True)
        messages = []

    # 兼容老数据：AI 消息没有 generated_files 时从 content 补
    # 注意：反引号`排除——markdown 格式 `path` 的反引号不应被吞入路径
    file_path_re = re.compile(r"(/uploads/|/reports/)[^\s)\]\"',`]+")
    for msg in messages:
        if msg.get("role") == "ai" and not msg.get("generated_files"):
            content = msg.get("content", "")
            files = []
            seen = set()
            for m in file_path_re.finditer(content):
                path = m.group(0)
                if not path.startswith(f"/uploads/{user_id}/{session_id}/") and \
                   not path.startswith(f"/reports/{user_id}/{session_id}/"):
                    continue
                if path in seen:
                    continue
                seen.add(path)
                filename = path.split("/")[-1]
                if not filename:
                    # 目录路径（AI 只写了 /reports/{uid}/{sid}/ 没写文件名，如
                    # fe27a95a 会话最后一条 AI 的表格目录引用）不产生交付物，
                    # 补全会产出 {file_name:"", file_path:".../"} 脏条目，前端
                    # deriveGeneratedFiles 清洗后为空 → 吞掉真实文件卡
                    # （2026-08-24 实测 md/html 报告卡不显示）。跳过。
                    continue
                files.append({
                    "file_name": filename,
                    "file_path": path,
                    "file_size": 0,
                    "file_type": _infer_file_type(filename),
                })
            if files:
                msg["generated_files"] = files

    # ─── 目录扫描兜底已移除（2026-08-26）───
    # 原逻辑把 report_root/{user}/{sid}/ 下全部磁盘文件挂到「最后一条 AI 消息」，
    # 是为兼容旧 MessageItem（只在最后一条 AI 渲染文件卡）。前端改为 MessageGroup
    # 按 user turn 分组后，最后一条 AI 落在最后一个轮次，导致刷新后所有历史文件
    # 都显示在最后一个消息组（秋天/夏天/春天散文全挂在最后一条）。文件归属改由
    # 前端 deriveGeneratedFiles 从各组 tool_calls 派生（write_file/download_from_sandbox
    # 路径），不再依赖后端全量兜底。

    return {"session_id": session_id, "messages": messages}


# ─── 编辑历史消息并截断后续消息 ───


class EditSessionRequest(BaseModel):
    from_index: int
    new_message: str


@router.post("/sessions/{session_id}/edit")
async def edit_session_message(
    session_id: str,
    body: EditSessionRequest,
    request: Request,
    store=Depends(get_store),
    current_user: dict = Depends(get_current_user),
):
    """
    编辑会话中的某条用户消息，并删除其后的所有消息。

    只更新 PostgresStore 中的消息列表（get_session_messages 从此读取）。
    后续 /chat 的 continue_from_state 模式从存储消息重建 graph 输入，
    避免 LangGraph add_messages reducer 将截断视为追加导致旧消息残留。
    """
    user_id = current_user["user_id"]

    # 更新 PostgresStore 中的消息列表
    msg_namespace = _messages_namespace(user_id, session_id)
    try:
        item = await store.aget(msg_namespace, "messages")
        stored = item.value if item else {"items": []}
        stored_items = stored.get("items", []) if isinstance(stored, dict) else []

        if body.from_index < 0 or body.from_index >= len(stored_items):
            raise HTTPException(status_code=400, detail=f"from_index {body.from_index} 越界，消息总数 {len(stored_items)}")

        # 截断并替换
        stored_items = stored_items[: body.from_index + 1]
        stored_items[body.from_index] = {
            **stored_items[body.from_index],
            "content": body.new_message,
            "edited": True,
        }
        await store.aput(msg_namespace, "messages", {"items": stored_items})
        logger.info("edit: truncate to index %d done, new count=%d", body.from_index, len(stored_items))
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("edit: store update failed: %s", e)
        raise HTTPException(status_code=500, detail=f"更新消息列表失败: {str(e)}")

    return {"success": True, "session_id": session_id, "from_index": body.from_index}


# ─── 文件卡片修复（临时排查端点） ───


@router.post("/admin/repair-session-files")
async def repair_session_files(
    session_id: str,
    store=Depends(get_store),
    current_user: dict = Depends(get_current_user),
):
    """一次性修复：清洗历史消息里的脏 generated_files 并用磁盘真实文件补全。

    触发背景（2026-08-21）：模型 write_file 的 file_path 被传成目录（漏文件名），
    旧后端逻辑（chat.py 保存消息时只用工具解析的 _generated_files、未合并磁盘差集）
    emit 保存了 file_name="" 的 generated_files → 前端 "(未知文件) 文件不可用"
    （清洗后则无卡片）。本端点：
      1. 过滤 file_name 为空 / file_path 尾斜杠的脏条目；
      2. 用 report_root 磁盘全量文件补全缺失的 generated_files（挂到最后一条 AI 消息）。
    幂等：重复调用不会重复补已存在的 file_path。仅能操作当前登录用户自己的会话。
    """
    user_id = current_user["user_id"]
    msg_namespace = _messages_namespace(user_id, session_id)

    try:
        item = await store.aget(msg_namespace, "messages")
        msg_data = item.value if item else {}
        messages = msg_data.get("items", []) if isinstance(msg_data, dict) else []
    except Exception as e:
        logger.error("修复会话消息失败（读取）: session_id=%s, error=%s", session_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"读取消息失败: {e}")

    # 1. 清洗脏条目（file_name 空 / file_path 空或尾斜杠）
    removed = 0
    for msg in messages:
        gfs = msg.get("generated_files")
        if not gfs:
            continue
        cleaned = [
            g for g in gfs
            if g.get("file_name") and g.get("file_path")
            and not str(g["file_path"]).endswith("/")
        ]
        removed += len(gfs) - len(cleaned)
        if cleaned:
            msg["generated_files"] = cleaned
        else:
            msg.pop("generated_files", None)

    # 2. 磁盘全量文件补全（挂到最后一条 AI 消息）
    existing_fps = set()
    for msg in messages:
        for g in msg.get("generated_files", []):
            if g.get("file_path"):
                existing_fps.add(g["file_path"])
    base = os.path.join(settings.report_root, user_id, session_id)
    new_entries: list[dict] = []
    for rel in snapshot_report_files(settings.report_root, user_id, session_id):
        if not os.path.isfile(os.path.join(base, rel)):
            continue  # 只补文件，跳过子目录
        fp = f"/reports/{user_id}/{session_id}/{rel}"
        if fp in existing_fps:
            continue
        file_name = rel.rsplit("/", 1)[-1]
        try:
            size = os.path.getsize(os.path.join(base, rel))
        except OSError:
            size = 0
        new_entries.append({
            "file_name": file_name,
            "file_path": fp,
            "file_size": size,
            "file_type": _infer_file_type(file_name),
        })

    added = len(new_entries)
    if new_entries:
        target = None
        for msg in reversed(messages):
            if msg.get("role") == "ai":
                target = msg
                break
        if target is not None:
            target["generated_files"] = list(target.get("generated_files") or []) + new_entries
        else:
            logger.warning("修复会话文件：无 AI 消息可挂载，跳过补全: session=%s", session_id)
            added = 0

    # 3. 写回
    try:
        await store.aput(msg_namespace, "messages", {"items": messages})
    except Exception as e:
        logger.error("修复会话消息失败（写回）: session_id=%s, error=%s", session_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"写回失败: {e}")

    logger.info(
        "修复会话文件: user=%s, session=%s, cleaned=%d, added=%d",
        user_id, session_id, removed, added,
    )
    return {
        "ok": True,
        "session_id": session_id,
        "cleaned": removed,
        "added": added,
        "note": "已清洗空文件名条目并用磁盘真实文件补全；刷新前端会话即可看到文件卡片",
    }


