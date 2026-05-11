import os
from langchain.chat_models import init_chat_model
from langgraph.store.memory import InMemoryStore
from dotenv import load_dotenv


from deepagents import (
    create_deep_agent,
    CompositeBackend,
    FilesystemBackend,
    StoreBackend,
    StateBackend,
)

load_dotenv()

os.getenv("OPENAI_API_KEY")

model = init_chat_model(
    model="google_genai:gemini-3.1-pro-preview", thinking_level="medium", temperature=0
)

agent = create_deep_agent(
    model=model,
    backend=StoreBackend(
        namespace=lambda ctx: (ctx.runtime.context.user_id,),
    ),
    store=InMemoryStore(),  # Good for local dev; omit for LangSmith Deployment
)

agent = create_deep_agent(
    model="google_genai:gemini-3.1-pro-preview",
    backend=CompositeBackend(
        default=StateBackend(),
        routes={
            "/memories/": FilesystemBackend(
                root_dir="/deepagents/myagent", virtual_mode=True
            ),
        },
    ),
)


from langchain_google_genai import ChatGoogleGenerativeAI
from deepagents import create_deep_agent

model = ChatGoogleGenerativeAI(
    model="gemini-3.1-pro-preview", thinking_level="medium", temperature=0
)
agent = create_deep_agent(
    model=model,
    backend=FilesystemBackend(root_dir="/Users/nh/Desktop/", virtual_mode=True),
)


# from langchain_openai import ChatOpenAI
# model = ChatOpenAI(
#     model="Qwen3.6-35B-A3B",
#     api_key=os.getenv("OPENAI_API_KEY"),
#     base_url="http://172.16.66.13:8003/v1",
# )
# response = model.invoke("Why do parrots talk?")
