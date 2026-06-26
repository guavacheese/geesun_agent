import json
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from src.infra.sandbox import create_sandbox
from src.services.agent import create_agent
from src.core.mcp import get_mcp_tools
from src.api.deps import get_store, get_checkpointer
from src.core.config import settings


class ChatRequest(BaseModel):
    user_id: str = "default-user"
    session_id: str = "default-session"
    message: str


router = APIRouter()


@router.post("/chat")
async def chat(
    request: Request,
    body: ChatRequest,  # ← FastAPI 自动解析 JSON
    store=Depends(get_store),
    checkpointer=Depends(get_checkpointer),
):
    user_id = body.user_id
    session_id = body.session_id
    thread_id = f"{user_id}:{session_id}"

    tools = await get_mcp_tools()
    sandbox = create_sandbox(thread_id)
    # 从 lifespan 预加载的 skills 缓存获取
    skills = getattr(request.app.state, "skills", [])
    agent = await create_agent(
        user_id=body.user_id,
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
        f"需要把解密后的文件传入沙箱时，使用 MCP 工具 decrypt_and_upload_to_sandbox\n"
        f"  sandbox_id 参数填以上 ID，remote_path 建议用 /home/user/文件名\n"
        f"\n"
        f"【当前会话路径——严格遵守】\n"
        f"你的输入文件（PLC源码、Excel等）：/uploads/{user_id}/{session_id}/\n"
        f"你的报告输出目录：/reports/{user_id}/{session_id}/\n"
        f"\n"
        f"MCP 工具：\n"
        f"  decrypt_and_upload_to_sandbox → 解密并上传到沙箱（推荐，不经过 LLM 上下文）\n"
        f"  upload_to_sandbox → 上传不需解密的文件（XML/TXT）到沙箱（不经过 LLM 上下文）\n"
        f"  copy_script_to_sandbox → 直传 skill 脚本到沙箱（推荐，不经过 LLM 上下文）\n"
        f"  download_from_sandbox → 从沙箱下载报告到 /reports/（推荐，不经过 LLM 上下文）\n"
        f"  decrypt_file_to_base64 → 已废弃，仅小文件回溯兼容\n"
        f"  使用物理路径：{settings.upload_root}/{user_id}/{session_id}/\n"
        f"\n"
        f"【关键规则：文件操作 ≠ 命令执行】\n"
        f"- read_file、write_file、ls、grep、glob 只能操作 /uploads/、/reports/、/workspace/memories/ 等虚拟路径\n"
        f"- write_file 不能写 /home/user/（那是沙箱路径，write_file 写不进去！）\n"
        f"- 往沙箱写文件只能用 MCP 工具：decrypt_and_upload_to_sandbox（加密文件）或 upload_to_sandbox（非加密文件）\n"
        f"- execute 只能运行命令，沙箱内无法访问 /uploads/ 和 /reports/\n"
        f"\n"
        f"【绝对不要做的事】\n"
        f"- 不要用 execute ls/find 在沙箱里查找文件（沙箱是空的）\n"
        f"- 文件只存在于 /uploads/ 和 /reports/ 下，用 ls/glob/read_file 访问\n"
        f"- execute 只用于运行 Python 脚本，不用于文件搜索\n"
        f"\n"
        f"【致命错误（严格禁止）】\n"
        f"-ls 和 glob 的结果就是真实的文件列表。不要用 execute 去验证文件存在与否，\n"
        f"-execute 在沙箱里运行，看不到 /uploads/ 和 /reports/ 下的文件。\n"
        f"-glob  说文件在，文件就在。你重复用 execute 验证一次，就浪费一次 LLM 调用。\n"
    )

    async def event_stream():
        async for mode, data in agent.astream(
            {"messages": [{"role": "user", "content": f"{path_hint}\n{body.message}"}]},
            config={
                "configurable": {"thread_id": thread_id},
                "recursion_limit": 60,  # ← 最多 60 步，超了直接报错而不是卡死
            },
            # stream_mode=["updates", "messages", "custom"],
            stream_mode=["messages", "updates"],
            # stream_mode=["messages"],
        ):
            if (
                mode == "messages"
            ):  # mode == "messages" 时，data 是 (AIMessageChunk, metadata) 的元组
                token, _ = data
                content = token.content if hasattr(token, "content") else ""
                if content:
                    yield f"data: {json.dumps({'type': 'token', 'content': content}, ensure_ascii=False)}\n\n"
            elif mode == "updates":
                yield f"data: {json.dumps({'type': 'node_update'}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
