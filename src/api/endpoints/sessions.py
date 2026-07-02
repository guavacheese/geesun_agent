import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.api.deps import get_store, get_current_user
from src.services.agent import create_agent
from src.infra.sandbox import create_sandbox
from src.core.mcp import get_mcp_tools

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
        try:
            index_item = await store.aget(namespace, index_key)
            idx_data = index_item.value if index_item else {}
            session_ids = idx_data.get("items", []) if isinstance(idx_data, dict) else []
        except Exception:
            session_ids = []
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
        msg_data = item.value if item else {}
        messages = msg_data.get("items", []) if isinstance(msg_data, dict) else []
    except Exception:
        messages = []

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
    except Exception:
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

    - from_index: 要编辑的用户消息在 messages 中的索引
    - new_message: 编辑后的新内容

    后端通过 LangGraph checkpoint 修改 state.messages：
    1. 保留 from_index 之前的消息
    2. 将索引 from_index 处的消息替换为新的 HumanMessage
    3. 保存回 checkpoint，后续 /chat 即可从该状态继续流式生成
    """
    user_id = current_user["user_id"]
    thread_id = f"{user_id}:{session_id}"

    # 读取 LangGraph state
    checkpointer = request.app.state.checkpointer
    base_skills = getattr(request.app.state, "skills", [])
    user_skill_path = f"/skills/__user_{user_id}__/"
    skills = list(base_skills) + [user_skill_path]
    tools = await get_mcp_tools()
    sandbox = create_sandbox(thread_id)

    agent = await create_agent(
        user_id=user_id,
        session_id=session_id,
        thread_id=thread_id,
        store=store,
        sandbox=sandbox,
        checkpointer=checkpointer,
        tools=tools,
        skills=skills,
    )

    config = {"configurable": {"thread_id": thread_id}}

    try:
        state = await agent.aget_state(config)
    except Exception as e:
        logger.error("读取 session state 失败: %s", e)
        raise HTTPException(status_code=500, detail=f"读取会话状态失败: {str(e)}")

    messages = state.values.get("messages", [])
    if not messages:
        raise HTTPException(status_code=404, detail="会话没有消息")

    if body.from_index >= len(messages):
        raise HTTPException(status_code=400, detail="from_index 超出消息范围")

    # 截断并替换用户消息
    new_messages = messages[: body.from_index + 1]
    target_message = new_messages[body.from_index]

    # 判断消息类型：HumanMessage 直接替换 content
    from langchain_core.messages import HumanMessage

    is_human = isinstance(target_message, HumanMessage) or getattr(target_message, "type", None) == "human"
    if not is_human:
        raise HTTPException(status_code=400, detail="from_index 指向的不是用户消息")

    new_messages[body.from_index] = HumanMessage(content=body.new_message)

    try:
        await agent.aupdate_state(config, {"messages": new_messages})
    except Exception as e:
        logger.error("更新 session state 失败: %s", e)
        raise HTTPException(status_code=500, detail=f"更新会话状态失败: {str(e)}")

    # 同步更新 PostgresStore 中的消息列表（get_session_messages 从此读取）
    msg_namespace = _messages_namespace(user_id, session_id)
    try:
        item = await store.aget(msg_namespace, "messages")
        stored = item.value if item else {"items": []}
        stored_items = stored.get("items", []) if isinstance(stored, dict) else []
        if body.from_index < len(stored_items):
            stored_items = stored_items[: body.from_index + 1]
            stored_items[body.from_index] = {
                **stored_items[body.from_index],
                "content": body.new_message,
                "edited": True,
            }
            await store.aput(msg_namespace, "messages", {"items": stored_items})
    except Exception as e:
        logger.warning("同步 messages store 失败: %s", e)
        # 不影响主流程

    return {"success": True, "session_id": session_id, "from_index": body.from_index}

