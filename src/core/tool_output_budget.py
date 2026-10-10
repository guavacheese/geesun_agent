"""工具结果体积预算中间件：任何单条工具结果都不得无界进入模型上下文。

事故背景（2026-10-10，会话 4863afff）
=====================================
模型跑的 PaddleOCR 脚本把结果（含 base64 图片）整坨 print 到 stdout，单次
``execute`` 返回 **45,777,272 字符（45.8MB）**。deepagents 自带的 offload 安全网
（``FilesystemMiddleware``，阈值 ``NUM_CHARS_PER_TOKEN × 20000`` = 80000 字符）
本该把超限结果搬到 ``/large_tool_results/{tool_call_id}``，但有两条独立缺陷：

1. 该路径在 ``build_backend`` 里路由到 ``StateBackend()`` —— **内容仍在 LangGraph
   state 里**，每次 checkpoint 仍要全量序列化落 Postgres，只是不再进模型上下文；
2. 写入被 ``ValidatedCompositeBackend.ALLOWED_WRITE_PREFIXES`` 拒绝 →
   ``_offload_tool_message_content`` 返回 ``None`` → **原封不动保留 45.8MB**
   （``_message_eviction.py:129`` 原文：*Returns ``None`` if the backend write
   fails — caller should keep the original message in that case.*）。

即：**库自带的 offload 是一条没有备份的独木桥** —— 走得通没事，掉下去就是
45.8MB 砸进上下文。本中间件补的就是"掉下去之后"的那一半，并把落点从 state 挪到
真实磁盘。

与两个参考实现的取向对照
=========================
- **deer-flow**（``agents/middlewares/tool_output_budget_middleware.py``）：走
  **fail-closed**（转存失败必须截断），因为它没有独立的上游 cap。
- **deepseek-harness**（``packages/spill/spill-policy``）：走 **fail-open**（宁可留
  原文也不隐藏结果），因为它强制要求"上游 provider cap 独立存在"——其 README 原话：
  *"提供方／资源上限仍然是必需的，并且与该策略相互独立"*。
- **本项目**：第 2 步已给 ``execute`` 加了 200000 字符的源头 cap
  （``ValidatedCompositeBackend.MAX_EXECUTE_OUTPUT_CHARS``），但 **MCP / read_file
  等非 execute 通道仍无上限** ⇒ 没有"另一半"兜底，故取 **fail-closed**。

为什么主闸门放在 wrap_model_call 而不是 wrap_tool_call
=======================================================
``langchain/agents/factory.py:629`` 的 ``_chain_tool_call_wrappers`` 文档原话是
**"Compose wrappers into middleware stack (first = outermost)"**，而 deepagents 在
``graph.py:367`` 把**用户中间件插在 base stack 之后** ⇒ 我们的 ``wrap_tool_call``
一定落在 ``FilesystemMiddleware`` 的**内层**（先于它拿到原始结果）。由此推出两条
硬约束：

- **不能指望"等库 offload 失败后再兜底"** —— 我们是内层，等不到外层的失败结果。
- **但内层正是我们需要的**：只有在内层改写 result，改写结果才能随返回一路上行、
  最终写进 state ⇒ **state 体积同步下降**（这才叫"根治"，见下"职责边界"）。

故本中间件设计为**两个入口、各司其职**：

1. ``wrap_tool_call`` / ``awrap_tool_call``：结果一出来就判定，先尝试**转存真实磁盘**，
   失败则按 ``fallback_max_chars`` **截断**（fail-closed）。替换后的内容随 result
   进入 state，**同时**解决"撑爆上下文"与"state 肿胀"两个问题。
2. ``wrap_model_call`` / ``awrap_model_call``：出站前对 ``request.messages`` 做一次
   **廉价预扫 + 兜底处置**。它管的是**历史里已经躺着的**超限消息（例如 4863afff 那条
   45.8MB 的 pending write 被 resume 捞回 state 的情形）——这类消息不会再经过
   ``wrap_tool_call``。deepagents 自己的 sweep 只管 ``HumanMessage``
   （``_check_eviction_needed``，``filesystem.py:2126`` 第 4 行即
   ``if not self._human_message_token_limit_before_evict: return False, False``），
   **历史 ToolMessage 它一概不管** —— 这个缺口由本中间件补上。

双阈值语义（与 deer-flow 对齐，但取值理由不同）
================================================
- ``externalize_min_chars``：**超过即转存**。语义不是"超过就危险"，而是"超过就不该
  再占 state 与上下文"。
- ``fallback_max_chars``：**转存失败时的硬上界**（fail-closed：宁可截断，绝不放行
  原文）。它同时就是"任何单条工具结果的内联天花板"这一不变量的载体。

取值与理由（默认 40000 / 60000，均刻意 **小于** deepagents 的 80000 offload 阈值）：
我们因此**永远先于库动作**，结果不会再落进 ``StateBackend``，也不必与库的驱逐逻辑
争抢同一份内容；两个机制不会对同一条结果各写一次。

``exempt_tools`` 为什么必须有
==============================
防 ``read_file → 转存 → 再 read_file`` 死循环（deer-flow 踩过，其
``ToolOutputConfig.exempt_tools`` 默认豁免 read_file）。本项默认豁免集合与 deepagents
的 ``TOOLS_EXCLUDED_FROM_EVICTION``（``filesystem.py:696``）**完全一致**
（ls / glob / grep / read_file / edit_file / write_file）——两者行为因此不会分叉，
一致性由 ``tests/core/test_tool_output_budget.py`` 的依赖契约断言守着。

诚实记录残留风险：豁免意味着这些工具的结果不受本闸门约束，其上界来自各自的自带
限制（``read_file`` 在 ``filesystem.py:1059`` 按 80000 字符自行截断；ls / grep /
glob 输出为结构化 JSON 且受工具参数约束），**并非零风险**，只是换了一层管辖。

职责边界（明确不做什么）
=========================
- 不管 AI 正文 / 用户消息：那是交付物本体与用户输入，本项目已在持久化层明确
  "不截"（``persist_max_content_chars = 0``），此处保持一致。
- 不解决"多次小量累积"：多条各 3 万字符的结果累积仍会触发 Summarization，那是对
  的另一层（``summarization_mw`` trigger=("fraction", 0.8)）。
- 不管子代理内部执行：本中间件挂在主 agent 的 middleware 链上。

--- 设计对齐记录：deer-flow / deepseek-harness 对照见 2026-10-10 工作区 memory ---
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace as dc_replace
from typing import TYPE_CHECKING, Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deepagents.backends.utils import sanitize_tool_call_id

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol

logger = logging.getLogger(__name__)

# ─── 转存落点：复用 deepagents 自带前缀 ───
# 为什么复用 ``/large_tool_results/`` 而不是另起一个前缀（两个理由）：
# ① 系统提示词里已经写死了这个前缀（``filesystem.py:548`` 原文："Offloaded tool
#    results are stored under `{large_tool_results_prefix}/<tool_call_id>`"）——换前缀
#    就得同步造一套提示词，否则模型不知道怎么去找回；
# ② 万一本中间件被关掉（``enabled=False`` 回滚开关），deepagents 自己的 offload 写的
#    是同一个目录，两套机制不会分叉成两个位置、两种取回话术。
LARGE_TOOL_RESULTS_PREFIX = "/large_tool_results"

# additional_kwargs 里的处置标记（供排障/端到端断言用；不参与模型提示词）
TRANSFORM_KEY = "tool_output_budget"

_SEP = "─" * 60

# 转存成功：给路径 + 取回方式 + 头尾预览
_EXTERNALIZED_HEADER = (
    "[工具结果超限——已转存到虚拟文件系统]\n"
    "- 工具：{tool}\n"
    "- 原始体积：{total} 字符；转存阈值 {limit} 字符\n"
    "- 完整内容：{path}\n"
    "- 取回方式：read_file(file_path='{path}', offset=0, limit=200) 分段读取；"
    "不知道确切文件名时先 ls {prefix}/，或用 grep(path='{prefix}/') 检索\n"
    "- 以下为头尾预览（中间部分已省略）：\n"
)

# 转存失败：必须截断（fail-closed），且**必须自报**"完整内容不可取回"。
# 注意此处报的是**内联硬上界**（fallback_max_chars）而非转存阈值 —— 失败路径下真正
# 决定模型看到多长的是前者，报错数字才与实际一致（否则模型/排障者会误判）。
_TRUNCATED_HEADER = (
    "[工具结果超限且转存失败——已强制截断]\n"
    "- 工具：{tool}\n"
    "- 原始体积：{total} 字符；内联硬上界 {limit} 字符\n"
    "- 转存虚拟文件系统失败，**完整内容不可取回**；本条只保留头尾，中间已丢弃\n"
    "- 建议：命令只输出汇总行，或把输出重定向到文件后分段 read_file\n"
    "- 以下为头尾预览：\n"
)

# 默认阈值（可被构造器 / settings 覆盖）
_DEFAULT_EXTERNALIZE_MIN_CHARS = 40_000
_DEFAULT_FALLBACK_MAX_CHARS = 60_000
_DEFAULT_PREVIEW_HEAD_CHARS = 2_000
_DEFAULT_PREVIEW_TAIL_CHARS = 2_000

# 与 deepagents ``TOOLS_EXCLUDED_FROM_EVICTION``（filesystem.py:696）逐项一致；
# 一致性由 tests/core/test_tool_output_budget.py 的依赖契约断言守护。
_DEFAULT_EXEMPT_TOOLS = frozenset({
    "ls",
    "glob",
    "grep",
    "read_file",
    "edit_file",
    "write_file",
})


# ─── 文本提取 / 重建 ─────────────────────────────────────


def _extract_text(content: Any) -> str | None:
    """取出 content 里的**纯文本**部分用于体积判定；无文本则返回 None。

    与 deepagents ``_extract_text_from_message``（``_message_eviction.py:66``）同思路：
    非文本块（image_url 等）**不参与体积测量** —— 图片是合法内容且 base64 天然巨大，
    拿它当"超大工具结果"去截断是误伤。``None`` 表示"这条没有可预算的文本"，直接跳过。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                pieces.append(part["text"])
        return "\n".join(pieces) if pieces else None
    return None


