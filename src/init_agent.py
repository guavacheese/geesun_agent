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
from langchain.messages import trim_messages
from langchain.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from langchain_cubesandbox import CubeSandbox
# from deepagents.middleware import MessagesFilterMiddleware


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

# system_prompt = """\
# You are an elite PLC Code Review Engineer specializing in
# Omron NX/NJ series industrial automation systems.

# ## 上下文管理
# 每次开始执行任务前，先调用 compact_conversation 工具压缩历史消息。
# 这能确保你不会超出模型的上下文窗口限制。
# 调用方式：compact_conversation

# ## 文件读取策略（极其重要）
# - PLC 源码和 Excel 数据通常有数十万行，超出上下文窗口
# - 使用 grep 搜索关键词定位到具体位置，然后用 read_file 只读相关行
# - 每次 read_file 限制读取 50-100 行，不要一次性读整个文件
# - 大文件的完整内容会自动 offload 到后端存储，你需要时可以用路径引用取回

# ## 关键工具
# - grep: 搜索特定变量名、功能块、指令
# - read_file: 精确读取指定行范围，不要省略 offset 和 limit 参数
# - glob: 列出文件清单

# ## 文件系统规则（极其重要）
# 工作区根目录是 /workspace/，所有文件操作都以此为根。

# 正确用法：
#   ls /workspace/                          # 列出项目根目录文件
#   read_file /workspace/file.xml           # 用相对工作区路径
#   grep "TON" /workspace/                  # 在项目目录下搜索
#   glob "*.xml" /workspace/                # 查找 XML 文件

# 错误用法：
#   read_file /workspace/mnt/d/workspace/geesun_agent/file.xml  ❌ 不要带绝对路径
#   read_file /mnt/d/workspace/geesun_agent/file.xml            ❌ 不要直接访问磁盘

# 规则：/workspace/ 下的路径就是相对于项目根目录的路径。

# ## 核心原则
# - 基于证据，不捏造数据；解析失败时报告部分结果
# - 保留所有原始标识符（中文/日文/英文）原样输出
# - 数据有歧义时，先明确说明假设再下结论

# ## 重要
# 所有审查流程、输出格式、报告模板，严格遵循你的 plc-code-auditor Skills 中的规范，
# 不得自行发明格式。"""


system_prompt = """\
You are an elite PLC Code Review Engineer specializing in
Omron NX/NJ series industrial automation systems.



## 文件读取策略（极其重要）
- PLC 源码和 Excel 数据通常有数十万行，超出上下文窗口
- 使用 grep 搜索关键词定位到具体位置，然后用 read_file 只读相关行
- 每次 read_file 限制读取 50-100 行，不要一次性读整个文件
- 大文件的完整内容会自动 offload 到后端存储，你需要时可以用路径引用取回

## 关键工具
- grep: 搜索特定变量名、功能块、指令
- read_file: 精确读取指定行范围，不要省略 offset 和 limit 参数
- glob: 列出文件清单

## 文件系统规则（极其重要）
工作区根目录是 /workspace/，所有文件操作都以此为根。

正确用法：
  ls /workspace/                          # 列出项目根目录文件
  read_file /workspace/file.xml           # 用相对工作区路径
  grep "TON" /workspace/                  # 在项目目录下搜索
  glob "*.xml" /workspace/                # 查找 XML 文件

错误用法：
  read_file /workspace/mnt/d/workspace/geesun_agent/file.xml  ❌ 不要带绝对路径
  read_file /mnt/d/workspace/geesun_agent/file.xml            ❌ 不要直接访问磁盘

规则：/workspace/ 下的路径就是相对于项目根目录的路径。

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
        print("loaded tools:")
        for t in _cached_tools:
            print(t.name)
        return _cached_tools
    except Exception:
        return []


# ─── Sandbox Factory ──────────────────────────────────────────────────────────


def _make_sandbox(thread_id: str) -> Any:

    try:
        return CubeSandbox.get_or_create(
            template=os.environ["CUBE_TEMPLATE_ID"],
            thread_id=thread_id,
            api_url=os.environ.get("CUBE_API_URL"),
            api_key=os.environ.get("CUBE_API_KEY", "dummy"),
            # ssl_cert=
        )
    except Exception as e:
        print(f"[WARN] sandbox unavailable: {e}")
        return None


# ─── Backend ──────────────────────────────────────────────────────────────────


def build_backend(
    user_id: str,
    session_id: str,
    thread_id: str,
    store: AsyncPostgresStore,
    sandbox: Any,
) -> CompositeBackend:
    routes: dict[str, Any] = {
        # ── 工作区 A：Agent 按规范传 /workspace/file.xml ──
        "/workspace/": LocalShellBackend(
            root_dir=WORKSPACE,  # ← 映射到真实磁盘目录
            virtual_mode=True,  # 路径相对于 root_dir 解析，阻止 .. 和 ~
            env={**os.environ},
        ),
        # ── 工作区 B：模型不听话，传磁盘绝对路径时的兜底 ──
        f"{WORKSPACE}/": LocalShellBackend(
            root_dir=WORKSPACE,
            virtual_mode=True,
            env={**os.environ},
        ),
        # ── 用户上传 ──
        f"/uploads/{user_id}/{session_id}": FilesystemBackend(
            root_dir=f"{UPLOAD_ROOT}/{user_id}/{session_id}/",
            virtual_mode=True,
        ),
        # ── 报告输出 ──
        f"/reports/{user_id}/{session_id}": FilesystemBackend(
            root_dir=f"{REPORT_ROOT}/{user_id}/{session_id}/",
            virtual_mode=True,
        ),
        # ── 长期记忆 ──
        "/memories/": StoreBackend(
            namespace=lambda rt: ("memories", _user_id_safe(user_id)),
            store=store,  # 或者 AsyncPgStore
        ),
    }
    if sandbox:
        routes["/code/"] = sandbox

    return CompositeBackend(
        default=StateBackend(),  # ← offload 文件落这里，不污染磁盘
        # default=LocalShellBackend(
        #     root_dir=WORKSPACE,
        #     virtual_mode=False,
        #     env={**os.environ},
        # ),
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

    print("creating agent...")
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
        # middleware=[
        #     MessagesFilterMiddleware(
        #         max_messages=20,  # 只保留最近 20 条消息在上下文中
        #         # 旧消息仍存在 checkpointer 中，需要时可回溯
        #     ),
        # ],
        # messages_modifier=trim_messages(
        #     max_tokens=200000,  # 不超过 200K tokens,自动裁剪超过 200K tokens 的旧消息
        #     strategy="last",  # 保留最后的消息
        #     token_counter=model,  # 用模型自己的 tokenizer 计数
        #     include_system=True,  # system prompt 始终保留
        # ),
        debug=True,
    )

    print("agent created")
    print(agent.get_graph().draw_ascii())
    return agent


# ─── Entry Point ──────────────────────────────────────────────────────────────


async def main(user_id: str = "", session_id: str = ""):
    # thread_id = f"conv-{int(time.time())}"
    thread_id = "conv-1781579271"

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
                            "content": "根据PLC代码审查skill在当前workspace输出分析报告，并把用户的偏好'使用中文交流'写入 /memories/ 目录",
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
                    print(data)
                    for node_name, node_output in data.items():
                        print(f"\n[节点完成:{node_name}]", flush=True)
                elif mode == "custom":
                    print(f"\n[自定义：{data}]", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
