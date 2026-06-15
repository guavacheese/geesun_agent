from __future__ import annotations
from urllib.parse import quote_plus

import asyncio
import os
import time
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import (
    CompositeBackend,
    FilesystemBackend,
    LocalShellBackend,
    StateBackend,
    StoreBackend,
)
from dotenv import load_dotenv
from langchain.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from langchain_cubesandbox import CubeSandbox

load_dotenv()

# ─── Environment ──────────────────────────────────────────────────────────────

BASE_URL = os.getenv("BASE_URL")
API_KEY = os.getenv("OPENAI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME")
WORKSPACE = os.getenv("AGENT_WORKSPACE")
UPLOAD_ROOT = os.getenv("UPLOAD_ROOT", "/data/myapp/uploads")
REPORT_ROOT = os.getenv("REPORT_ROOT", "/data/myapp/reports")
MCP_TOKEN = os.getenv("MCP_TOKEN", "YOUR_TOKEN")

# Validate required env vars
for _name, _val in [
    ("BASE_URL", BASE_URL),
    ("API_KEY", API_KEY),
    ("MODEL_NAME", MODEL_NAME),
    ("WORKSPACE", WORKSPACE),
]:
    if not _val:
        raise ValueError(f"Missing required env var: {_name}")

# ─── Model ────────────────────────────────────────────────────────────────────

model = ChatOpenAI(
    base_url=BASE_URL,
    model=MODEL_NAME,
    api_key=API_KEY,
    temperature=0,
    max_retries=5,
    timeout=300,
)

# ─── System Prompt ────────────────────────────────────────────────────────────

system_prompt = """\
You are an elite PLC Code Review Engineer specializing in
Omron NX/NJ series industrial automation systems.

## 核心原则
- 基于证据，不捏造数据；解析失败时报告部分结果
- 保留所有原始标识符（中文/日文/英文）原样输出
- 数据有歧义时，先明确说明假设再下结论

## 重要
所有审查流程、输出格式、报告模板，严格遵循你的 plc-code-auditor Skills 中的规范，
不得自行发明格式。"""


def _user_id_safe(user_id: str) -> str:
    return user_id or "default-user"


# ─── MCP Client Cache ─────────────────────────────────────────────────────────

_cached_tools: list[BaseTool] | None = None


async def mcp_tools() -> list[BaseTool]:
    global _cached_tools
    if _cached_tools is not None:
        return _cached_tools

    try:
        client = MultiServerMCPClient(
            {
                "decrypt-file": {
                    "transport": "streamable-http",
                    "url": "http://localhost:8000/mcp",
                    "headers": {
                        "Authorization": f"Bearer {MCP_TOKEN}",
                        "X-customer-header": "custom-value",
                    },
                }
            }
        )
        _cached_tools = await client.get_tools()
        return _cached_tools
    except Exception:
        return []


# ─── Sandbox Factory ──────────────────────────────────────────────────────────


def _make_sandbox(thread_id: str) -> Any:

    return CubeSandbox.get_or_create(
        template=os.environ["CUBE_TEMPLATE_ID"],
        thread_id=thread_id,
        api_url=os.environ.get("CUBE_API_URL"),
        api_key=os.environ.get("CUBE_API_KEY", "dummy"),
    )


# ─── Backend ──────────────────────────────────────────────────────────────────


def build_backend(
    user_id: str,
    session_id: str,
    thread_id: str,
    store: AsyncPostgresStore,
    sandbox: Any,
) -> CompositeBackend:
    routes: dict[str, Any] = {
        f"/uploads/{user_id}/{session_id}": FilesystemBackend(
            root_dir=f"{UPLOAD_ROOT}/{user_id}/{session_id}/",
            virtual_mode=True,
        ),
        f"/reports/{user_id}/{session_id}": FilesystemBackend(
            root_dir=f"{REPORT_ROOT}/{user_id}/{session_id}/",
            virtual_mode=True,
        ),
        "/memories/": StoreBackend(
            namespace=lambda rt: ("memories", _user_id_safe(user_id)),
            store=store,  # 或者 AsyncPgStore
        ),
    }
    if sandbox:
        routes["/code/"] = sandbox

    return CompositeBackend(
        default=LocalShellBackend(
            root_dir=WORKSPACE,
            virtual_mode=False,
            env={**os.environ},
        ),
        routes=routes,
    )


# ─── Agent Creation ───────────────────────────────────────────────────────────


async def create_my_agent(
    user_id: str,
    session_id: str,
    thread_id: str,
    store: AsyncPostgresStore,
    sandbox: Any,
    checkpointer,
):
    backend = build_backend(
        user_id,
        session_id,
        thread_id,
        store,
        sandbox,
    )
    tools = await mcp_tools()

    agent = create_deep_agent(
        model=model,
        tools=tools,
        backend=backend,
        system_prompt=system_prompt,
        skills=[f"{WORKSPACE}/skills/plc-code-auditor"],
        interrupt_on={
            "write_file": False,
            "read_file": False,
            "edit_file": False,
        },
        checkpointer=checkpointer,
        debug=True,
    )

    return agent


# ─── Entry Point ──────────────────────────────────────────────────────────────


async def main(user_id: str = "", session_id: str = ""):
    thread_id = f"conv-{int(time.time())}"

    pg_dsn = (
        f"postgresql://{os.getenv('POSTGRES_USER')}:"
        f"{quote_plus(os.getenv('POSTGRES_PASSWORD'))}@"
        f"{os.getenv('POSTGRES_HOST')}:"
        f"{os.getenv('POSTGRES_PORT')}/"
        f"{os.getenv('POSTGRES_DB')}"
    )

    sandbox = _make_sandbox(thread_id)

    async with AsyncPostgresStore.from_conn_string(pg_dsn) as store:
        await store.setup()

        async with AsyncPostgresSaver.from_conn_string(pg_dsn) as checkpointer:
            # 首次运行需要建表（幂等，可重复执行）
            await checkpointer.setup()

            agent = await create_my_agent(
                user_id or "default-user",
                session_id or "default-session",
                thread_id,
                store=store,
                sandbox=sandbox,
                checkpointer=checkpointer,
            )

            async for mode, data in agent.astream(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": "根据PLC代码审查skill在当前workspace输出分析报告",
                        }
                    ]
                },
                config={"configurable": {"thread_id": thread_id}},
                stream_mode=["updates", "messages", "custom"],
            ):
                if mode == "messages":
                    token, _ = data
                    if hasattr(token, "content"):
                        print(token.content, end="", flush=True)
                elif mode == "updates":
                    for node_name, node_output in data.items():
                        print(f"\n[节点完成:{node_name}]", flush=True)
                elif mode == "custom":
                    print(f"\n[自定义：{data}]", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
