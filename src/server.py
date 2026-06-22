from contextlib import asynccontextmanager
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from .api.router import api_router
from .infra.database import _build_dsn


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = _build_dsn()
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
