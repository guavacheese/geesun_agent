import os
import logging

logger = logging.getLogger(__name__)

from deepagents import create_deep_agent
from deepagents.backends import (
    CompositeBackend,
    FilesystemBackend,
    LocalShellBackend,
    StateBackend,
    StoreBackend,
)
from deepagents.backends.protocol import FileDownloadResponse, WriteResult
from deepagents.backends.utils import file_data_to_string
import base64
from langchain.messages import trim_messages
from src.core.config import settings
from src.core.model import create_model, switch_model
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


class ValidatedCompositeBackend(CompositeBackend):
    """带路径白名单校验的 CompositeBackend 包装。

    关键：在 CompositeBackend.write 调用 _get_backend_and_key 之前拦截，
    此时 file_path 还是完整虚拟路径（/reports/{user}/{sid}/file.go），
    还没被路由前缀剥掉。

    M2（设计文档）：拒绝 ≠ 沉默。持有 user/session 上下文，拒绝时按路径
    模式返回可执行修正建议，把模型可能不记得的会话上下文直接算好塞回。
    """

    ALLOWED_WRITE_PREFIXES = {"/reports/", "/workspace/memories/"}

    # 沙箱内路径：write_file 本就不该碰，命中即提示走 MCP 传输或直接写 reports
    SANDBOX_PATH_PREFIXES = ("/tmp/", "/home/", "/root/", "/mnt/", "/code/", "/var/")
    # 虚拟文件系统内只读路径：命中即提示改 reports
    VIRTUAL_READONLY_PREFIXES = ("/uploads/", "/skills/", "/workspace/agent-memory/")

    def __init__(self, default, routes, *, user_id: str = "", session_id: str = ""):
        super().__init__(default=default, routes=routes)
        self._report_prefix = f"/reports/{user_id}/{session_id}/" if user_id and session_id else "/reports/<user_id>/<session_id>/"

    def _reject_hint(self, file_path: str) -> str:
        """按路径类型生成修正建议（单条 ≤ 200 字，仅拒绝路径出现）。"""
        if file_path.startswith(self.SANDBOX_PATH_PREFIXES):
            return (
                f"路径 '{file_path}' 是沙箱内路径，write_file 无法写入沙箱文件系统。"
                f"沙箱内文件请用 upload_to_sandbox / copy_script_to_sandbox 传输；"
                f"如需生成交付物，请直接写入 '{self._report_prefix}<文件名>'"
            )
        if file_path.startswith(self.VIRTUAL_READONLY_PREFIXES):
            return (
                f"路径 '{file_path}' 为只读（输入/技能/共享规则）。"
                f"交付物请写入 '{self._report_prefix}<文件名>'"
            )
        return (
            f"只能写入到以下路径: {', '.join(sorted(self.ALLOWED_WRITE_PREFIXES))}。"
            f"当前会话交付目录为 '{self._report_prefix}'"
        )

    def write(self, file_path: str, content: str) -> WriteResult:
        if not any(file_path.startswith(p) for p in self.ALLOWED_WRITE_PREFIXES):
            hint = self._reject_hint(file_path)
            logger.warning(
                "[VALIDATED_CB] 拒绝写入: path=%s (只允许 %s) | hint=%s",
                file_path, self.ALLOWED_WRITE_PREFIXES, hint,
            )
            return WriteResult(
                error=f"拒绝写入: {hint}",
                path=file_path,
                files_update=None,
            )
        return super().write(file_path, content)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        if not any(file_path.startswith(p) for p in self.ALLOWED_WRITE_PREFIXES):
            hint = self._reject_hint(file_path)
            logger.warning(
                "[VALIDATED_CB] 拒绝写入: path=%s (只允许 %s) | hint=%s",
                file_path, self.ALLOWED_WRITE_PREFIXES, hint,
            )
            return WriteResult(
                error=f"拒绝写入: {hint}",
                path=file_path,
                files_update=None,
            )
        return await super().awrite(file_path, content)


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
        return ValidatedCompositeBackend(
            default=sandbox,
            routes=routes,
            user_id=user_id,
            session_id=session_id,
        )
    else:
        return ValidatedCompositeBackend(
            default=LocalShellBackend(
                root_dir=settings.agent_workspace,
                virtual_mode=True,
                env={**os.environ},
            ),
            routes=routes,
            user_id=user_id,
            session_id=session_id,
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
        middleware=[switch_model],
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
