from dataclasses import dataclass, field, asdict
from typing import Callable

from langchain_openai import ChatOpenAI
from langchain.agents.middleware import (
    wrap_model_call,
    ModelRequest,
    ModelResponse,
)
from src.core.config import settings


# ─── 模型配置（支持多 provider，走 OpenAI 兼容协议） ───

@dataclass
class ModelConfig:
    """运行时动态切换模型的配置。
    
    所有 OpenAI 兼容的 API（vLLM / Kimi / GLM / DeepSeek 等）
    统一走 ChatOpenAI + base_url。
    """
    model_name: str = settings.model_name
    base_url: str = settings.base_url
    api_key: str = settings.openai_api_key


def create_model() -> ChatOpenAI:
    """默认模型（内网 vLLM Qwen）"""
    return ChatOpenAI(
        base_url=settings.base_url,
        model=settings.model_name,
        api_key=settings.openai_api_key,
        temperature=0,
        max_retries=5,
        timeout=300,
    )


# ─── 动态模型切换 middleware ───

@wrap_model_call
async def switch_model(
    request: ModelRequest,
    handler: Callable[[ModelRequest], ModelResponse],
) -> ModelResponse:
    """从 runtime context 读取 ModelConfig，动态切换模型。

    不传 context 或传 None 时走默认模型（内网 vLLM），
    传了 ModelConfig 则用指定模型。
    """
    raw = request.runtime.context.get("model_config")
    if raw is None:
        return await handler(request)

    # 支持 dict 和 ModelConfig 两种传入方式
    if isinstance(raw, dict):
        cfg = ModelConfig(**raw)
    elif isinstance(raw, ModelConfig):
        cfg = raw
    else:
        return await handler(request)

    model = ChatOpenAI(
        model=cfg.model_name,
        base_url=cfg.base_url,
        api_key=cfg.api_key or "not-used",
        temperature=0,
    )
    return await handler(request.override(model=model))
