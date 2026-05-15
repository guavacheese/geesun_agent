import os
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.DEBUG)

from langchain_cubesandbox import CubeSandbox


# 用户发消息时 — 有就复用，没有就新建
sandbox = CubeSandbox.get_or_create(
    template=os.environ["CUBE_TEMPLATE_ID"],
    thread_id="conv-12345",
    api_url=os.environ["CUBE_API_URL"],
    api_key="dummy",
)
print(f"=============Created sandbox: {sandbox.id}")
print(f"======================={sandbox._sandbox}")

result = CubeSandbox.list(metadata={"thread_id": "conv-12345"}, state="running")
print(f"=============List result: {result}")

# 执行代码
result = sandbox.execute("echo 'hello kitty'")
print(f"================{result.output}")
