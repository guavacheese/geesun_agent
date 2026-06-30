"""FastAPI 应用入口 — 日志配置由 src/core/logging.py 统一管理。"""

from src.core.logging import *  # noqa: F401,F403 — 日志最早就绪

from contextlib import asynccontextmanager
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from .api.router import api_router
from .infra.database import _build_dsn


from src.core.config import settings
import os
import logging


def _load_skills() -> list[str]:
    """启动时一次性加载所有技能目录，避免每次请求扫描磁盘。"""
    skills_root = f"{settings.agent_workspace}/skills"
    logging.warning(f"[DIAG] _load_skills: skills_root={skills_root}")
    if not os.path.isdir(skills_root):
        logging.warning(f"[DIAG] _load_skills: DIR NOT FOUND: {skills_root}")
        return []
    dirs = os.listdir(skills_root)
    logging.warning(f"[DIAG] _load_skills: dirs={dirs}")
    found = [f"{skills_root}/{d}" for d in dirs if os.path.isdir(f"{skills_root}/{d}")]
    logging.warning(f"[DIAG] _load_skills: found skills={found}")
    return found


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = _build_dsn()

    # 预加载 skills（启动时设置系统 + Agent 自创路径，用户路径在 chat.py 中按请求追加）
    app.state.skills = ["/skills/__system__/", "/skills/__agent__/"]
    logging.warning(f"[DIAG] lifespan: skills loaded = {app.state.skills}")

    async with AsyncPostgresStore.from_conn_string(dsn) as store:
        await store.setup()

        # 种子 AGENTS.md 到 store（供 memory= 参数通过 StoreBackend 读取）
        agents_md_key = "/AGENTS.md"
        if await store.aget(("__agent__",), agents_md_key) is None:
            with open("AGENTS.md", "r", encoding="utf-8") as f:
                content = f.read()
            await store.aput(
                ("__agent__",),
                agents_md_key,
                {"content": content, "encoding": "utf-8"},
            )
            logging.warning("[DIAG] AGENTS.md seeded to store")

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
