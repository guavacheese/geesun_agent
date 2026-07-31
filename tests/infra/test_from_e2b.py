import os
from dotenv import load_dotenv
import logging

load_dotenv()

logging.basicConfig(level=logging.DEBUG)

from e2b_code_interpreter import Sandbox


sb = None

try:
    sb = Sandbox.create(
        template=os.environ["CUBE_TEMPLATE_ID"],
        api_key=os.environ["CUBE_API_KEY"],
        api_url=os.environ["CUBE_API_URL"],
        secure=False,
    )
    result = sb.run_code("print('============hello')")
    print(result)
finally:
    if sb is not None:
        try:
            sb.kill()
        except Exception as e:
            # 408 只是"销毁超过 30s 被 CubeAPI 超时掐断"，不代表功能失败
            print(f"[warn] kill failed (sandbox_id={sb.sandbox_id}): {e}")
