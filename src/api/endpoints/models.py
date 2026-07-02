"""模型列表端点 — 返回系统预装和额外配置的可用模型。"""

import json
import logging

from fastapi import APIRouter
from src.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/models")
async def list_models():
    """
    返回当前系统可用的模型列表。
    默认模型从 .env 的 base_url / model_name 读取，
    额外模型从 EXTRA_MODELS JSON 数组读取。
    """
    models = [
        {
            "id": "default",
            "model_name": settings.model_name,
            "base_url": settings.base_url,
            "is_default": True,
        }
    ]

    # 解析额外模型（JSON 数组）
    extra_raw = settings.extra_models.strip()
    if extra_raw and extra_raw != "[]":
        try:
            extra = json.loads(extra_raw)
            for item in extra:
                models.append({
                    "id": item.get("id", f"extra-{len(models)}"),
                    "model_name": item["model_name"],
                    "base_url": item["base_url"],
                    "api_key": item.get("api_key", ""),
                    "is_default": False,
                })
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("EXTRA_MODELS 解析失败: %s", e)

    return {"models": models}
