import os
import logging

from deepagents import create_deep_agent
from deepagents.backends import (
    CompositeBackend,
    FilesystemBackend,
    LocalShellBackend,
    StateBackend,
    StoreBackend,
)
from deepagents.backends.protocol import FileDownloadResponse
from deepagents.backends.utils import file_data_to_string
import base64
from langchain.messages import trim_messages
from src.core.config import settings
from src.core.model import create_model
from src.core.prompts.plc_auditor import PLC_AUDITOR_SYSTEM_PROMPT

# ─── Monkey-patch: 给 StoreBackend 补上 adownload_files ─────────────────
# deepagents StoreBackend 缺少异步下载文件的实现，默认降级为 asyncio.to_thread
# 但 AsyncPostgresStore 的同步 get() 在跨线程调用时连不上连接池。
# 等官方修好后可删除此补丁。
async def _store_adownload_files(self, paths):
    store = self._get_store()
    namespace = self._get_namespace()
    responses = []
    for path in paths:
        item = await store.aget(namespace, path)
        if item is None:
            responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
            continue
        file_data = self._convert_store_item_to_file_data(item)
        content_str = file_data_to_string(file_data)
        encoding = file_data["encoding"]
        content_bytes = base64.standard_b64decode(content_str) if encoding == "base64" else content_str.encode("utf-8")
        responses.append(FileDownloadResponse(path=path, content=content_bytes, error=None))
    return responses

StoreBackend.adownload_files = _store_adownload_files

# AGENTS.md 虚拟路径，由 memory= 参数始终注入系统提示词（平台通用规则）
# 注意：使用独立的 agent-memory 路由，所有用户共享同一份 AGENTS.md
AGENTS_MD_PATH = "/workspace/agent-memory/AGENTS.md"


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
        # f"{settings.agent_workspace}/": LocalShellBackend(
        #     root_dir=settings.agent_workspace,
        #     virtual_mode=True,
        #     env={**os.environ},
        # ),
        f"/uploads/{user_id}/{session_id}/": FilesystemBackend(
            root_dir=f"{settings.upload_root}/{user_id}/{session_id}/",
            virtual_mode=True,
        ),
        f"/reports/{user_id}/{session_id}/": FilesystemBackend(
            root_dir=f"{settings.report_root}/{user_id}/{session_id}/",
            virtual_mode=True,
        ),
        "/workspace/memories/": StoreBackend(
            namespace=lambda rt: ("memories", user_id or "default-user"),
            store=store,
        ),
        # 独立路由：所有用户共享的 AGENTS.md（平台通用规则，不由用户修改）
        "/workspace/agent-memory/": StoreBackend(
            namespace=lambda rt: ("__agent__",),
            store=store,
        ),
        # Skills 三层路由：系统 → Agent自创 → 用户共享
        "/skills/__system__/": FilesystemBackend(
            root_dir=f"{settings.agent_workspace}/skills/__system__/",
            virtual_mode=True,
        ),
        "/skills/__agent__/": FilesystemBackend(
            root_dir=f"{settings.agent_workspace}/skills/__agent__/",
            virtual_mode=True,
        ),
        f"/skills/__user_{user_id}__/": FilesystemBackend(
            root_dir=f"{settings.agent_workspace}/skills/__user_{user_id}__/",
            virtual_mode=True,
        ),
        # ★ SummarizationMiddleware 需要这个路径来 offload 历史消息
        # f"/conversation_history/{user_id}:{session_id}": FilesystemBackend(
        #     root_dir=f"{settings.report_root}/{user_id}/{session_id}/",
        #     virtual_mode=True,
        # ),
        # offload → LangGraph state，不写磁盘，不经过沙箱;offload 归档：SummarizationMiddleware 写 /conversation_history/xxx.md
        "/conversation_history/": StateBackend(),
        # 大工具结果驱逐 → LangGraph state
        "/large_tool_results/": StateBackend(),
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
        memory=[AGENTS_MD_PATH],
        interrupt_on={
            "write_file": False,
            "read_file": False,
            "edit_file": False,
        },
        checkpointer=checkpointer,
        # messages_modifier=trim_messages(
        #     max_tokens=200000,
        #     strategy="last",
        #     token_counter=model,
        #     include_system=True,
        # ),
        debug=True,
    )
