import logging
import os
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.api.deps import get_store, get_current_user

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
    store=Depends(get_store),
    current_user: dict = Depends(get_current_user),
):
    """
    获取当前用户的所有会话列表。
    按 updated_at 倒序排列。
    """
    user_id = current_user["user_id"]
    namespace = _session_namespace(user_id)

    # 遍历 store 中该 namespace 下的所有 session
    sessions = []
    try:
        # 用 store 的 list 方法，或者通过 get 单个 key 的方式
        # 由于 store 不直接支持遍历 namespace，我们用约定：
        # 每个 session 存为 key = session_id
        # 通过维护一个 index key 来记录所有 session_id
        index_key = "__index__"
        try:
            index_item = await store.aget(namespace, index_key)
            idx_data = index_item.value if index_item else {}
            session_ids = idx_data.get("items", []) if isinstance(idx_data, dict) else []
        except Exception as e:
            logger.error("获取会话索引失败: %s", e, exc_info=True)
            session_ids = []
    except Exception as e:
        logger.error("获取会话索引失败(外层): %s", e, exc_info=True)
        session_ids = []

    for sid in session_ids:
        try:
            item = await store.aget(namespace, sid)
            if item is None:
                continue
            sessions.append({
                "session_id": sid,
                **item.value,
            })
        except Exception as e:
            logger.error("获取会话 %s 的元数据失败: %s", sid, e, exc_info=True)
            continue

    # 先按更新时间倒序，再稳定排序让 pinned 置顶（同一组内保持倒序）
    sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    sessions.sort(key=lambda s: not s.get("pinned", False))
    return {"sessions": sessions}


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

    await store.aput(namespace, session_id, session_data)

    # 更新索引
    await _update_session_index(store, namespace, session_id, add=True)

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

    # 删除会话元数据
    await store.aput(namespace, session_id, None)

    # 从索引移除
    await _update_session_index(store, namespace, session_id, add=False)

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
                files.append({
                    "file_name": filename,
                    "file_path": path,
                    "file_size": 0,
                    "file_type": _infer_file_type(filename),
                })
            if files:
                msg["generated_files"] = files

    return {"session_id": session_id, "messages": messages}


# ─── 工具函数 ───


async def _update_session_index(
    store, namespace: tuple, session_id: str, add: bool
):
    """维护 session_id 索引列表。
    
    注意：__index__ 必须以 dict 存储（{"items": [...]}），
    因为 LangGraph PostgresStore 的 _row_to_item 对非 dict 值
    会调用 json.loads()，导致列表类型报错。
    """
    index_key = "__index__"
    try:
        item = await store.aget(namespace, index_key)
        idx_data = item.value if item else {}
        ids = idx_data.get("items", []) if isinstance(idx_data, dict) else []
    except Exception as e:
        logger.error("读取会话索引失败: %s", e, exc_info=True)
        ids = []

    if add and session_id not in ids:
        ids.append(session_id)
    elif not add and session_id in ids:
        ids.remove(session_id)

    await store.aput(namespace, index_key, {"items": ids})


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

