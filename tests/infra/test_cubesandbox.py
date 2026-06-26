import os
from pathlib import Path
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.DEBUG)

from langchain_cubesandbox import CubeSandbox


BASE_DIR = Path(__file__).resolve().parent.parent.parent
ca_path = os.getenv("CUBE_CA_PATH", str(BASE_DIR / "certs" / "cube-ca.pem"))

# 用户发消息时 — 有就复用，没有就新建
sandbox = CubeSandbox.get_or_create(
    template=os.environ["CUBE_TEMPLATE_ID"],
    thread_id="test-user:1001",
    api_url=os.environ["CUBE_API_URL"],
    api_key="e2b_0000000000000000000000000000000000000000",
    ssl_cert=str(ca_path),
)
print(f"=============Created sandbox: {sandbox.id}")
print(f"======================={sandbox._sandbox}")

result = CubeSandbox.list(metadata={"thread_id": "test-user:1001"}, state="running")
print(f"=============List result: {result}")

# 执行代码
result = sandbox.execute("echo 'hello kitty'")
print(f"================{result.output}")

input("Press Enter to exit...")  # 卡住，不让进程结束
