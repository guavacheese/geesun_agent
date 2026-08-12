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
from deepagents.backends.protocol import FileDownloadResponse, ReadResult, WriteResult
from deepagents.backends.utils import file_data_to_string
from deepagents.middleware.summarization import SummarizationMiddleware


class _SummarizationAccurate(SummarizationMiddleware):
    """精确计数版 Summarization：实现与默认完全一致，仅类名不同。

    langchain factory 按 middleware.name 去重（factory.py:1079）：
    默认实例（deepagents graph.py:797 创建）name="SummarizationMiddleware"，
    本子类 name=type(self).__name__="_SummarizationAccurate"，避免 AssertionError。
    唯一差异：挂载用 model.get_num_tokens 精确计数的 token_counter（中文 token 低估修复）。
    """
import base64
from langchain.messages import trim_messages
from src.core.config import settings
from src.core.model import create_model, switch_model, file_to_image
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

    # /conversation_history/ 是 SummarizationMiddleware 的 offload 路由（StateBackend，不落盘），
    # 必须允许写，否则历史压缩后无法恢复（实测 2026-08-10 日志：Offloading failed, Older messages will not be recoverable）
    # /skills/__agent__/：agent 自创 skill 层（三层设计：__system__ 预装 / __agent__ 自创 / __user_{id}__ 用户上传）
    # /home/、/tmp/：沙箱内路径——write_file 走 sandbox backend 的 e2b 上传通道直写沙箱（仅 UTF-8 文本），
    #   让 AI 直接 write_file 写脚本/中间文件，不必绕 execute heredoc（也更安全，无 shell 注入面）
    ALLOWED_WRITE_PREFIXES = {
        "/reports/", "/workspace/memories/", "/conversation_history/",
        "/skills/__agent__/",
        "/home/", "/tmp/",
    }

    # 沙箱内仍禁写的路径（/home/ /tmp/ 已放行直写沙箱；这些是系统级/挂载路径，AI 不应碰）
    SANDBOX_PATH_PREFIXES = ("/root/", "/mnt/", "/code/", "/var/")
    # 虚拟文件系统内只读路径：命中即提示改 reports（/skills/ 需细分提示，见 _reject_hint）
    VIRTUAL_READONLY_PREFIXES = ("/uploads/", "/skills/", "/workspace/agent-memory/")

    def __init__(self, default, routes, *, user_id: str = "", session_id: str = ""):
        super().__init__(default=default, routes=routes)
        self._report_prefix = f"/reports/{user_id}/{session_id}/" if user_id and session_id else "/reports/<user_id>/<session_id>/"

    def _is_binary_read(self, file_path: str) -> str | None:
        """read_file 二进制拦截：返回拦截提示（带下一步指引）或 None。

        直接 read_file 读 PDF/Excel/Word 等二进制会把 base64 塞进 LLM 上下文：
        1) 撑爆上下文；2) Qwen 不支持 file part → 501 崩溃；3) 密文内容进上下文后
        可被 heredoc 绕道写进沙箱。故在 read 层直接拒绝并给出正确链路。
        """
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in settings.read_file_binary_exts:
            return None
        return (
            f"文件 '{file_path}' 是二进制/加密文档（{ext}），read_file 无法直接读取文本，"
            f"直接读取会撑爆上下文或导致模型 API 报错（501 Unknown part type: file）。"
            f"正确流程：decrypt_and_upload_to_sandbox(file_path='{file_path}', "
            f"remote_path='/home/user/<文件名>', sandbox_id='<沙箱ID>') "
            f"→ execute 在沙箱内用 python（pdfplumber 等）解析 → 文本返回。"
            f"禁止用 read_file 读取 {ext} 文件。"
        )

    def _reject_hint(self, file_path: str) -> str:
        """按路径类型生成修正建议（单条 ≤ 200 字，仅拒绝路径出现）。"""
        # 沙箱内仍禁写的路径（/home/ /tmp/ 已放行，只剩 /root/ /mnt/ /code/ /var/）
        if file_path.startswith(self.SANDBOX_PATH_PREFIXES):
            return (
                f"路径 '{file_path}' 是沙箱内系统级/挂载路径，禁止写入。"
                f"沙箱内文件请用 write_file 写 '/home/user/<文件名>'（已支持直写沙箱）或 execute 创建；"
                f"交付物请写入 '{self._report_prefix}<文件名>'"
            )
        # 用户共享 skill：归上传 API 管（SKILL.md 需 YAML 校验）
        if file_path.startswith("/skills/__user_"):
            return (
                f"路径 '{file_path}' 是用户共享 skill 目录（只读，SKILL.md 需 YAML 校验）。"
                f"创建/更新用户 skill 请通过 /api/v1/skill/upload 接口上传；"
                f"agent 自创 skill 请写入 '/skills/__agent__/<skill_name>/'"
            )
        # 系统预装 skill：只读
        if file_path.startswith("/skills/__system__"):
            return (
                f"路径 '{file_path}' 是系统预装 skill（只读），不可修改。"
                f"如需新技能，agent 自创请写 '/skills/__agent__/<skill_name>/'，用户上传走 /api/v1/skill/upload"
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

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """read_file 二进制拦截：PDF/Excel/Word 等直接拒绝并提示走解密链路。"""
        hint = self._is_binary_read(file_path)
        if hint is not None:
            logger.warning("[VALIDATED_CB] 拒绝读取二进制: path=%s", file_path)
            return ReadResult(error=f"拒绝读取: {hint}", file_data=None)
        return super().read(file_path, offset=offset, limit=limit)

    async def aread(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """异步版 read_file 二进制拦截。"""
        hint = self._is_binary_read(file_path)
        if hint is not None:
            logger.warning("[VALIDATED_CB] 拒绝读取二进制: path=%s", file_path)
            return ReadResult(error=f"拒绝读取: {hint}", file_data=None)
        return await super().aread(file_path, offset=offset, limit=limit)


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

    # ─── SummarizationMiddleware（历史 offload 精确计数版）───
    # deepagents 默认挂的 Summarization 用 count_tokens_approximately（4 字符/token），
    # 对中文低估 ~2 倍（2026-08-12 实测：真实 1501 vs 近似 769），导致 262k 真实 tokens
    # 的上下文不触发 offload → Qwen 400（max context 262144）。这里显式加一个用
    # model.get_num_tokens（tiktoken 兜底）精确计数的实例；默认那个因低估恒 no-op，不冲突。
    def _count_tokens_accurate(messages, *, tools=None) -> int:
        """用 model.get_num_tokens 逐条精确计数（tiktoken 兜底），解决中文低估。"""
        total = 0
        for m in messages:
            content = str(getattr(m, "content", "")) if getattr(m, "content", None) else ""
            if content:
                try:
                    total += model.get_num_tokens(content)
                except Exception:
                    total += len(content)  # 兜底：1 字符≈1 token（足够保守）
        # 临时诊断：仅接近/超过阈值时打（避免每步刷屏）
        if total > 100000:
            logger.warning(
                "[DIAG] _count_tokens_accurate: messages=%d, total=%d tokens（阈值 200000）",
                len(messages), total,
            )
        return total

    summarization_mw = _SummarizationAccurate(
        model=model,
        backend=backend,
        trigger=("tokens", 200000),  # Qwen 262144 的 ~76%，留足输出/工具 schema 余量
        keep=("messages", 10),
        token_counter=_count_tokens_accurate,
    )

    return create_deep_agent(
        model=model,
        tools=tools,
        backend=backend,
        system_prompt=PLC_AUDITOR_SYSTEM_PROMPT,
        skills=skills,
        memory=[AGENTS_MD_PATH],
        middleware=[switch_model, file_to_image, summarization_mw],
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
