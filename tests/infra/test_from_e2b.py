import os
from pathlib import Path
from dotenv import load_dotenv
import logging

load_dotenv()

logging.basicConfig(level=logging.DEBUG)

from e2b_code_interpreter import Sandbox

BASE_DIR = Path(__file__).resolve().parent.parent.parent
ca_path = os.getenv("CUBE_CA_PATH", str(BASE_DIR / "certs" / "rootCA.pem"))

# 关键：e2b SDK 没有 ssl_cert 参数，证书信任靠环境变量
os.environ["SSL_CERT_FILE"] = ca_path  # ← 加这一行，删掉 create 里的 ssl_cert=

sb = None

try:
    sb = Sandbox.create(
        template=os.environ["CUBE_TEMPLATE_ID"],
        api_key=os.environ["CUBE_API_KEY"],
        api_url=os.environ["CUBE_API_URL"],
        # ssl_cert=str(ca_path),
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
