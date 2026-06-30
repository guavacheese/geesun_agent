import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from src.infra.sandbox import create_sandbox
from src.services.agent import create_agent
from src.core.mcp import get_mcp_tools
from src.api.deps import get_store, get_checkpointer, get_current_user
from src.core.config import settings

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    session_id: str = "default-session"
    message: str
    model_override: dict | None = (
        None  # 可选，动态切换模型：{"model_name": "...", "base_url": "...", "api_key": "..."}
    )


router = APIRouter()


@router.post("/chat")
async def chat(
    request: Request,
    body: ChatRequest,  # ← FastAPI 自动解析 JSON
    store=Depends(get_store),
    checkpointer=Depends(get_checkpointer),
    current_user: dict = Depends(get_current_user),  # 从 JWT 取当前用户
):
    user_id = current_user["user_id"]
    session_id = body.session_id
    thread_id = f"{user_id}:{session_id}"

    tools = await get_mcp_tools()
    sandbox = create_sandbox(thread_id)
    # 构建 skill sources：系统 + Agent 自创（启动时加载）+ 当前用户的共享 skill
    base_skills = getattr(request.app.state, "skills", [])
    user_skill_path = f"/skills/__user_{user_id}__/"
    skills = list(base_skills) + [user_skill_path]
    agent = await create_agent(
        user_id=user_id,
        session_id=body.session_id,
        thread_id=thread_id,
        store=store,
        sandbox=sandbox,
        checkpointer=checkpointer,
        tools=tools,
        skills=skills,
    )

    sandbox_id = ""
    if sandbox is not None:
        try:
            sandbox_id = sandbox.sandbox_id
        except Exception:
            pass

    path_hint = (
        f"沙箱 ID：{sandbox_id}\n"
        f"当前用户：{current_user.get('display_name', user_id)}（{current_user.get('role', 'user')}）\n"
        f"\n"
        f"【当前会话路径】\n"
        f"输入文件：/uploads/{user_id}/{session_id}/\n"
        f"报告输出：/reports/{user_id}/{session_id}/\n"
    )

    async def event_stream():
        invoke_kwargs = {}
        # 如果传了 model_config，通过 runtime context 传给 switch_model middleware
        if body.model_override:
            invoke_kwargs["context"] = {"model_config": body.model_override}

        # 追踪当前模型调用 ID，用于识别新一次 LLM 调用
        current_run_id = None
        # 标记当前是否刚发出过 thinking 事件
        thinking_emitted = False

        async for mode, data in agent.astream(
            {"messages": [{"role": "user", "content": f"{path_hint}\n{body.message}"}]},
            config={
                "configurable": {"thread_id": thread_id},
                "recursion_limit": 60,  # ← 最多 60 步，超了直接报错而不是卡死
            },
            stream_mode=["messages", "updates"],
            **invoke_kwargs,
        ):
            if (
                mode == "messages"
            ):  # mode == "messages" 时，data 是 (AIMessageChunk, metadata) 的元组
                token, metadata = data
                run_id = metadata.get("run_id") if metadata else None

                # 检测到新一次 LLM 调用 → 发出 thinking 状态
                if run_id and run_id != current_run_id:
                    current_run_id = run_id
                    thinking_emitted = False

                # 第一条 token 时发出 thinking（避免空 thinking 事件）
                content = token.content if hasattr(token, "content") else ""
                if content and not thinking_emitted:
                    thinking_emitted = True
                    yield f"data: {json.dumps({'type': 'agent_status', 'status': 'thinking'}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'token', 'content': content}, ensure_ascii=False)}\n\n"
                elif content:
                    yield f"data: {json.dumps({'type': 'token', 'content': content}, ensure_ascii=False)}\n\n"

            elif mode == "updates":
                # 根据 node 名称和输出内容解析 Agent 行为
                for node_name, node_output in data.items():
                    # 跳过 middleware 节点（非 agent 关键节点）
                    if node_name in (
                        "SkillsMiddleware.before_agent",
                        "PatchToolCallsMiddleware.before_agent",
                        "MemoryMiddleware.before_agent",
                        "HumanInTheLoopMiddleware.after_model",
                        "TodoListMiddleware.after_model",
                    ):
                        continue

                    messages = (
                        node_output.get("messages")
                        if isinstance(node_output, dict)
                        else None
                    )
                    if (
                        not messages
                        or not isinstance(messages, list)
                        or len(messages) == 0
                    ):
                        continue

                    last_msg = messages[-1]

                    if (
                        node_name == "model"
                        and hasattr(last_msg, "tool_calls")
                        and last_msg.tool_calls
                    ):
                        # LLM 调用了工具
                        for tc in last_msg.tool_calls:
                            yield f"data: {
                                json.dumps(
                                    {
                                        'type': 'tool_call',
                                        'tool': tc['name'],
                                        'args': tc['args'],
                                        'id': tc['id'],
                                    },
                                    ensure_ascii=False,
                                )
                            }\n\n"
                        yield f"data: {json.dumps({'type': 'agent_status', 'status': 'running_tool', 'tool': last_msg.tool_calls[0]['name']}, ensure_ascii=False)}\n\n"

                    elif node_name == "tools" and hasattr(last_msg, "name"):
                        # 工具执行结果
                        tool_name = last_msg.name
                        content_str = str(last_msg.content) if last_msg.content else ""
                        is_error = (
                            any(
                                kw in content_str.lower()
                                for kw in [
                                    "error",
                                    "exception",
                                    "traceback",
                                    "not found",
                                    "failed",
                                    "failure",
                                    "timeout",
                                    "permission denied",
                                ]
                            )
                            if content_str
                            else False
                        )

                        yield f"data: {
                            json.dumps(
                                {
                                    'type': 'tool_result',
                                    'tool': tool_name,
                                    'success': not is_error,
                                    'error': content_str[:500] if is_error else None,
                                },
                                ensure_ascii=False,
                            )
                        }\n\n"

        yield "data: [DONE]\n\n"

        # ─── SSE 流结束，保存消息到会话历史（P1）───
        try:
            # 读取最终状态中的消息
            state = await agent.aget_state(
                {
                    "configurable": {"thread_id": thread_id},
                }
            )
            logging.warning("[DIAG] SSE 结束, state=%s, has_values=%s",
                        type(state).__name__ if state else None,
                        hasattr(state, "values") if state else False)
            if state and hasattr(state, "values"):
                all_msgs = state.values.get("messages", [])
                logger.warning("[DIAG] 消息数=%d, 用户=%s, 会话=%s", len(all_msgs), user_id, session_id)
                all_msgs = state.values.get("messages", [])
                # 提取人类可读的消息（只保留 user / assistant / tool 角色的核心信息）
                history = []
                for msg in all_msgs:
                    role = getattr(msg, "type", "unknown")
                    if role == "human":
                        role = "user"
                    entry = {
                        "role": role,
                        "content": str(msg.content)[:2000] if msg.content else "",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    # AI 消息附带 tool_calls 信息
                    if role == "ai" and hasattr(msg, "tool_calls") and msg.tool_calls:
                        entry["tool_calls"] = [
                            {"name": tc["name"], "args": tc["args"]}
                            for tc in msg.tool_calls
                        ]
                    history.append(entry)

                # 存入 store（用 dict 包裹列表，避免 LangGraph PostgresStore 的 json.loads bug）
                msg_namespace = ("messages", user_id, session_id)
                await store.aput(msg_namespace, "messages", {"items": history})

                # 更新会话元数据（标题、消息数、时间）
                session_ns = ("sessions", user_id)
                item = await store.aget(session_ns, session_id)
                now_ts = datetime.now(timezone.utc).isoformat()

                # 用用户实际输入作为默认标题
                title = body.message[:50] + ("..." if len(body.message) >= 50 else "") if body.message else "新会话"

                if item is not None:
                    data = item.value
                    data["message_count"] = len(history)
                    data["updated_at"] = now_ts
                    old_title = data.get("title", "")
                    # 覆盖旧的 path_hint 标题，或首次设置标题
                    if old_title.startswith("沙箱 ID") or old_title == "新会话" or not old_title:
                        data["title"] = title
                else:
                    # 会话不存在则创建（兼容直接调 /chat 而非 POST /sessions 的场景）
                    data = {
                        "title": title,
                        "created_at": now_ts,
                        "updated_at": now_ts,
                        "message_count": len(history),
                    }

                # 确保会话在索引中（新增或已有都要维护）
                # 注意：__index__ 必须以 dict 存储（{"items": [...]}），
                # 因为 LangGraph PostgresStore 的 _row_to_item 对非 dict 值
                # 会调用 json.loads()，导致列表类型报错
                try:
                    idx_item = await store.aget(session_ns, "__index__")
                    idx_data = idx_item.value if idx_item else {}
                    ids = idx_data.get("items", []) if isinstance(idx_data, dict) else []
                except Exception as e:
                    logger.warning("[DIAG] 索引读取失败，重新初始化: %s", e)
                    ids = []
                try:
                    if session_id not in ids:
                        ids.append(session_id)
                    await store.aput(session_ns, "__index__", {"items": ids})
                except Exception as e:
                    logger.warning("[DIAG] 索引更新失败: %s", e)

                await store.aput(session_ns, session_id, data)
                logger.warning("[DIAG] 会话保存完成: user=%s, session=%s, msgs=%d", user_id, session_id, len(history))
        except Exception as e:
            logger.warning("保存会话消息失败（非关键错误）: %s", e)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