def _estimate_text_len(content: Any) -> int:
    """只算长度、不拼字符串 —— 供 ``wrap_model_call`` 的**廉价预扫**用。

    预扫跑在每次模型调用前。若在这里 join 出一个 45MB 的字符串，代价本身就是事故的
    一部分；故对 str 直接 ``len()``（O(1)），对 list 只累加各文本块长度（不构造）。
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, str):
                total += len(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                total += len(part["text"])
        return total
    return 0


def _rebuild_content(original: Any, replacement: str) -> str | list[Any]:
    """把 content 换成 replacement，但**保留非文本块**。

    对齐 deepagents ``_build_evicted_content``（``_message_eviction.py:82``）：多模态
    工具结果被处置后，图片等块必须原样留下，否则等于静默丢上下文。
    （deer-flow 的实现（``_patch_tool_message``）在此处直接 ``{"content": replacement}``，
    会连带丢掉图片块 —— 这是刻意不照搬的一点。）
    """
    if isinstance(original, list):
        kept = [
            part
            for part in original
            if not (
                isinstance(part, str)
                or (isinstance(part, dict) and isinstance(part.get("text"), str))
            )
        ]
        if not kept:
            return replacement
        return [{"type": "text", "text": replacement}, *kept]
    return replacement


def _build_body(
    text: str,
    budget: int,
    head_pref: int,
    tail_pref: int,
) -> str:
    """在 ``budget`` 字符内构造"头 + 省略标记 + 尾"的预览。

    头尾都要留：脚本的最终结论、错误 traceback、以及 langchain-cubesandbox 拼在
    output 末尾的 stderr（``sandbox.py:471``）都落在尾部；而命令回显/上下文在头部。

    **长度保证**：返回串长度恒 ≤ ``budget``。生产配置下 body 预算约 59700 字符，
    远大于头尾预览之和（4000）⇒ 永不触发降级分支；该分支只在阈值被配置得极小
    （单测/排障）时生效，此时按"先砍尾、再砍头"的顺序压缩（头部含命令回显，优先留）。
    """
    if budget <= 0:
        return ""
    head_n = min(head_pref, budget)
    tail_n = min(tail_pref, max(0, budget - head_n))
    head = text[:head_n]
    tail = text[len(text) - tail_n :] if tail_n else ""
    omitted = max(0, len(text) - len(head) - len(tail))
    marker = f"\n... [中间 {omitted} 字符已省略] ...\n"
    if len(head) + len(tail) + len(marker) > budget:
        # 预算极小：标记先退化为最短形态，仍超则继续压缩头尾
        marker = "\n...\n"
        overflow = len(head) + len(tail) + len(marker) - budget
        if overflow > 0:
            cut = min(overflow, len(tail))
            tail = tail[: len(tail) - cut]
            overflow -= cut
            if overflow > 0:
                head = head[: max(0, len(head) - overflow)]
        # 预算连标记本身都放不下（阈值被配置得极小）：标记也按剩余预算截断
        remaining = budget - len(head) - len(tail)
        if remaining < len(marker):
            marker = marker[: max(remaining, 0)]
    return head + marker + tail


class ToolOutputBudgetMiddleware(AgentMiddleware[AgentState]):
    """给单条工具结果加体积预算：超限则转存真实磁盘，转不动则 fail-closed 截断。

    为什么需要它（一句话）：**上游总会有管不到的通道**（MCP、read_file、未来新增的
    工具），而"context 被单条结果撑爆"这件事只要发生一次就是灾难级（4863afff 就是）。
    与其逐通道补 cap，不如在"结果进 state / 进上下文"的必经之路上设一道不依赖上游
    自觉的闸门。

    不变式（两条，由单测守着）：
    1. 任何**非豁免**工具结果，进入 state / 模型上下文时长度**不超过**
       ``fallback_max_chars``（转存成功则是"指针 + 预览"，远小于阈值）；
    2. 该闸门**自身出错时绝不让一次成功的工具调用变成失败** —— 沿用第 2 步
       ``_clamp_execute_output`` 的同一条取舍：兜底闸门宁可退化，不可制造新错误。
    """

    def __init__(
        self,
        backend: BackendProtocol,
        *,
        enabled: bool = True,
        externalize_min_chars: int = _DEFAULT_EXTERNALIZE_MIN_CHARS,
        fallback_max_chars: int = _DEFAULT_FALLBACK_MAX_CHARS,
        preview_head_chars: int = _DEFAULT_PREVIEW_HEAD_CHARS,
        preview_tail_chars: int = _DEFAULT_PREVIEW_TAIL_CHARS,
        exempt_tools: frozenset[str] | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.enabled = enabled
        self.externalize_min_chars = externalize_min_chars
        self.fallback_max_chars = fallback_max_chars
        self.preview_head_chars = preview_head_chars
        self.preview_tail_chars = preview_tail_chars
        self.exempt_tools = (
            _DEFAULT_EXEMPT_TOOLS if exempt_tools is None else frozenset(exempt_tools)
        )
        # 预扫阈值 = 两个阈值里更小的那个（>0 者）。<= 0 语义为"该阈值不限制"，
        # 与第 2 步 MAX_EXECUTE_OUTPUT_CHARS <= 0 的约定一致；两者都不限制时预扫直接跳过。
        positives = [
            v for v in (externalize_min_chars, fallback_max_chars) if v > 0
        ]
        self._pre_scan_trigger = min(positives) if positives else 0

    # ─── 判定 ─────────────────────────────────────────────

    def _text_budget(self) -> int:
        """替换文本的硬上界：优先取 fallback_max_chars，退化到 externalize 阈值。"""
        if self.fallback_max_chars > 0:
            return self.fallback_max_chars
        return max(self.externalize_min_chars, 0)

    def _over_budget_len(self, content: Any) -> int | None:
        """廉价判定：返回需要处置的原文长度，不需要则 None（不构造任何字符串）。"""
        trigger = self._pre_scan_trigger
        if trigger <= 0:
            return None
        length = _estimate_text_len(content)
        return length if length > trigger else None

    def _plan(self, message: ToolMessage) -> tuple[str, int] | None:
        """判定单条 ToolMessage 是否需要处置；需要则返回 (全文, 长度)。

        两个阈值任一被超过即需处置（``>0`` 才生效，``<=0`` 表示该阈值不限制）：
        - 超 ``externalize_min_chars`` → 应转存（不该再占 state / 上下文）；
        - 超 ``fallback_max_chars`` → 必须落到硬上界内（即便转存失败）。

        度量口径说明：预扫（``_over_budget_len``）对 list content 用**累加**，此处用
        **join**（多个文本块之间会多出 n-1 个换行）。两者在阈值附近可能有 n-1 字符的
        偏差 ⇒ 极端边界上预扫可能漏判，代价仅是"该条本轮不被处置、留待下一轮"，
        相对 40000 量级的阈值可忽略；换来的是预扫**不必构造大字符串**。
        """
        if (message.name or "") in self.exempt_tools:
            return None
        text = _extract_text(message.content)
        if text is None:
            return None
        length = len(text)
        over_externalize = (
            self.externalize_min_chars > 0 and length > self.externalize_min_chars
        )
        over_ceiling = self.fallback_max_chars > 0 and length > self.fallback_max_chars
        if not (over_externalize or over_ceiling):
            return None
        return text, length

    # ─── 落盘 ─────────────────────────────────────────────

    @staticmethod
    def _externalize_path(message: ToolMessage, text: str) -> str:
        """内容寻址的转存路径：``{tool_call_id}-{sha1 前 12 位}.txt``。

        **为什么带内容哈希**（两个作用，都不是美化）：
        - 幂等：同一条结果被处置两次（如工具调用重跑）会落到同一路径，
          ``FilesystemBackend.write`` 的"拒绝覆盖已存在文件"（``filesystem.py:487``）
          恰好等价于"文件已经在盘上、内容一致" ⇒ 可安全判定为成功；
        - 防陈旧：若同一次工具调用产出**不同内容**，哈希不同 ⇒ 新路径，
          不可能出现"指针指向上一轮的旧内容"这种静默错误。
        """
        raw_id = message.tool_call_id or "unknown"
        safe_id = sanitize_tool_call_id(raw_id)
        digest = hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]
        return f"{LARGE_TOOL_RESULTS_PREFIX}/{safe_id}-{digest}.txt"

    @staticmethod
    def _write_ok(result: Any) -> bool:
        """写是否算成功。

        唯一被当作成功的错误是"文件已存在"：文件名是内容寻址的（见
        ``_externalize_path``），已存在即证明**同内容已在盘上**、指针有效。
        其余一切错误（白名单拒绝、磁盘满、权限）都算失败 → 走 fail-closed 截断。
        """
        if result is None:
            return False
        error = getattr(result, "error", None)
        if not error:
            return True
        return "already exists" in str(error)

    def _try_write_sync(self, message: ToolMessage, text: str) -> str | None:
        path = self._externalize_path(message, text)
        try:
            result = self.backend.write(path, text)
        except Exception:
            logger.exception("[TOOL_BUDGET] 转存工具结果抛异常（同步路径）: path=%s", path)
            return None
        if not self._write_ok(result):
            logger.warning(
                "[TOOL_BUDGET] 转存工具结果失败: path=%s | error=%s",
                path, getattr(result, "error", None),
            )
            return None
        return path

    async def _try_write_async(self, message: ToolMessage, text: str) -> str | None:
        path = self._externalize_path(message, text)
        try:
            result = await self.backend.awrite(path, text)
        except Exception:
            logger.exception("[TOOL_BUDGET] 转存工具结果抛异常（异步路径）: path=%s", path)
            return None
        if not self._write_ok(result):
            logger.warning(
                "[TOOL_BUDGET] 转存工具结果失败: path=%s | error=%s",
                path, getattr(result, "error", None),
            )
            return None
        return path

    # ─── 改写 ─────────────────────────────────────────────

    def _build_replacement(self, message: ToolMessage, text: str, path: str | None) -> str:
        """构造替换文本，并**保证其长度 ≤ ``fallback_max_chars``**（不变式 1）。

        两条路径报的"上限"数字**不同且各自正确**：转存成功报转存阈值
        （``externalize_min_chars``，它是触发本次处置的那个数），转存失败报内联硬上界
        （``fallback_max_chars``，它才是决定模型实际看到多长的那个数）。
        """
        tool = message.name or "unknown"
        ceiling = self._text_budget()
        if path is not None:
            header = _EXTERNALIZED_HEADER.format(
                tool=tool, total=len(text),
                limit=self.externalize_min_chars
                if self.externalize_min_chars > 0
                else ceiling,
                path=path, prefix=LARGE_TOOL_RESULTS_PREFIX,
            )
        else:
            header = _TRUNCATED_HEADER.format(tool=tool, total=len(text), limit=ceiling)
        if ceiling > 0:
            body_budget = ceiling - len(header) - 2 * (len(_SEP) + 1)
        else:
            # 上限被关闭（≤0）：预览就用配置的头尾长度，不再卡总长
            body_budget = self.preview_head_chars + self.preview_tail_chars
        body = _build_body(
            text,
            max(body_budget, 0),
            self.preview_head_chars,
            self.preview_tail_chars,
        )
        replacement = header + _SEP + "\n" + body + "\n" + _SEP
        if ceiling > 0 and len(replacement) > ceiling:
            # 兜底中的兜底：阈值被配置得比通知文本还短（如 ceiling < 400）。
            # "内联长度不得超过硬上界"是不变式，优先级高于"通知必须完整"——
            # 因为长度失控是灾难级、通知不全是可接受的退化。
            logger.warning(
                "[TOOL_BUDGET] 内联上界 %d 小于通知文本长度 %d，截断通知（阈值配置过小）",
                ceiling, len(replacement),
            )
            replacement = replacement[:ceiling]
        return replacement

    def _rebuild(self, message: ToolMessage, text: str, path: str | None) -> ToolMessage:
        replacement = self._build_replacement(message, text, path)
        kwargs = dict(message.additional_kwargs or {})
        kwargs[TRANSFORM_KEY] = {
            "transform": "externalized" if path is not None else "truncated",
            "original_chars": len(text),
            "path": path,
        }
        new_message = message.model_copy(
            update={
                "content": _rebuild_content(message.content, replacement),
                "additional_kwargs": kwargs,
            }
        )
        logger.warning(
            "[TOOL_BUDGET] %s: tool=%s 原始=%d 字符 → 内联=%d 字符%s",
            "已转存" if path is not None else "**已截断（转存失败）**",
            message.name or "unknown",
            len(text),
            len(replacement),
            f" | path={path}" if path else "",
        )
        return new_message

    def _process_sync(self, message: ToolMessage) -> ToolMessage:
        plan = self._plan(message)
        if plan is None:
            return message
        text, _total = plan
        return self._rebuild(message, text, self._try_write_sync(message, text))

    async def _process_async(self, message: ToolMessage) -> ToolMessage:
        plan = self._plan(message)
        if plan is None:
            return message
        text, _total = plan
        return self._rebuild(message, text, await self._try_write_async(message, text))

    # ─── 结果改写（ToolMessage / Command 两种形态）─────────

    def _patch_result_sync(self, result: ToolMessage | Command[Any]) -> ToolMessage | Command[Any]:
        if isinstance(result, ToolMessage):
            return self._process_sync(result)
        return self._patch_command_sync(result)

    async def _patch_result_async(
        self, result: ToolMessage | Command[Any]
    ) -> ToolMessage | Command[Any]:
        if isinstance(result, ToolMessage):
            return await self._process_async(result)
        return await self._patch_command_async(result)

    def _patch_command_sync(self, result: Command[Any]) -> Command[Any]:
        update = getattr(result, "update", None)
        if not isinstance(update, dict):
            return result
        messages = update.get("messages")
        if not isinstance(messages, list):
            return result
        patched: list[Any] = []
        changed = False
        for msg in messages:
            new_msg = self._process_sync(msg) if isinstance(msg, ToolMessage) else msg
            changed = changed or new_msg is not msg
            patched.append(new_msg)
        return dc_replace(result, update={**update, "messages": patched}) if changed else result

    async def _patch_command_async(self, result: Command[Any]) -> Command[Any]:
        update = getattr(result, "update", None)
        if not isinstance(update, dict):
            return result
        messages = update.get("messages")
        if not isinstance(messages, list):
            return result
        patched: list[Any] = []
        changed = False
        for msg in messages:
            new_msg = (
                await self._process_async(msg) if isinstance(msg, ToolMessage) else msg
            )
            changed = changed or new_msg is not msg
            patched.append(new_msg)
        return dc_replace(result, update={**update, "messages": patched}) if changed else result

    # ─── 钩子 1/2：工具结果侧（唯一能在写 state 前改写内容的位置）───

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        result = handler(request)
        if not self.enabled:
            return result
        return self._patch_result_sync(result)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        if not self.enabled:
            return result
        return await self._patch_result_async(result)

    # ─── 钩子 2/2：模型请求侧（历史消息兜底）───────────────

    def _over_budget_indices(self, messages: list[Any]) -> list[int] | None:
        """廉价预扫：返回需要处置的下标；没有则 None（常见情形，避免重建整个消息列表）。"""
        indices = [
            i
            for i, msg in enumerate(messages)
            if isinstance(msg, ToolMessage)
            and (msg.name or "") not in self.exempt_tools
            and self._over_budget_len(msg.content) is not None
        ]
        return indices or None

    def _sweep_sync(self, messages: list[Any]) -> list[Any] | None:
        indices = self._over_budget_indices(messages)
        if indices is None:
            return None
        updated = list(messages)
        for i in indices:
            updated[i] = self._process_sync(updated[i])
        return updated

    async def _sweep_async(self, messages: list[Any]) -> list[Any] | None:
        indices = self._over_budget_indices(messages)
        if indices is None:
            return None
        updated = list(messages)
        for i in indices:
            updated[i] = await self._process_async(updated[i])
        return updated

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        if self.enabled:
            updated = self._sweep_sync(list(request.messages))
            if updated is not None:
                request = request.override(messages=updated)
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        if self.enabled:
            updated = await self._sweep_async(list(request.messages))
            if updated is not None:
                request = request.override(messages=updated)
        return await handler(request)
