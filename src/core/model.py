from langchain_openai import ChatOpenAI
from src.core.config import settings


def create_model() -> ChatOpenAI:
    return ChatOpenAI(
        base_url=settings.base_url,
        model=settings.model_name,
        api_key=settings.openai_api_key,
        temperature=0,
        max_retries=5,
        timeout=300,
    )
