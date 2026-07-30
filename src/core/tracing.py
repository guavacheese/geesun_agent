"""Arize Phoenix + OpenInference 追踪初始化模块。

必须在任何 LangChain / LangGraph / deepagents 被 import 之前调用 setup_tracing()。
这是因为 OpenInference 的 auto-instrumentation 需要在模块加载时 hook 进去。

使用方法（在 server.py 顶部）：

    from src.core.logging import *         # 日志最先就绪
    from src.core.tracing import setup_tracing
    setup_tracing()                         # ← 此时还没有 import LangChain
    from .api.router import api_router      # ← 安全了
"""

import os
import logging

logger = logging.getLogger(__name__)

_initialized = False


def setup_tracing() -> bool:
    """初始化 Arize Phoenix 追踪。

    通过 Settings（读取 .env 文件）获取 PHOENIX_COLLECTOR_ENDPOINT，
    也支持通过真实环境变量覆盖。不设置时静默跳过，不影响正常启动。
    """
    global _initialized
    if _initialized:
        return True

    # 通过 pydantic-settings 读取 .env 中的 PHOENIX_COLLECTOR_ENDPOINT
    from src.core.config import settings

    collector_endpoint = settings.phoenix_collector_endpoint
    if not collector_endpoint:
        logger.warning(
            "[TRACING] PHOENIX_COLLECTOR_ENDPOINT 未设置 — Arize Phoenix tracing 已禁用"
        )
        return False

    try:
        from phoenix.otel import register

        # ★ 关键：phoenix.otel.register() 内部通过 os.environ 读取 endpoint，
        # 之前只存在 settings 对象里，register() 读不到，默认走了 localhost:4317。
        # 这里显式写入 os.environ 并用 endpoint 参数双重保证。
        os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = collector_endpoint

        tracer_provider = register(
            project_name="Geesun-Agent-dev",
            auto_instrument=True,
        )

        _initialized = True
        logger.info(
            "[TRACING] Arize Phoenix tracing 已初始化 — "
            "project=Geesun-Agent-dev, endpoint=%s",
            collector_endpoint,
        )
        return True

    except ImportError as e:
        logger.warning(
            "[TRACING] Arize Phoenix 导入失败 (%s) — 请检查是否已安装 "
            "arize-phoenix-otel 和 openinference-instrumentation-langchain",
            e,
        )
        return False
    except Exception as e:
        logger.warning("[TRACING] Arize Phoenix 初始化异常: %s", e)
        return False
