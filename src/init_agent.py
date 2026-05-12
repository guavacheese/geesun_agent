import asyncio
import os
import time

from deepagents import create_deep_agent
from deepagents.backends import (
    CompositeBackend,
    FilesystemBackend,
    LocalShellBackend,
    StateBackend,
    # StoreBackend,
)
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

# from langchain.chat_models import init_chat_model
from langchain_openai import ChatOpenAI

# from langgraph.store.memory import InMemoryStore
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from deepagents.middleware.summarization import create_summarization_tool_middleware


load_dotenv()

BASE_URL = os.getenv("BASE_URL")
API_KEY = os.getenv("OPENAI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME")
WORKSPACE = os.getenv("AGENT_WORKSPACE")


async def main():
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

    # model = init_chat_model(
    #     base_url=BASE_URL,
    #     model=MODEL_NAME,
    #     model_provider="openai",
    #     api_key=API_KEY,
    #     # thinking_level="medium",
    #     temperature=0,
    # )

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

    # System prompt to steer the agent to be an expert researcher
    # research_instructions = """You are an expert researcher. Your job is to conduct thorough research and then write a polished report.

    # You have access to an internet search tool as your primary means of gathering information.

    # ## `internet_search`

    # Use this to run an internet search for a given query. You can specify the max number of results to return, the topic, and whether raw content should be included.
    # """

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

    # agent = create_deep_agent(
    #     model=model,
    #     backend=StoreBackend(
    #         namespace=lambda ctx: (ctx.runtime.context.user_id,),
    #     ),
    #     store=InMemoryStore(),  # Good for local dev; omit for LangSmith Deployment
    # )

    db_path = f"{WORKSPACE}/checkpoints.db"
    os.makedirs(WORKSPACE, exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(db_path) as checkpointer:
        agent = create_deep_agent(
            # model="google_genai:gemini-3.1-pro-preview",
            # tools=[file_decrypt],
            model=model,
            tools=mcp_tools,
            # middleware=[
            #     create_summarization_tool_middleware(
            #         max_tokens=180000,
            #         model=model,
            #     )
            # ],
            backend=CompositeBackend(
                # default=StateBackend(),
                # default=FilesystemBackend(root_dir=f"{WORKSPACE}", virtual_mode=False),
                default=LocalShellBackend(
                    root_dir=f"{WORKSPACE}",
                    virtual_mode=False,
                    env={
                        **os.environ
                    },  # 继承当前 shell 的 PATH，让 Agent 能找到 uv/python
                ),
                routes={
                    "/memories/": FilesystemBackend(
                        root_dir=f"{WORKSPACE}/myagent", virtual_mode=True
                    ),
                    "/skills/": FilesystemBackend(
                        root_dir=f"{WORKSPACE}/skills",  # skills 目录
                        virtual_mode=True,
                    ),
                },
            ),
            skills=[f"{WORKSPACE}/skills/plc-code-auditor"],  # 具体 skill 路径
            interrupt_on={
                "write_file": False,  # Default: approve, edit, reject
                "read_file": False,  # No interrupts needed
                "edit_file": False,  # Default: approve, edit, reject
            },
            checkpointer=checkpointer,  # Required!
            system_prompt=plc_audit_instructions,
            debug=True,
        )

        # result = await agent.ainvoke(
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

    # result = await agent.ainvoke(
    #     {
    #         "messages": [
    #             {
    #                 "role": "user",
    #                 "content": "根据PLC代码审查skill在当前workspace输出分析报告",
    #             }
    #         ]
    #     },
    #     config={"configurable": {"thread_id": "xx12345"}},
    #     # stream_mode=["updates", "messages", "custom"],
    # )

    # print(result["messages"][-1].content)

    # from langchain_google_genai import ChatGoogleGenerativeAI
    # from deepagents import create_deep_agent

    # model = ChatGoogleGenerativeAI(
    #     model="gemini-3.1-pro-preview", thinking_level="medium", temperature=0
    # )
    # agent = create_deep_agent(
    #     model=model,
    #     backend=FilesystemBackend(root_dir="/Users/nh/Desktop/", virtual_mode=True),
    # )

    # model = ChatOpenAI(
    #     model="Qwen3.6-35B-A3B",
    #     api_key=os.getenv("OPENAI_API_KEY"),
    #     base_url="http://172.16.66.13:8003/v1",
    # )
    # response = model.invoke("Why do parrots talk?")


if __name__ == "__main__":
    asyncio.run(main())
