import os
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.DEBUG)

from langchain_cubesandbox import CubeSandbox

sandbox = CubeSandbox(
    template=os.environ["CUBE_TEMPLATE_ID"],
    api_url=os.environ["CUBE_API_URL"],
    api_key=os.environ["CUBE_API_KEY"],
    ssl_cert=os.environ["SSL_CERT_FILE"],
)

result = sandbox.execute("写一个java冒牌排序")

print(result)
