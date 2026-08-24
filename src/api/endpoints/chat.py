import asyncio
import json
import os
import re
import logging
import psycopg
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


async def _aget_state_with_retry(agent, thread_id: str, max_attempts: int = 3):
    """带重试地读取 agent 最终状态（防御性）。

    断连兜底路径（reason="interrupted"）下，agent 图可能仍在写 checkpoint。
    原单连接模式会在此撞 "another command is already in progress" 而静默丢消息
    （server.log 16:30:09 checkpointer.aget_tuple 失败）。底层已换连接池后该碰撞
    不再发生，此处仍加短暂重试作为纵深防御，避免瞬时争用直接丢本轮消息。
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await agent.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
        except (
            psycopg.OperationalError,
            psycopg.InterfaceError,
            psycopg.ProgrammingError,
        ) as e:
            last_exc = e
            logger.warning(
                "[DIAG] aget_state 第 %d/%d 次失败（%s），重试...",
                attempt, max_attempts, e,
            )
            await asyncio.sleep(0.2 * attempt)
    logger.error(
        "aget_state 重试 %d 次仍失败，放弃读取最终状态: %s",
        max_attempts, last_exc,
    )
    raise last_exc


def _tool_intent_sig(tool_name: str, result_str: str) -> str:
    """提取工具调用意图指纹：工具名 + 结果内容的关键特征。

    用于 P0 无进展检测——判断模型是否在重复执行同一件事。
    - execute：取结果前 60 字符（命令相同 → 结果通常相同，指纹稳定）
    - 其他工具：取结果前 80 字符
    结果为空时退化为纯工具名（同工具重复也算重复意图）。
    """
    s = (result_str or "").strip()
    if tool_name == "execute":
        return f"{tool_name}:{s[:60]}"
    return f"{tool_name}:{s[:80]}" if s else tool_name


def _missing_skill_artifacts(
    report_root: str,
    user_id: str,
    session_id: str,
    stage1_done: bool,
    stage3_done: bool,
) -> list[str]:
    """确定性校验 skill 工作流关键产物（M3 扩展，2026-08-19）。

    本轮跑过 run_pdf_diff_stage1/stage3（成功）后，reports 必须出现对应产物
    （agent 用 download_from_sandbox 拉回）。缺失即视为"流程未走完"，
    由 M3 完成门注入收敛提示继续，不信任模型自陈"做完了"。

    Returns: 缺失项描述列表（空 = 齐）。
    """
    missing: list[str] = []
    rp = os.path.join(report_root, user_id, session_id)
    if stage1_done and not os.path.isfile(os.path.join(rp, "diff_pages.json")):
        missing.append(
            "diff_pages.json（run_pdf_diff_stage1 产物，需 download_from_sandbox 拉回再读）"
        )
    if stage3_done:
        try:
            has_report = any(
                n.startswith("技术协议差异对比报告_")
                and n.endswith((".md", ".html"))
                for n in os.listdir(rp)
            ) if os.path.isdir(rp) else False
        except OSError:
            has_report = False
        if not has_report:
            missing.append(
                "技术协议差异对比报告_*（run_pdf_diff_stage3 产物，需 download_from_sandbox 拉回）"
            )
    return missing


class ToolLoopAbortError(Exception):
    """工具连续失败超限，提前终止本轮流式生成。

    与 GraphRecursionError 的区别：后者要烧满 recursion_limit（100 步）才抛，
    期间模型会反复空转；本异常在连续失败达到阈值时立刻抛出，
    由 _drain_astream 的 except 分支捕获后转为 SSE error 事件，秒级止损。
    """


class NoProgressAbortError(Exception):
    """工具"成功但无进展"循环检测，中断当前轮并触发收敛注入。

    与 ToolLoopAbortError 的区别：那个管"工具连续失败"（错误循环），
    本异常管"工具全部成功但模型反复执行同一意图、零新交付物"的空转
    （2026-08-17/08-18 两次 Recursion limit 死循环实证：execute 全 OK、
    报告已生成，但模型不断"重新构建 diff.json"永不收敛）。
    由外层捕获后注入收敛 SystemMessage，让模型收敛交付而非烧满 200 步。
    """


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


def _sanitize_generated_path(file_path: str) -> str:
    """清洗工具返回的文件路径（对称前端 deriveGeneratedFiles 清洗，2026-08-24 根治脏路径）。

    上游（模型/技能工具）返回的 /reports/ 路径可能尾随单引号/空白等脏字符——
    2026-08-24 实测 tech-spec-pdf-diff 返回 `.../diff.json'`（尾随单引号）：
    磁盘上根本不存在该文件 → 后端 emit file_generated → 前端 HEAD 探测 404
    → 红框"文件不可用"卡，连累整组报告卡片的预览/下载观感。
    前端 deriveGeneratedFiles 只过滤 `file_path.endsWith("/")`，漏掉尾随引号，
    故在后端 emit 前统一清洗（去首尾引号/空白）。清洗后再走磁盘查找/去重/emit，
    `diff.json'` 自动归正为 `diff.json`（磁盘存在 → 正常预览下载，且与真实条目去重）。
    """
    if not file_path:
        return file_path
    return file_path.strip().strip("'\" \t")


def _merge_disk_diff_into_generated(
    generated_files: list,
    disk_files: frozenset[str],
    report_root: str,
    user_id: str,
    session_id: str,
) -> list:
    """磁盘差集补全 generated_files（2026-08-21 根治）。

    背景：_generated_files 来自模型/工具返回的 file_path 解析，可能脏——模型把
    file_path 传成目录（漏文件名）时无法产出正确 file_name；而 snapshot_report_files
    的磁盘差集（_new_files）是 report_root 下的真实文件名，是唯一可靠来源。
    chat.py 旧实现算出差集却只用于 M3 完成门，保存消息时只用脏源 → 350e5f80 会话
    全卡片 (未知文件)。此处按 file_path 去重，缺的用磁盘真实文件补齐。
    """
    result = list(generated_files)
    base = os.path.join(report_root, user_id, session_id)
    for rel in disk_files:
        disk_full = os.path.join(base, rel)
        if not os.path.isfile(disk_full):
            continue  # snapshot 含子目录名，只补文件
        fp = f"/reports/{user_id}/{session_id}/{rel}"
        if any(g.get("file_path") == fp for g in result):
            continue  # 已有（工具解析正确）保留原项，不重复
        try:
            size = os.path.getsize(disk_full)
        except OSError:
            size = 0
        file_name = rel.rsplit("/", 1)[-1]
        result.append(
            {
                "file_name": file_name,
                "file_path": fp,
                "file_size": size,
                "file_type": _infer_file_type(file_name),
            }
        )
    return result


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

    async def _persist_session(
        agent,
        thread_id: str,
        user_id: str,
        session_id: str,
        store,
        body,
        *,
        generated_files: list | None = None,
        disk_files: frozenset[str] | None = None,
        completion_blocked: bool = False,
        reason: str = "normal",
    ) -> int:
        """把 agent checkpoint 中的消息保存到会话历史（store）。

        由 event_stream 两处调用：
        - 正常路径（reason="normal"）：SSE 流完整结束
        - 断连/取消路径（reason="interrupted"）：生成器被 GeneratorExit/CancelledError
          打断时在 finally 中强制保存，防止用户刷新/关闭页面后本轮消息丢失
          （2026-08-11 15:26 实测：断连后"写 skill"指令未保存，前端重拉即消失）。
        函数内部只有 await 没有 yield，可在 finally 块中安全调用。
        """
        generated_files = generated_files or []
        if disk_files:
            # 磁盘差集补全：工具解析的 generated_files 可能脏（模型把 file_path 传成
            # 目录），用 report_root 磁盘真实文件名补缺（2026-08-21 根治）
            generated_files = _merge_disk_diff_into_generated(
                generated_files, disk_files,
                settings.report_root, user_id, session_id,
            )
        try:
            # 读取最终状态中的消息（断连兜底路径下 agent 图可能仍在写 checkpoint，
            # 用带重试的读取防御瞬时争用导致的 "another command is already in progress"）
            state = await _aget_state_with_retry(agent, thread_id)
            logging.warning(
                "[DIAG] %s: state=%s, has_values=%s",
                "SSE 结束" if reason == "normal" else "SSE 中断强制保存",
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

                # 循环结束后，将 generated_files 关联到合适的 AI 消息
                # 策略：优先附加到第一条有 tool_calls 的 AI 消息（与流式阶段一致——
                #  file_generated 事件在 tool_call 后立即到达，前端把文件卡片加到
                #  当前最后一条 AI 消息，也就是发起 tool_call 的那条）。如果没有任何
                #  AI 消息带 tool_call（比如文件由其他方式生成），fallback 到最后一条
                #  AI 消息。
                if generated_files:
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
                        history[target_idx]["generated_files"] = list(generated_files)

                # M3 完成门：零产出时在最后一条 AI 消息上标记失败状态，
                # 前端可据此展示"未产出交付物"徽标，避免"状态不明"
                if completion_blocked:
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
                    "[DIAG] %s: user=%s, session=%s, msgs=%d",
                    "会话保存完成" if reason == "normal" else "断连强制保存完成",
                    user_id,
                    session_id,
                    len(history),
                )
                return len(history)
        except Exception as e:
            logger.error(
                "保存会话消息失败（关键错误，本轮消息可能丢失）: %s", e, exc_info=True
            )
        return 0

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
        # 已推送 file_generated 事件的 file_path 集合（同会话内同一文件只推一次，
        # 覆盖写/重复 write_file 不再触发重复卡片——2026-08-11 16:00 实测重复报告）
        _emitted_files: set[str] = set()
        # 工具连续失败计数（跨 astream 轮共享声明，实际每轮在 _drain_astream 内重置）
        _consecutive_tool_failures = 0
        # M3 完成门：astream 前快照 /reports/ 目录，结束后做差集判定本轮产出
        _before_files = snapshot_report_files(settings.report_root, user_id, session_id)
        # 每轮 after-before 差集（磁盘真实文件名）；断连路径（finally）也可能引用，
        # 故在循环前初始化，避免 GeneratorExit 早抛时 NameError（2026-08-21）
        _new_files: frozenset[str] = frozenset()
        _completion_blocked = False
        # graph_input 是 chat() 外层变量；event_stream 内若要重新赋值（完成门自动
        # 继续轮注入 SystemMessage），必须用独立局部变量 _graph_input，否则
        # Python 会把 graph_input 判定为 event_stream 局部变量，首轮读取即
        # UnboundLocalError（2026-08-04 M3 重构回归，已修复）
        _graph_input = graph_input

        # ─── 流式生成（多轮共用：首轮 + 完成门自动继续轮）───
        # P0 无进展循环检测状态（跨轮共享，收敛注入后重置）：
        # _last_tool_sig / _repeat_count：连续同一工具意图计数；
        # _files_in_window：本轮重复窗口内是否有新交付物（有则不算空转）
        _last_tool_sig = None
        _repeat_count = 0
        _files_in_window = 0
        _no_progress_injections = 0
        async def _drain_astream(_input):
            """消费一轮 agent.astream，实时 yield SSE 事件；内部吞掉异常不崩流。
            M3-v2 继续轮传入 {'messages': [SystemMessage(...)]} 追加系统消息，
            依赖 deepagents astream 继续模式（spike 验证，未通过时自动继续保持关闭）。
            """
            nonlocal _last_debug_step, thinking_emitted, _think_buffer, _think_done, _generated_files, _consecutive_tool_failures
            nonlocal _last_tool_sig, _repeat_count, _files_in_window, _no_progress_injections, _graph_input, _no_progress_triggered
            _consecutive_tool_failures = 0  # 每轮 astream 重新计数（完成门继续轮独立统计）
            # ─── 清洗：messages 里不应有 SystemMessage ───
            # 主 system 由 deepagents 的 system_message 字段承载（factory 最内层
            # 前置 + memory middleware append_to_system_message），messages 中出现
            # system 均为异常数据：历史 checkpoint 可能残留旧版 P0 收敛注入的
            # SystemMessage（2026-08-19 09:44 旧代码注入后被保存进 state，09:57
            # 重启恢复后第一轮 model 调用即 400 'System message must be at the
            # beginning'——vLLM 要求 system 连续位于开头）。统一过滤掉。
            _raw_msgs = _input.get("messages", [])
            _filtered_system = sum(
                1 for m in _raw_msgs if getattr(m, "type", "") == "system"
            )
            if _filtered_system:
                logger.warning(
                    "[DIAG] astream 输入已清洗 %d 条中间 SystemMessage（checkpoint 残留）",
                    _filtered_system,
                )
            _input = {
                **_input,
                "messages": [
                    m for m in _raw_msgs if getattr(m, "type", "") != "system"
                ],
            }
            # 使用 try/except 保护，防止 agent.astream 内部异常导致 SSE 流中断
            try:
                async for mode, data in agent.astream(
                    _input,
                    config={
                        **graph_config,
                        "recursion_limit": 200,  # ← 最多 200 步（原 100）：大文档任务（80+页×2 PDF 逐章节提取）实测 100 步不够，报告都没来得及写；正常流程 25-35 步，200 是 5-8 倍余量
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

                            # ─── model 节点可观测性日志：AI 回复/思考摘要 + 工具调用列表 ───
                            # 屏蔽 langgraph print 后，模型输出不再出现在日志（原 values/updates
                            # 快照）；这里补一条精简日志，每步 model 一次（2026-08-13 补）
                            if node_name == "model" and hasattr(last_msg, "content"):
                                _ai_content = str(getattr(last_msg, "content", ""))
                                _tool_names = [
                                    tc.get("name", "?")
                                    for tc in (getattr(last_msg, "tool_calls", None) or [])
                                ]
                                logger.warning(
                                    "[DIAG] model: content=%s tool_calls=%s",
                                    (_ai_content or "(空)")[:150],
                                    _tool_names or [],
                                )

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
                                # 结构化提取：MCP 工具返回 list[{'type':'text','text':...}]，
                                # 直接 str() 会得到 Python repr（单引号），json.loads 会失败，
                                # 进而误判 is_error（曾因 "error":null 命中 error 关键词把成功标为失败）
                                raw = last_msg.content
                                candidate = None
                                if isinstance(raw, list) and raw:
                                    first = raw[0]
                                    if isinstance(first, dict) and first.get("text"):
                                        candidate = first["text"]
                                elif isinstance(raw, dict) and raw.get("text"):
                                    candidate = raw["text"]
                                elif isinstance(raw, str):
                                    candidate = raw
                                content_str = str(candidate) if candidate else ""
                                is_error = False
                                if content_str:
                                    # 优先尝试 JSON 解析：MCP 工具返回结构化 JSON 带 success 字段
                                    try:
                                        parsed = json.loads(content_str)
                                        if isinstance(parsed, dict) and "success" in parsed:
                                            is_error = not parsed["success"]
                                        # 结构化 JSON 不走关键词匹配
                                    except (json.JSONDecodeError, TypeError):
                                        # 非 JSON 内容，退回到精确关键词匹配（去掉了宽泛的 "error"）
                                        lower = content_str.lower()
                                        if tool_name == "execute":
                                            is_error = (
                                                "command failed with exit code" in lower
                                                or content_str.startswith("Execution error:")
                                            )
                                        else:
                                            if tool_name == "read_file":
                                                # read_file 是内容型工具：成功返回全文（带行号），
                                                # 内容里可能含 failed/no such file 等正常文本（2026-08-19
                                                # 实测：tech-spec-pdf-diff/SKILL.md 第 25 行
                                                # "No such file or directory" 命中旧关键词 → 成功误判 FAIL）。
                                                # deepagents 失败必以 "Error: " 前缀开头
                                                # （middleware/filesystem.py:1094 content=f"Error: {error}"）。
                                                is_error = content_str.startswith(
                                                    ("Error: ", "error: ")
                                                )
                                            else:
                                                is_error = any(
                                                    marker in lower
                                                    for marker in [
                                                        "exception",
                                                        "traceback",
                                                        "failed",
                                                        "failure",
                                                        "timeout",
                                                        "permission denied",
                                                        "no such file",
                                                    ]
                                                )

                                # ─── tool 节点可观测性日志：工具名 + 成功/失败 + 结果截断 ───
                                # 屏蔽 langgraph print 后工具调用过程不再出现在日志，
                                # 排查时看不到 AI 在调什么工具（2026-08-13 补）
                                logger.warning(
                                    "[DIAG] tool %s → %s | %s",
                                    tool_name,
                                    "OK" if not is_error else "FAIL",
                                    content_str[:150] if content_str else "(空)",
                                )
                                # skill workflow 阶段标记（M3 产物校验用）：
                                # 成功跑过 stage1/stage3 → 后续要求 reports 出现对应产物
                                if not is_error:
                                    if tool_name == "run_pdf_diff_stage1":
                                        _skill_state["stage1"] = True
                                    elif tool_name == "run_pdf_diff_stage3":
                                        _skill_state["stage3"] = True

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

                                # ─── 工具连续失败检测：超限提前终止，防止烧满 recursion_limit ───
                                # 模型在错误循环里空转时（如反复 glob 失败/read 失败），
                                # 每次 ToolMessage 都进 messages 撑大步数，直到 100 步才抛
                                # GraphRecursionError（几分钟白等）。这里数连续失败次数，
                                # 达到阈值立即 raise，由 except 分支转成 SSE error 事件止损。
                                if is_error:
                                    _consecutive_tool_failures += 1
                                    if (
                                        _consecutive_tool_failures
                                        >= settings.sandbox_tool_failure_threshold
                                    ):
                                        logger.warning(
                                            "[M3] 工具连续失败 %d 次（最近: %s），提前终止: user=%s, session=%s",
                                            _consecutive_tool_failures, tool_name, user_id, session_id,
                                        )
                                        raise ToolLoopAbortError(
                                            f"工具连续失败 {_consecutive_tool_failures} 次"
                                            f"（最近一次：{tool_name}），判定任务已无法继续，已提前终止"
                                        )
                                else:
                                    _consecutive_tool_failures = 0

                                # ─── P0 无进展循环检测：工具成功但反复同一意图 ───
                                # 模型在"工具全成功但任务未收敛"时空转（2026-08-17/18 两次
                                # Recursion limit 实证：execute 全 OK、报告已生成，模型仍不断
                                # "重新构建 diff.json"）。连续同一工具意图 + 窗口内零新交付物
                                # → 判定循环 → 抛 NoProgressAbortError 由外层注入收敛提示。
                                if not is_error:
                                    sig = _tool_intent_sig(tool_name, content_str)
                                    if sig == _last_tool_sig:
                                        _repeat_count += 1
                                    else:
                                        _last_tool_sig = sig
                                        _repeat_count = 1
                                    # 重复窗口内出现新交付物 → 重置（不是空转）
                                    if _files_in_window >= settings.no_progress_window_files:
                                        _files_in_window = 0
                                        _repeat_count = 0
                                    if (
                                        _repeat_count >= settings.no_progress_repeat_threshold
                                    ):
                                        if (
                                            _no_progress_injections
                                            < settings.no_progress_max_injections
                                        ):
                                            _no_progress_injections += 1
                                            logger.warning(
                                                "[P0] 无进展循环检测: 工具 %s 连续 %d 次同一意图且无新交付物"
                                                "（最近结果: %s）→ 注入收敛提示",
                                                tool_name, _repeat_count, content_str[:120],
                                            )
                                            raise NoProgressAbortError(
                                                f"检测到无进展循环：工具 {tool_name} 连续 "
                                                f"{_repeat_count} 次重复相同操作且无新产出"
                                            )
                                        else:
                                            # 收敛注入已耗尽仍未收敛 → 直接终止（防烧满 recursion_limit）
                                            logger.warning(
                                                "[P0] 无进展循环收敛注入已耗尽（%d 次），终止: 工具 %s 连续 %d 次同一意图",
                                                _no_progress_injections, tool_name, _repeat_count,
                                            )
                                            raise ToolLoopAbortError(
                                                f"检测到无进展循环：已注入 {_no_progress_injections} 次收敛提示"
                                                f"仍未收敛（最近: {tool_name} 连续 {_repeat_count} 次重复），"
                                                "判定任务无法继续，已提前终止"
                                            )

                                # ─── 检测工具返回的文件信息，提取生成/下载的文件 ───
                                # 不限定工具名，任何返回 /reports/ 路径的工具都能触发
                                file_path_virtual = None
                                if not is_error:
                                    # 1. 结构化解析：MCP 工具（如 download_from_sandbox）返回
                                    #    list[{'type','text'}] 或 dict，text 里是 JSON 字符串；
                                    #    直接用 json.loads 取路径字段，避免正则贪婪匹配吃进 JSON 尾巴
                                    #    （旧实现 re.search(r'/reports/\S+') 会把
                                    #    ","size":15758,"error":null 等尾巴一起吞掉 → 前端 404）
                                    parsed_json = None
                                    raw_content = getattr(last_msg, "content", None)
                                    candidate = None
                                    if isinstance(raw_content, list) and raw_content:
                                        first = raw_content[0]
                                        if isinstance(first, dict) and first.get("text"):
                                            candidate = first["text"]
                                    elif isinstance(raw_content, dict) and raw_content.get("text"):
                                        candidate = raw_content["text"]
                                    elif isinstance(raw_content, str):
                                        candidate = raw_content
                                    if isinstance(candidate, str):
                                        try:
                                            parsed_json = json.loads(candidate)
                                        except (json.JSONDecodeError, TypeError):
                                            parsed_json = None
                                    if isinstance(parsed_json, dict):
                                        for key in ("host_path", "path", "file_path", "output_path"):
                                            val = parsed_json.get(key)
                                            if isinstance(val, str) and val.strip():
                                                file_path_virtual = val.strip()
                                                break
                                    # 2. 回退：非 JSON 输出（write_file 的 "Updated file ..."）用正则硬抠
                                    if not file_path_virtual:
                                        if tool_name in ("write_file", "write", "create_file"):
                                            m = re.search(
                                                r"Updated file\s+(/\S+)", content_str
                                            )
                                            if m:
                                                file_path_virtual = m.group(1)
                                        else:
                                            # [^"\s,}\]]+ 匹配到中文等非分隔符字符为止（\w 不含中文）
                                            m = re.search(
                                                r'/reports/[^"\s,}\]]+', content_str
                                            )
                                            if m:
                                                file_path_virtual = m.group(0).rstrip(
                                                    '"'
                                                ).rstrip("}").rstrip(",")

                                # 规范化文件路径：如果 write_file 返回的路径是 WSL/宿主机完整路径
                                # （如 /mnt/d/.../data/reports/.../file），从中提取 /reports/... 部分
                                if file_path_virtual and not file_path_virtual.startswith("/reports/"):
                                    reports_m = re.search(r'/reports/[^"\s,}\]]+', file_path_virtual)
                                    if reports_m:
                                        file_path_virtual = reports_m.group(0)
                                        logger.debug(
                                            "[DIAG] 文件路径已规范化: path=%s",
                                            file_path_virtual,
                                        )

                                # 清洗 file_path_virtual（对称前端清洗）：上游返回的路径可能
                                # 尾随单引号/空白等脏字符（2026-08-24 实测 .../diff.json'），
                                # 不清洗则磁盘 404 → 前端 HEAD 失败 → 红框"文件不可用"卡。
                                if file_path_virtual:
                                    file_path_virtual = _sanitize_generated_path(file_path_virtual)

                                # 触发条件：/reports/ 交付物 + /skills/__agent__/ 自创 skill
                                # （A1：skill 文件也触发 file_generated，否则前端看不到 skill 卡片
                                #   —— 2026-08-11 15:28 实测 SKILL.md 写成功但前端"文件不可用"）
                                is_report_path = bool(
                                    file_path_virtual
                                    and file_path_virtual.startswith("/reports/")
                                )
                                is_agent_skill = bool(
                                    file_path_virtual
                                    and file_path_virtual.startswith("/skills/__agent__/")
                                )
                                if file_path_virtual and (is_report_path or is_agent_skill):
                                    filename = None
                                    file_size = 0
                                    if is_report_path:
                                        prefix = f"/reports/{user_id}/{session_id}/"
                                        if file_path_virtual.startswith(prefix):
                                            filename = file_path_virtual[len(prefix) :]
                                        # 磁盘大小：report_root → agent_workspace/data/reports 兜底
                                        # （write_file 可能写到了 /mnt/d/... 而不是 /data/myapp/...）
                                        for base in (
                                            settings.report_root,
                                            os.path.join(
                                                settings.agent_workspace, "data", "reports"
                                            ),
                                        ):
                                            try:
                                                disk_path = os.path.join(
                                                    base, user_id, session_id, filename
                                                )
                                                if os.path.isfile(disk_path):
                                                    file_size = os.path.getsize(disk_path)
                                                    break
                                            except Exception:
                                                pass
                                    else:  # /skills/__agent__/
                                        filename = file_path_virtual[
                                            len("/skills/__agent__/") :
                                        ]
                                        try:
                                            disk_path = os.path.join(
                                                settings.agent_workspace,
                                                "skills", "__agent__", filename,
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

                                    # 目录路径不产生交付物：write_file 的 file_path 若为目录
                                    # （漏文件名，如 /reports/uid/sid/ 或 /reports/uid/sid），
                                    # 提取出的 filename 为空/会话ID，emit 后前端收到
                                    # file_name="" 的 file_generated → "(未知文件)" 卡片 +
                                    # 空 URL 请求 HEAD 307/401（2026-08-21 实测 350e5f80 会话；
                                    # 与 agent.py _reject_directory_write 双保险）
                                    if is_report_path and not filename:
                                        logger.warning(
                                            "[FILE_GEN] 忽略目录路径 file_generated: path=%s (无文件名)",
                                            file_path_virtual,
                                        )
                                        continue

                                    # B：同会话内同一文件只推送一次（覆盖写/重复 write_file 去重）
                                    if file_path_virtual in _emitted_files:
                                        # 事件已发过，仅更新 _generated_files 里的大小，不重复推送
                                        for _f in _generated_files:
                                            if _f["file_path"] == file_path_virtual:
                                                _f["file_size"] = file_size
                                                break
                                    else:
                                        _emitted_files.add(file_path_virtual)
                                        _files_in_window += 1  # P0：新交付物计数（非空转信号）
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
            except ToolLoopAbortError as e:
                # ─── 工具连续失败超限：预期内主动终止，不打印堆栈（非程序 bug）───
                # 只向前端发 error 事件（比 GraphRecursionError 快几十步）
                logger.warning("工具连续失败超限提前终止: %s", e)
                yield f"data: {json.dumps({'type': 'error', 'content': f'Agent 处理异常: {str(e)[:200]}'}, ensure_ascii=False)}\n\n"
            except NoProgressAbortError as e:
                # ─── P0 无进展循环：中断本轮，注入收敛提示让模型收敛 ───
                # 触发后 _graph_input 被替换为收敛提示，外层 while 循环继续跑一轮；
                # 收敛轮再触发（超 no_progress_max_injections）则不再注入，
                # 由 except Exception 兜底转 error 终止（防无限收敛轮）。
                # 注意：必须用 HumanMessage 而非 SystemMessage——vLLM 要求所有
                # system 消息连续位于开头，本提示与 checkpoint 恢复的历史消息合并
                # 后不保证位置（2026-08-19 09:44:54 实测 400 'System message must
                # be at the beginning'；资源清单 13dab48→f093303 同款教训）。
                _no_progress_triggered = True  # 标记：收敛轮跳过 M3 零产出拦截
                logger.warning("[P0] 无进展循环，注入收敛提示: %s", e)
                yield f"data: {json.dumps({'type': 'agent_status', 'status': 'no_progress', 'content': f'{str(e)[:200]}'}, ensure_ascii=False)}\n\n"
                # 收敛提示：让模型停止重复，验证已有产出并交付
                _graph_input = {"messages": [HumanMessage(
                    f"系统检测到无进展循环：你已连续多轮重复相同操作（{str(e)[:150]}）"
                    "且未产生新交付物。请立即收敛："
                    "1) 检查 /reports/{user_id}/{session_id}/ 下是否已有可交付的报告文件，"
                    "   如有则直接确认交付，停止重新生成；"
                    "2) 若确需重跑，先说明与上一轮的具体差异，一次完成，不要重复相同命令；"
                    "3) 若无法收敛，明确向用户说明卡点和已完成的产出。"
                )]}
                # 重置无进展计数（收敛轮重新统计），基线重设
                _last_tool_sig = None
                _repeat_count = 0
                _files_in_window = 0
                _before_files = snapshot_report_files(settings.report_root, user_id, session_id)
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
        # skill workflow 阶段标记（M3 产物校验用）：本轮是否成功跑过 stage1/stage3
        _skill_state = {"stage1": False, "stage3": False}
        # P0：NoProgressAbortError 触发标记——收敛轮跳过 M3 零产出拦截
        # （收敛轮刚注入提示，本轮零产出是预期的，不应被完成门拦截）
        _no_progress_triggered = False
        # 断连强制保存：正常路径保存成功后置 True；流被 GeneratorExit/CancelledError
        # 打断时 finally 兜底强制保存（2026-08-11 15:26 断连丢"写 skill"指令的修复）
        _saved = False
        try:
            while True:
                async for _ev in _drain_astream(_graph_input):
                    yield _ev
                _after_files = snapshot_report_files(settings.report_root, user_id, session_id)
                _new_files = _after_files - _before_files
                if _no_progress_triggered:
                    # 收敛轮：跳过零产出拦截，直接继续下一轮（_graph_input 已换成收敛提示）
                    _no_progress_triggered = False
                    _before_files = _after_files  # 重新基线
                    continue
                if _generated_files or _new_files:
                    # 有本轮产出 → 再校验 skill 工作流关键产物（跑过 stage1/stage3 时）。
                    # 缺失即"流程没走完"（如 stage1 产物没拉回 reports），不放行，
                    # 注入收敛提示继续——不信任模型自陈"做完了"。
                    _missing = _missing_skill_artifacts(
                        settings.report_root, user_id, session_id,
                        _skill_state["stage1"], _skill_state["stage3"],
                    )
                    if not _missing:
                        break  # 产物齐，放行
                    if (
                        _completion_retries >= settings.sandbox_completion_gate_max_retries
                        or not settings.sandbox_completion_gate_auto_continue
                    ):
                        _completion_blocked = True
                        logger.warning(
                            "[M3] skill 产物缺失且重试耗尽: user=%s, session=%s, missing=%s",
                            user_id, session_id, _missing,
                        )
                        yield f"data: {json.dumps({
                            'type': 'completion_blocked',
                            'reason': f'skill 工作流产物缺失: {_missing}',
                            'hint': '请检查是否遗漏 download_from_sandbox 拉回产物',
                            'terminated': True,
                        }, ensure_ascii=False)}\n\n"
                        break
                    _completion_retries += 1
                    logger.warning(
                        "[M3] skill 产物缺失，注入收敛继续（%d/%d）: missing=%s",
                        _completion_retries, settings.sandbox_completion_gate_max_retries, _missing,
                    )
                    _graph_input = {"messages": [HumanMessage(
                        f"系统校验：本轮 skill 工作流关键产物缺失：{'、'.join(_missing)}。"
                        f"请立即用 download_from_sandbox 将对应文件拉回 /reports/{user_id}/{session_id}/，"
                        "然后继续完成。禁止自写替代代码或跳过流程步骤。"
                    )]}
                    _before_files = _after_files  # 重新基线
                    continue
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
                _graph_input = {"messages": [SystemMessage(
                    f"系统检测：/reports/{user_id}/{session_id}/ 当前无本轮新文件，本轮任务未产出任何交付物。"
                    "请检查是否遗漏 download_from_sandbox / write_file 步骤，并继续完成。"
                )]}
                _before_files = _after_files  # 重新基线
            _saved = True  # 循环正常结束（未被取消/打断），finally 不再兜底保存
        finally:
            # 断连/取消/异常路径：流被中断，正常保存逻辑未执行，强制保存 checkpoint 状态
            if not _saved:
                try:
                    logger.warning(
                        "[DIAG] SSE 流中断（断连/取消），强制保存当前会话: user=%s, session=%s",
                        user_id, session_id,
                    )
                    await _persist_session(
                        agent, thread_id, user_id, session_id, store, body,
                        generated_files=_generated_files,
                        disk_files=_new_files,
                        completion_blocked=_completion_blocked,
                        reason="interrupted",
                    )
                except Exception as e:
                    logger.error(
                        "断连强制保存失败（关键错误，本轮消息可能丢失）: %s",
                        e, exc_info=True,
                    )

        # ─── SSE 流结束，先保存消息到会话历史，再发 [DONE] ───
        # 防止前端 [DONE] 后立即编辑导致竞态（消息尚未落盘 → from_index 越界）
        await _persist_session(
            agent, thread_id, user_id, session_id, store, body,
            generated_files=_generated_files,
            disk_files=_new_files,
            completion_blocked=_completion_blocked,
            reason="normal",
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
