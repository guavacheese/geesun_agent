from src.core.config import settings
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
ca_path = os.getenv("CUBE_CA_PATH", str(BASE_DIR / "certs" / "cube-ca.pem"))


def create_sandbox(thread_id: str):
    key = settings.cube_api_key
    if not key or not key.startswith("e2b_"):
        return None  # key 无效，不尝试创建，静默跳过

    try:
        from langchain_cubesandbox import CubeSandbox

        return CubeSandbox.get_or_create(
            template=settings.cube_template_id,
            thread_id=thread_id,
            api_url=settings.cube_api_url,
            api_key=key,
            ssl_cert=str(ca_path),
        )
    except Exception as e:
        print(f"[WARN] sandbox unavailable: {e}")
        return None
