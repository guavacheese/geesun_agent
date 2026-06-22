from contextlib import asynccontextmanager
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from .api.router import api_router
from .infra.database import _build_dsn


from src.core.config import settings
import os


def _load_skills() -> list[str]:
    """启动时一次性加载所有技能目录，避免每次请求扫描磁盘。"""
    skills_root = f"{settings.agent_workspace}/skills"
    if not os.path.isdir(skills_root):
        return []
    return [
        f"{skills_root}/{d}"
        for d in os.listdir(skills_root)
        if os.path.isdir(f"{skills_root}/{d}")
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = _build_dsn()

    # 预加载 skills（一次性，避免请求时扫描磁盘）
    app.state.skills = _load_skills()

    async with AsyncPostgresStore.from_conn_string(dsn) as store:
        await store.setup()
        app.state.store = store

        async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
            await checkpointer.setup()
            app.state.checkpointer = checkpointer

            from src.core.mcp import get_mcp_tools

            await get_mcp_tools()

            yield  # ← 服务运行期间停在这里
            # 关闭时：PostgreSQL 连接池由 gc 自动清理


app = FastAPI(title="Geesun Agent", lifespan=lifespan)
app.include_router(api_router)
