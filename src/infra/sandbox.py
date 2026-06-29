from src.core.config import settings
import os
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
ca_path = os.getenv("CUBE_CA_PATH", str(BASE_DIR / "certs" / "cube-ca.pem"))


def create_sandbox(thread_id: str):
    key = settings.cube_api_key
    if not key or not key.startswith("e2b_"):
        return None  # key 无效，不尝试创建，静默跳过

    try:
        from langchain_cubesandbox import CubeSandbox

        sandbox = CubeSandbox.get_or_create(
            template=settings.cube_template_id,
            thread_id=thread_id,
            api_url=settings.cube_api_url,
            api_key=key,
            ssl_cert=str(ca_path),
        )

        # 沙箱内配置 pip 国内镜像源（清华优先 + PyPI 官方兜底）
        if hasattr(sandbox, "_sandbox") and sandbox._sandbox is not None:
            try:
                sandbox.execute(
                    "pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple "
                    "&& pip config set global.extra-index-url https://pypi.org/simple"
                )
            except Exception:
                pass  # pip 配置非关键，失败不影响沙箱使用
            return sandbox
        return None
    except Exception as e:
        logger.warning("sandbox unavailable: %s", e)
        return None
