import json
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from src.infra.sandbox import create_sandbox
from src.services.agent import create_agent
from src.core.mcp import get_mcp_tools
from src.api.deps import get_store, get_checkpointer


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
    thread_id = f"{body.user_id}:{body.session_id}"

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

    async def event_stream():
        async for mode, data in agent.astream(
            {"messages": [{"role": "user", "content": body.message}]},
            config={"configurable": {"thread_id": thread_id}},
            # stream_mode=["updates", "messages", "custom"],
            stream_mode=["messages"],
        ):
            if (
                mode == "messages"
            ):  # mode == "messages" 时，data 是 (AIMessageChunk, metadata) 的元组
                token, _ = data
                content = token.content if hasattr(token, "content") else ""
                if content:
                    yield f"data: {json.dumps({'type': 'token', 'content': content}, ensure_ascii=False)}\n\n"
            # elif mode == "updates":
            #     yield f"data: {json.dumps({'type': 'node_update'}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
