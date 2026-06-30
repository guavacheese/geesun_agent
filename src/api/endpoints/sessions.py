import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.api.deps import get_store, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


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
        index_item = await store.aget(namespace, index_key)
        session_ids = index_item.value if index_item else []
    except Exception:
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
        except Exception:
            continue

    # 按 updated_at 倒序
    sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
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
    except Exception:
        pass

    return {"deleted": True, "session_id": session_id}


# ─── 获取消息 ───


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    store=Depends(get_store),
    current_user: dict = Depends(get_current_user),
):
    """获取某会话的所有消息。"""
    user_id = current_user["user_id"]
    msg_namespace = _messages_namespace(user_id, session_id)

    try:
        item = await store.aget(msg_namespace, "messages")
        messages = item.value if item else []
    except Exception:
        messages = []

    return {"session_id": session_id, "messages": messages}


# ─── 工具函数 ───


async def _update_session_index(
    store, namespace: tuple, session_id: str, add: bool
):
    """维护 session_id 索引列表。"""
    index_key = "__index__"
    try:
        item = await store.aget(namespace, index_key)
        ids = list(item.value) if item else []
    except Exception:
        ids = []

    if add and session_id not in ids:
        ids.append(session_id)
    elif not add and session_id in ids:
        ids.remove(session_id)

    await store.aput(namespace, index_key, ids)
