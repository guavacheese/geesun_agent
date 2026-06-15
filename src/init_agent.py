import asyncio
import os
import time

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

# from langchain.chat_models import init_chat_model
from langchain_openai import ChatOpenAI

# from langgraph.store.memory import InMemoryStore
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from deepagents.middleware.summarization import create_summarization_tool_middleware


load_dotenv()

BASE_URL = os.getenv("BASE_URL")
API_KEY = os.getenv("OPENAI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME")
WORKSPACE = os.getenv("AGENT_WORKSPACE")
UPLOAD_ROOT = "/data/myapp/uploads"  # 用户上传文件的物理路径
REPORT_ROOT = "/data/myapp/reports"  # 生成报告的物理路径

# sandbox
from langchain_cubesandbox import CubeSandbox


# 用户发消息时 — 有就复用，没有就新建
sandbox = CubeSandbox.get_or_create(
    template=os.environ["CUBE_TEMPLATE_ID"],
    thread_id="conv-12345",
    api_url=os.environ["CUBE_API_URL"],
    api_key="dummy",
)

user_id = ""
session_id = ""


plc_audit_instructions = """
    You are an elite PLC Code Review Engineer specializing in 
    Omron NX/NJ series industrial automation systems.

    ## 核心原则
    - 基于证据，不捏造数据；解析失败时报告部分结果
    - 保留所有原始标识符（中文/日文/英文）原样输出
    - 数据有歧义时，先明确说明假设再下结论

    ## 重要
    所有审查流程、输出格式、报告模板，严格遵循你的 plc-code-auditor Skills 中的规范，
    不得自行发明格式。
    """


model = ChatOpenAI(
    base_url=BASE_URL,
    model=MODEL_NAME,
    api_key=API_KEY,
    temperature=0,
    max_retries=5,
    # 如需关闭 thinking：
    # model_kwargs={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    timeout=300,  # ← 5 分钟，给长文本生成留足时间
)


async def mcp_tools() -> list[BaseTool]:

    client = MultiServerMCPClient(
        {
            "decrypt-file": {
                "transport": "streamable-http",
                "url": "http://localhost:8000/mcp",
                "headers": {
                    "Authorization": "Bearer YOUR_TOKEN",
                    "X-customer-header": "custom-value",
                },
            }
        }
    )

    mcp_tools = await client.get_tools()

    return mcp_tools


def build_backend(user_id: str, session_id: str) -> CompositeBackend:
    return CompositeBackend(
        default=StateBackend,
        routes={
            f"/uploads/{user_id}/{session_id}": FilesystemBackend(
                root_dir=f"{UPLOAD_ROOT}/{user_id}/{session_id}",
                virtual_mode=True,
            ),
            f"/reports/{user_id}/{session_id}": FilesystemBackend(
                root_dir=f"{REPORT_ROOT}/{user_id}/{session_id}",
                virtual_mode=True,
            ),
            "/memories/": StoreBackend(
                namespace=lambda rt: (
                    "memories",
                    user_id,
                )
            ),
            "/code/": sandbox,
        },
    )


async def create_my_agent(user_id: str, session_id: str, checkpointer):

    backend = build_backend(user_id, session_id)
    tools = await mcp_tools()

    agent = create_deep_agent(
        model=model,
        tools=tools,
        backend=backend,
        skills=[f"{WORKSPACE}/skills/plc-code-auditor"],  # 具体 skill 路径
        interrupt_on={
            "write_file": False,  # Default: approve, edit, reject
            "read_file": False,  # No interrupts needed
            "edit_file": False,  # Default: approve, edit, reject
        },
        checkpointer=checkpointer,  # Required!
        # system_prompt=plc_audit_instructions,
        debug=True,
        system_prompt=f"""你是一个全能助手，当前服务的用户ID是 {user_id}。

        文件系统说明：
        - /uploads/ 目录存放用户上传的文件
        - /reports/ 目录存放你生成的报告，完成后告诉用户下载
        - /memories/ 目录是你的长期记忆，可以写入跨会话记住的关键信息
        - 比如用户的偏好、已完成的任务等，每次会话开始先读取这里
                
        请根据用户指令完成对应的任务。""",
    )

    return agent


async def main():

    db_path = f"{WORKSPACE}/checkpoints.db"
    os.makedirs(WORKSPACE, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(db_path) as checkpointer:
        agent = await create_my_agent(user_id, session_id, checkpointer=checkpointer)

        async for mode, data in agent.astream(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "根据PLC代码审查skill在当前workspace输出分析报告",
                    }
                ]
            },
            config={"configurable": {"thread_id": f"audit_{int(time.time())}"}},
            stream_mode=["updates", "messages", "custom"],
        ):
            if mode == "messages":
                token, _ = data
                if hasattr(token, "content"):
                    print(token.content, end="", flush=True)
            elif mode == "updates":
                # 仅打印节点名
                for node_name, node_output in data.items():
                    print(f"\n[节点完成:{node_name}]", flush=True)
            elif mode == "custom":
                print(f"\n[自定义：{data}]", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
