import os

from deepagents import create_deep_agent
from deepagents.backends import (
    CompositeBackend,
    FilesystemBackend,
    LocalShellBackend,
    StateBackend,
    StoreBackend,
)
from src.core.config import settings
from src.core.model import create_model
from src.core.prompts.plc_auditor import PLC_AUDITOR_SYSTEM_PROMPT


def build_backend(user_id: str, session_id: str, store, sandbox):
    """
    Execute a shell command via the default backend.
    Unlike file operations, execution is not path-routable — it always delegates to the default backend
    """

    routes: dict = {
        # 模型按规范发 /workspace/file.xml
        # "/workspace/": LocalShellBackend(
        #     root_dir=settings.agent_workspace,
        #     virtual_mode=True,
        #     env={**os.environ},
        # ),
        # 兜底：模型不听话发磁盘绝对路径
        f"{settings.agent_workspace}/": LocalShellBackend(
            root_dir=settings.agent_workspace,
            virtual_mode=True,
            env={**os.environ},
        ),
        f"/uploads/{user_id}/{session_id}": FilesystemBackend(
            root_dir=f"{settings.upload_root}/{user_id}/{session_id}/",
            virtual_mode=True,
        ),
        f"/reports/{user_id}/{session_id}": FilesystemBackend(
            root_dir=f"{settings.report_root}/{user_id}/{session_id}/",
            virtual_mode=True,
        ),
        "/workspace/memories/": StoreBackend(
            namespace=lambda rt: ("memories", user_id or "default-user"),
            store=store,
        ),
    }
    if sandbox:
        # sandbox 作为 default backend —— execute 走这里！
        return CompositeBackend(default=sandbox, routes=routes)
    else:
        return CompositeBackend(
            default=LocalShellBackend(
                root_dir=settings.agent_workspace,
                virtual_mode=True,
                env={**os.environ},
            ),
            routes=routes,
        )


async def create_agent(
    user_id: str,
    session_id: str,
    thread_id: str,
    store,
    sandbox,
    checkpointer,
    tools,
    skills: list[str],
):
    backend = build_backend(user_id, session_id, store, sandbox)
    model = create_model()

    return create_deep_agent(
        model=model,
        tools=tools,
        backend=backend,
        system_prompt=PLC_AUDITOR_SYSTEM_PROMPT,
        skills=skills,
        interrupt_on={
            "write_file": False,
            "read_file": False,
            "edit_file": False,
        },
        checkpointer=checkpointer,
        debug=True,
    )
