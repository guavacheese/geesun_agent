import json
import os
import re
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
    message: str = ""
    model_override: dict | None = (
        None  # 可选，动态切换模型：{"model_name": "...", "base_url": "...", "api_key": "..."}
    )
    files: list[str] | None = None  # 可选，本轮上传的文件虚拟路径列表
    continue_from_state: bool = (
        False  # 为 true 时不新增用户消息，直接从当前 checkpoint 继续生成
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
    logger.info("[DIAG] create_sandbox(thread_id=%s) → sandbox=%s", thread_id, type(sandbox).__name__ if sandbox else "None")
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
            logger.info("[DIAG] sandbox_id 提取成功: %s", sandbox_id)
        except Exception as e:
            logger.warning("[DIAG] sandbox_id 提取失败: %s", e)
    else:
        logger.warning("[DIAG] sandbox 为 None（create_sandbox 返回空），sandbox_id 未设置")
    
    if not sandbox_id:
        logger.warning("[DIAG] sandbox_id 最终为空，upload_to_sandbox 等工具将无法使用")

    path_hint = (
        f"沙箱 ID：{sandbox_id}\n"
        f"当前用户：{current_user.get('display_name', user_id)}（{current_user.get('role', 'user')}）\n"
        f"\n"
        f"【当前会话路径】\n"
        f"输入文件：/uploads/{user_id}/{session_id}/\n"
        f"报告输出：/reports/{user_id}/{session_id}/\n"
    )

    # 本轮文件提示（多轮对话时 Agent 只处理本轮上传的文件）
    file_hint = ""
    if body.files:
        file_list = "\n".join(f"- {f}" for f in body.files)
        file_hint = f"\n用户为本轮对话上传了以下文件（路径已映射到虚拟文件系统，请精确处理这些文件）：\n{file_list}"

    user_message = f"{path_hint}\n{body.message}{file_hint}"

    # 构造 graph 输入：
    graph_config = {"configurable": {"thread_id": thread_id}}

    # - 正常发送：新增用户消息
    # - 编辑后重发：从 PostgresStore 读取截断后的消息列表重建 graph 状态
    from langchain_core.messages import HumanMessage, AIMessage

    if body.continue_from_state:
        try:
            msg_namespace = ("messages", user_id, session_id)
            item = await store.aget(msg_namespace, "messages")
            stored = item.value if item else {"items": []}
            stored_items = stored.get("items", []) if isinstance(stored, dict) else []
            lc_msgs = []
            for m in stored_items:
                if m.get("role") == "user":
                    lc_msgs.append(HumanMessage(content=m.get("content", "")))
                elif m.get("role") == "ai":
                    lc_msgs.append(AIMessage(content=m.get("content", "")))
            graph_input = {"messages": lc_msgs}
            logger.info("continue_from_state: 从 store 重建 %d 条消息", len(lc_msgs))
        except Exception as e:
            logger.warning(
                "continue_from_state: 读取 store 失败, fallback 到 checkpoint: %s", e
            )
            latest = await agent.aget_state({"configurable": {"thread_id": thread_id}})
            graph_input = (
                {"messages": latest.values.get("messages", [])} if latest else None
            )
    else:
        graph_input = {"messages": [{"role": "user", "content": user_message}]}

    async def event_stream():
        invoke_kwargs = {}
        # 如果传了 model_config，通过 runtime context 传给 switch_model middleware
        if body.model_override:
            invoke_kwargs["context"] = {"model_config": body.model_override}

        # 标记当前是否刚发出过 thinking 事件
        thinking_emitted = False
        # [DEBUG] 记录上一个 langgraph_step，避免逐 token 重复打印
        _last_debug_step = None

        # ─── 流式生成循环 ───
        # 使用 try/except 保护，防止 agent.astream 内部异常导致 SSE 流中断
        try:
            async for mode, data in agent.astream(
                graph_input,
                config={
                    **graph_config,
                    "recursion_limit": 100,  # ← 最多 100 步，正常流程 25-35 步，留 2-3 倍余量
                },
                stream_mode=["messages", "updates"],
                **invoke_kwargs,
            ):
                if (
                    mode == "messages"
                ):  # mode == "messages" 时，data 是 (AIMessageChunk, metadata) 的元组
                    token, metadata = data
                    # 用 langgraph_step 变化检测"新一次 LLM 调用"，替代不存在的 run_id
                    if metadata:
                        step = metadata.get("langgraph_step")
                        if step is not None and step != _last_debug_step:
                            _last_debug_step = step
                            thinking_emitted = False  # 新 step → 重置 thinking 标记
                            logger.warning(
                                "[DIAG] new messages stream: step=%s, node=%s, metadata keys=%s",
                                step,
                                metadata.get("langgraph_node"),
                                list(metadata.keys()),
                            )

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
                            tool_call_id = getattr(last_msg, "tool_call_id", None)
                            content_str = (
                                str(last_msg.content) if last_msg.content else ""
                            )
                            is_error = False
                            if content_str:
                                # 优先尝试 JSON 解析：MCP 工具返回结构化 JSON 带 success 字段
                                try:
                                    parsed = json.loads(content_str)
                                    if isinstance(parsed, dict) and "success" in parsed:
                                        is_error = not parsed["success"]
                                    # 结构化 JSON 不走关键词匹配
                                except (json.JSONDecodeError, TypeError):
                                    # 非 JSON 内容，退回到关键词匹配
                                    if tool_name == "execute":
                                        is_error = (
                                            "command failed with exit code"
                                            in content_str.lower()
                                            or content_str.startswith("Execution error:")
                                        )
                                    else:
                                        is_error = any(
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

                            yield f"data: {
                                json.dumps(
                                    {
                                        'type': 'tool_result',
                                        'tool': tool_name,
                                        'id': tool_call_id,
                                        'success': not is_error,
                                        'error': content_str[:500]
                                        if is_error
                                        else None,
                                        'result': content_str[:2000]
                                        if content_str
                                        else None,
                                    },
                                    ensure_ascii=False,
                                )
                            }\n\n"

                            # ─── 检测工具返回的文件信息，提取生成/下载的文件 ───
                            # 不限定工具名，任何返回 /reports/ 路径的工具都能触发
                            file_path_virtual = None
                            if not is_error:
                                if tool_name in ("write_file", "write", "create_file"):
                                    m = re.search(
                                        r"Updated file\s+(/\S+)", content_str
                                    )
                                    if m:
                                        file_path_virtual = m.group(1)
                                else:
                                    # 其他工具（如 download_from_sandbox）：
                                    # 在返回值中搜索 /reports/ 路径
                                    m = re.search(
                                        r'/reports/\S+', content_str
                                    )
                                    if m:
                                        file_path_virtual = m.group(0).rstrip(
                                            '"'
                                        ).rstrip("}").rstrip(",")

                            if file_path_virtual and file_path_virtual.startswith(
                                "/reports/"
                            ):
                                prefix = f"/reports/{user_id}/{session_id}/"
                                if file_path_virtual.startswith(prefix):
                                    filename = file_path_virtual[len(prefix) :]
                                    file_size = 0
                                    try:
                                        disk_path = os.path.join(
                                            settings.report_root,
                                            user_id,
                                            session_id,
                                            filename,
                                        )
                                        if os.path.isfile(disk_path):
                                            file_size = os.path.getsize(disk_path)
                                    except Exception:
                                        pass
                                    # 如果磁盘没取到大小，尝试从 JSON 返回值中提取 size
                                    if file_size == 0:
                                        size_m = re.search(
                                            r'"size"\s*:\s*(\d+)', content_str
                                        )
                                        if size_m:
                                            file_size = int(size_m.group(1))

                                    yield f"data: {
                                                json.dumps(
                                                    {
                                                        'type': 'file_generated',
                                                        'file_name': file_path_virtual.split(
                                                            '/'
                                                        )[-1],
                                                        'file_path': file_path_virtual,
                                                        'file_size': file_size,
                                                    },
                                                    ensure_ascii=False,
                                                )
                                            }\n\n"
        except Exception as e:
            # ─── 异常保护：任何 agent.astream 内的异常都被捕获，不崩掉 SSE 流 ───
            logger.exception("Agent 流式处理异常: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'content': f'Agent 处理异常: {str(e)[:200]}'}, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

        # ─── SSE 流结束，保存消息到会话历史（P1）───
        try:
            # 读取最终状态中的消息
            state = await agent.aget_state(
                {
                    "configurable": {"thread_id": thread_id},
                }
            )
            logging.warning(
                "[DIAG] SSE 结束, state=%s, has_values=%s",
                type(state).__name__ if state else None,
                hasattr(state, "values") if state else False,
            )
            if state and hasattr(state, "values"):
                all_msgs = state.values.get("messages", [])
                logger.warning(
                    "[DIAG] 消息数=%d, 用户=%s, 会话=%s",
                    len(all_msgs),
                    user_id,
                    session_id,
                )
                all_msgs = state.values.get("messages", [])
                # 提取人类可读的消息（只保留 user / assistant / tool 角色的核心信息）
                history = []
                for msg in all_msgs:
                    role = getattr(msg, "type", "unknown")
                    if role == "human":
                        role = "user"
                    content = str(msg.content)[:2000] if msg.content else ""
                    # 去掉 user 消息中的 path_hint 前缀
                    if role == "user" and "\n\n" in content:
                        parts = content.rsplit("\n\n", 1)
                        content = (
                            parts[-1].strip() if len(parts) > 1 else parts[0].strip()
                        )
                    entry = {
                        "id": getattr(msg, "id", None),
                        "role": role,
                        "content": content,
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
                title = (
                    body.message[:50] + ("..." if len(body.message) >= 50 else "")
                    if body.message
                    else "新会话"
                )

                if item is not None:
                    data = item.value
                    data["message_count"] = len(history)
                    data["updated_at"] = now_ts
                    old_title = data.get("title", "")
                    # 覆盖旧的 path_hint 标题，或首次设置标题
                    if (
                        old_title.startswith("沙箱 ID")
                        or old_title in ("新会话", "新对话")
                        or not old_title
                    ):
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
                    ids = (
                        idx_data.get("items", []) if isinstance(idx_data, dict) else []
                    )
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
                logger.warning(
                    "[DIAG] 会话保存完成: user=%s, session=%s, msgs=%d",
                    user_id,
                    session_id,
                    len(history),
                )
        except Exception as e:
            logger.warning("保存会话消息失败（非关键错误）: %s", e)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
