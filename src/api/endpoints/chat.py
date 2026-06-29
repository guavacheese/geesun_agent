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
    model_override: dict | None = None  # 可选，动态切换模型：{"model_name": "...", "base_url": "...", "api_key": "..."}


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
    # 构建 skill sources：系统 + Agent 自创（启动时加载）+ 当前用户的共享 skill
    base_skills = getattr(request.app.state, "skills", [])
    user_skill_path = f"/skills/__user_{user_id}__/"
    skills = list(base_skills) + [user_skill_path]
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
        f"\n"
        f"【当前会话路径】\n"
        f"输入文件：/uploads/{user_id}/{session_id}/\n"
        f"报告输出：/reports/{user_id}/{session_id}/\n"
    )

    async def event_stream():
        # 如果传了 model_config，通过 runtime context 传给 switch_model middleware
        invoke_kwargs = {}
        if body.model_override:
            invoke_kwargs["context"] = {"model_config": body.model_override}

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
                token, _ = data
                content = token.content if hasattr(token, "content") else ""
                if content:
                    yield f"data: {json.dumps({'type': 'token', 'content': content}, ensure_ascii=False)}\n\n"
            elif mode == "updates":
                yield f"data: {json.dumps({'type': 'node_update'}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
