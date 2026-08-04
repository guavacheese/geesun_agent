import json
import os
import re
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from src.infra.sandbox import create_sandbox, get_env_snapshot
from src.infra.reports import snapshot_report_files
from src.services.agent import create_agent
from src.core.mcp import get_mcp_tools
from src.api.deps import get_store, get_checkpointer, get_current_user
from src.core.config import settings

logger = logging.getLogger(__name__)


# 与前端 lib/types.ts inferFileType 保持一致
# 用于保存 generated_files 时填充 file_type 字段
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
    mcp_servers: list[str] | None = (
        None  # 可选，本轮临时启用的 MCP 服务名列表（缺省 = 全部 enabled）
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

    # 按本轮透传的 mcp_servers 过滤 MCP 工具（缺省 = 全部 enabled）
    tools = await get_mcp_tools(body.mcp_servers)
    sandbox = create_sandbox(thread_id)
    logger.info("[DIAG] create_sandbox(thread_id=%s) → sandbox=%s", thread_id, type(sandbox).__name__ if sandbox else "None")

    # ─── M1 环境预检：快照注入 + 磁盘硬阈值拒绝（设计文档 M1）───
    # 快照缓存按 thread_id 60s 复用；无沙箱（本地模式）时 env_snapshot 为 None，跳过
    env_snapshot = get_env_snapshot(sandbox, thread_id)
    if env_snapshot is not None and env_snapshot.ok:
        disk = env_snapshot.disk_avail_mb
        if disk is not None and disk < settings.sandbox_disk_hard_mb:
            logger.warning(
                "[M1] 沙箱磁盘不足拒绝启动: user=%s, session=%s, avail=%sMB",
                user_id, session_id, disk,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    f"沙箱磁盘可用空间不足（{disk}MB < {settings.sandbox_disk_hard_mb}MB），"
                    "请清理沙箱后重试"
                ),
            )
        if disk is not None and disk < settings.sandbox_disk_warn_mb:
            logger.warning("[M1] 沙箱磁盘偏低: user=%s, session=%s, avail=%sMB", user_id, session_id, disk)
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

    # M1：环境快照注入（系统自动探测，模型不应自行重装/探测环境）
    if env_snapshot is not None:
        path_hint += (
            f"\n【沙箱环境】（系统自动探测，仅需直接使用，勿重复安装或探测）\n"
            f"{env_snapshot.to_hint()}\n"
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
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

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
                    stored_reasoning = m.get("reasoning", "")
                    ai_kwargs = {}
                    if stored_reasoning:
                        ai_kwargs["reasoning_content"] = stored_reasoning
                    lc_msgs.append(AIMessage(
                        content=m.get("content", ""),
                        additional_kwargs=ai_kwargs,
                    ))
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
        # Qwen 系模型将推理放在 content 的 </think> 前，流式场景下可能跨 chunk 截断
        _think_buffer = ""
        _think_done = False
        # [DEBUG] 记录上一个 langgraph_step，避免逐 token 重复打印
        _last_debug_step = None
        # 记录本次流式生成过程中产生/下载的文件
        # 后续保存到 store 时关联到对应的 AI 消息 entry，确保刷新页面后还能看到文件卡片
        _generated_files: list[dict] = []
        # M3 完成门：astream 前快照 /reports/ 目录，结束后做差集判定本轮产出
        _before_files = snapshot_report_files(settings.report_root, user_id, session_id)
        _completion_blocked = False

        # ─── 流式生成（多轮共用：首轮 + 完成门自动继续轮）───
        async def _drain_astream(_graph_input):
            """消费一轮 agent.astream，实时 yield SSE 事件；内部吞掉异常不崩流。
            M3-v2 继续轮传入 {'messages': [SystemMessage(...)]} 追加系统消息，
            依赖 deepagents astream 继续模式（spike 验证，未通过时自动继续保持关闭）。
            """
            nonlocal _last_debug_step, thinking_emitted, _think_buffer, _think_done, _generated_files
            # 使用 try/except 保护，防止 agent.astream 内部异常导致 SSE 流中断
            try:
                async for mode, data in agent.astream(
                    _graph_input,
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
                            node = metadata.get("langgraph_node")
                            # 只在 model 节点发送 ai_message_start（避免 tool 节点空触发）
                            if step is not None and step != _last_debug_step and node == "model":
                                # 新 step 到来前先通知前端：上一条 AI 消息结束
                                # 这是关键：让前端能正确分割多条 AIMessage
                                if _last_debug_step is not None:
                                    yield f"data: {json.dumps({'type': 'ai_message_start'}, ensure_ascii=False)}\n\n"
                                _last_debug_step = step
                                thinking_emitted = False  # 新 step → 重置 thinking 标记
                                # 关键：每条 AIMessage 独立切分 <think> 段
                                # 之前 _think_done 跨消息保留导致第二条 AIMessage 的
                                # <think> 段（被 langchain 拼在 ToolMessage 之后）无法切分
                                _think_done = False
                                _think_buffer = ""
                                logger.warning(
                                    "[DIAG] new messages stream: step=%s, node=%s, metadata keys=%s",
                                    step,
                                    metadata.get("langgraph_node"),
                                    list(metadata.keys()),
                                )

                        # ─── 流式 token 处理：分离推理内容与回复内容 ───
                        content = token.content if hasattr(token, "content") else ""

                        # 1. 优先从 additional_kwargs 提取推理内容（DeepSeek/Groq/Ollama/XAI 等）
                        reasoning = ""
                        if hasattr(token, "additional_kwargs") and token.additional_kwargs:
                            reasoning = (
                                token.additional_kwargs.get("reasoning_content") or ""
                            )

                        if reasoning:
                            # 模型通过 API 字段返回推理内容（不经过 content）
                            if not thinking_emitted:
                                thinking_emitted = True
                                yield f"data: {json.dumps({'type': 'agent_status', 'status': 'thinking'}, ensure_ascii=False)}\n\n"
                            yield f"data: {json.dumps({'type': 'reasoning', 'content': reasoning}, ensure_ascii=False)}\n\n"
                        elif not _think_done and content:
                            # 2. Qwen 系：仅以 </think> 作为推理结束标记（无开始标签）
                            _think_buffer += content
                            think_end = _think_buffer.find("</think>")
                            if think_end >= 0:
                                _think_done = True
                                # thinking_part 取 </think> 之前的内容；
                                # remaining 是 </think> 之后 8 字符（跳过标签）开始的内容
                                thinking_part = _think_buffer[:think_end]
                                remaining = _think_buffer[think_end + 8 :]
                                # 去掉 remaining 开头的换行
                                if remaining.startswith("\n"):
                                    remaining = remaining[1:]
                                if thinking_part.strip():
                                    if not thinking_emitted:
                                        thinking_emitted = True
                                        yield f"data: {json.dumps({'type': 'agent_status', 'status': 'thinking'}, ensure_ascii=False)}\n\n"
                                    yield f"data: {json.dumps({'type': 'reasoning', 'content': thinking_part}, ensure_ascii=False)}\n\n"
                                if remaining:
                                    yield f"data: {json.dumps({'type': 'token', 'content': remaining}, ensure_ascii=False)}\n\n"
                        elif content:
                            # 3. 正常 token：已过 </think> 或无推理内容
                            if not thinking_emitted:
                                thinking_emitted = True
                                yield f"data: {json.dumps({'type': 'agent_status', 'status': 'thinking'}, ensure_ascii=False)}\n\n"
                                yield f"data: {json.dumps({'type': 'token', 'content': content}, ensure_ascii=False)}\n\n"
                            else:
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

                                # 规范化文件路径：如果 write_file 返回的路径是 WSL/宿主机完整路径
                                # （如 /mnt/d/.../data/reports/.../file），从中提取 /reports/... 部分
                                if file_path_virtual and not file_path_virtual.startswith("/reports/"):
                                    reports_m = re.search(r'/reports/[\w/.-]+', file_path_virtual)
                                    if reports_m:
                                        file_path_virtual = reports_m.group(0)
                                        logger.debug(
                                            "[DIAG] 文件路径已规范化: path=%s",
                                            file_path_virtual,
                                        )

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
                                            else:
                                                # 如果 report_root 路径不存在，尝试 agent_workspace 下的 data 目录
                                                # （write_file 可能写到了 /mnt/d/... 而不是 /data/myapp/...）
                                                wsl_path = os.path.join(
                                                    settings.agent_workspace,
                                                    "data", "reports",
                                                    user_id, session_id, filename,
                                                )
                                                if os.path.isfile(wsl_path):
                                                    file_size = os.path.getsize(wsl_path)
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
                                                            'file_type': _infer_file_type(
                                                                file_path_virtual.split('/')[-1]
                                                            ),
                                                        },
                                                        ensure_ascii=False,
                                                    )
                                                }\n\n"
                                        # 同时记录到 _generated_files 列表，保存时关联到对应 AI 消息
                                        _generated_files.append({
                                            "file_name": file_path_virtual.split('/')[-1],
                                            "file_path": file_path_virtual,
                                            "file_size": file_size,
                                            "file_type": _infer_file_type(
                                                file_path_virtual.split('/')[-1]
                                            ),
                                        })
            except Exception as e:
                # ─── 异常保护：任何 agent.astream 内的异常都被捕获，不崩掉 SSE 流 ───
                logger.exception("Agent 流式处理异常: %s", e)
                yield f"data: {json.dumps({'type': 'error', 'content': f'Agent 处理异常: {str(e)[:200]}'}, ensure_ascii=False)}\n\n"


        # ─── M3 完成门（设计文档）：系统校验本轮 /reports/ 产出，零产出不放行结束 ───
        # 判定不信任模型自陈：_generated_files（流式识别）与磁盘差集双保险，
        # 两者皆空即视为"任务未产出任何交付物"。
        # v2 自动继续：注入 SystemMessage 让模型再跑一轮（受 auto_continue / max_retries 开关控制，
        # 异常由 _drain_astream 内部吞掉 → 本轮零产出仍会递增重试，超限终止）。
        _completion_retries = 0
        while True:
            async for _ev in _drain_astream(graph_input):
                yield _ev
            _after_files = snapshot_report_files(settings.report_root, user_id, session_id)
            _new_files = _after_files - _before_files
            if _generated_files or _new_files:
                break  # 有本轮产出，放行
            if not settings.sandbox_completion_gate_enabled:
                break  # 完成门关闭（回滚开关）
            if (
                _completion_retries >= settings.sandbox_completion_gate_max_retries
                or not settings.sandbox_completion_gate_auto_continue
            ):
                _completion_blocked = True
                logger.warning(
                    "[M3] 完成门拦截（本轮零产出）: user=%s, session=%s, generated=%d, new_files=%d, retries=%d",
                    user_id, session_id, len(_generated_files), len(_new_files), _completion_retries,
                )
                yield f"data: {json.dumps({
                    'type': 'completion_blocked',
                    'reason': '本轮任务未产出任何交付物（/reports 为空）',
                    'hint': '请检查是否遗漏 download_from_sandbox / write_file 步骤',
                    'terminated': _completion_retries >= settings.sandbox_completion_gate_max_retries,
                }, ensure_ascii=False)}\n\n"
                break
            _completion_retries += 1
            logger.warning(
                "[M3] 零产出，注入 SystemMessage 自动继续（第 %d/%d 次）: user=%s, session=%s",
                _completion_retries, settings.sandbox_completion_gate_max_retries, user_id, session_id,
            )
            graph_input = {"messages": [SystemMessage(
                f"系统检测：/reports/{user_id}/{session_id}/ 当前无本轮新文件，本轮任务未产出任何交付物。"
                "请检查是否遗漏 download_from_sandbox / write_file 步骤，并继续完成。"
            )]}
            _before_files = _after_files  # 重新基线

        # ─── SSE 流结束，先保存消息到会话历史，再发 [DONE] ───
        # 防止前端 [DONE] 后立即编辑导致竞态（消息尚未落盘 → from_index 越界）
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
                # 第一遍：建立 tool_call_id -> ToolMessage content 的映射
                # 用于在保存 AIMessage 的 tool_calls 时填入 result 字段
                tool_results: dict[str, str] = {}
                for m in all_msgs:
                    if getattr(m, "type", None) == "tool":
                        tc_id = getattr(m, "tool_call_id", None)
                        if tc_id:
                            tool_results[tc_id] = str(m.content) if m.content else ""

                # 提取人类可读的消息（只保留 user / assistant / tool 角色的核心信息）
                history = []
                for msg in all_msgs:
                    role = getattr(msg, "type", "unknown")
                    if role == "human":
                        role = "user"
                    content = str(msg.content) if msg.content else ""
                    reasoning = ""

                    # 1. 从 additional_kwargs 提取推理内容（DeepSeek/Groq/Ollama/XAI 等）
                    if hasattr(msg, "additional_kwargs") and msg.additional_kwargs:
                        kw_reasoning = msg.additional_kwargs.get("reasoning_content") or ""
                        if kw_reasoning:
                            reasoning = kw_reasoning

                    # 2. Qwen 系：从 content 中移除 </think> 段
                    # 关键：每条 AI 消息独立切分（之前的 `if not reasoning` 会让后一条
                    # AI 消息的 <think> 段残留到 content 里）
                    if "</think>" in content:
                        think_end = content.find("</think>")
                        thinking_part = content[:think_end]
                        remaining = content[think_end + 8:]  # skip </think>
                        if remaining.startswith("\n"):
                            remaining = remaining[1:]
                        if thinking_part.strip():
                            # 保留 original reasoning（如有），附加本次 <think> 段
                            if reasoning:
                                reasoning = reasoning + "\n\n" + thinking_part
                            else:
                                reasoning = thinking_part
                            content = remaining

                    # 3. 截断长度
                    content = content[:2000]
                    reasoning = reasoning[:2000]

                    # 4. 去掉 user 消息中的 path_hint 前缀
                    if role == "user" and "\n\n" in content:
                        parts = content.rsplit("\n\n", 1)
                        content = (
                            parts[-1].strip() if len(parts) > 1 else parts[0].strip()
                        )

                    # 5. 不再跳过中间 AI 消息
                    #    之前跳过的初衷是避免"只有 thinking 折叠块 + 空正文"的奇怪气泡，
                    #    但这会丢失 tool_call 渲染（流式阶段用户能看到 write_file 的 tool_call
                    #    卡片 + 文件卡片，刷新后这部分消失，体验不一致）。
                    #    现在保留所有 AI 消息：中间 AI 消息显示 thinking + ToolCallCard +
                    #    GeneratedFileCard，最终 AI 消息显示 thinking + 真正的回复内容。

                    entry = {
                        "id": getattr(msg, "id", None),
                        "role": role,
                        "content": content,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    if reasoning:
                        entry["reasoning"] = reasoning
                    # AI 消息附带 tool_calls 信息
                    if role == "ai" and hasattr(msg, "tool_calls") and msg.tool_calls:
                        entry["tool_calls"] = [
                            {
                                "id": tc.get("id") or f"tc-{i}",
                                "tool": tc["name"],
                                "args": tc["args"],
                                "status": "success",
                                "result": (tool_results.get(tc.get("id", "")) or "")[:2000],
                            }
                            for i, tc in enumerate(msg.tool_calls)
                        ]
                    history.append(entry)

                # 循环结束后，将 _generated_files 关联到合适的 AI 消息
                # 策略：优先附加到第一条有 tool_calls 的 AI 消息（与流式阶段一致——
                #  file_generated 事件在 tool_call 后立即到达，前端把文件卡片加到
                #  当前最后一条 AI 消息，也就是发起 tool_call 的那条）。如果没有任何
                #  AI 消息带 tool_call（比如文件由其他方式生成），fallback 到最后一条
                #  AI 消息。
                if _generated_files:
                    target_idx = None
                    for i, e in enumerate(history):
                        if e.get("role") == "ai" and e.get("tool_calls"):
                            target_idx = i
                            break
                    if target_idx is None:
                        for i in range(len(history) - 1, -1, -1):
                            if history[i].get("role") == "ai":
                                target_idx = i
                                break
                    if target_idx is not None:
                        history[target_idx]["generated_files"] = list(_generated_files)

                # M3 完成门：零产出时在最后一条 AI 消息上标记失败状态，
                # 前端可据此展示"未产出交付物"徽标，避免"状态不明"
                if _completion_blocked:
                    for i in range(len(history) - 1, -1, -1):
                        if history[i].get("role") == "ai":
                            history[i]["completion"] = "blocked_no_output"
                            break

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

        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
