import os
import re
import asyncio
import logging
import concurrent.futures
from typing import Annotated, NotRequired

logger = logging.getLogger(__name__)

from deepagents import create_deep_agent
from deepagents.backends import (
    CompositeBackend,
    FilesystemBackend,
    LocalShellBackend,
    StateBackend,
    StoreBackend,
)
from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import file_data_to_string
from deepagents.graph import DeepAgentState
from deepagents.middleware.summarization import (
    SummarizationEvent,
    SummarizationMiddleware,
    SummarizationState,
)
from deepagents.middleware.skills import (
    SkillsMiddleware,
    SkillsStateUpdate,
    _alist_skills_with_errors,
    _list_skills_with_errors,
)
from langchain.agents.middleware.types import AgentMiddleware, PrivateStateAttr
from src.core import execution_guard
from src.core.loop_detection import LoopDetectionMiddleware
from src.core.subagent_budget import SubagentBudgetMiddleware
# D3（2026-11-19）：显式声明 general-purpose 子代理时复用 deepagents 的默认
# 文案常量（导入而非抄写——升级时文案自动跟随，只锁"有保险丝"这一行为）。
from deepagents.middleware.subagents import (
    DEFAULT_GENERAL_PURPOSE_DESCRIPTION,
    DEFAULT_SUBAGENT_PROMPT,
)
from src.core.skill_md import validate_skill_md
from src.core.terminal_response import TerminalResponseMiddleware


def _summarization_event_last_wins(old, new):
    """`_summarization_event` 的 last-write-wins reducer（F4，2026-11-17）。

    deepagents 的 SummarizationState 把该字段标为 PrivateStateAttr（langgraph
    LastValue channel）：同一 super-step 收到两个更新即抛
    InvalidUpdateError('Can receive only one value per step')。生产 GY35362:3c4b5588
    在 crash 轮遗留 pending writes 后，恢复/快照读取重放与本次写入撞同一 step →
    _drain_astream 与 _persist_session→aget_state 全炸，线程永久 brick（2026-11-17
    15:54-15:57 反复复现）。

    本 reducer 使字段成为 BinaryOperatorAggregate channel（langgraph
    state.py:_is_field_binop 识别 Annotated meta[-1] 为 2 参数 callable）：
    同 step 多个更新逐个 fold、最后写入者赢，pending writes 重放不再炸。
    覆盖路径为 langchain 官方扩展点（factory.py:1229 注释："base_state last
    so it wins any field conflict... explicit state_schema override middleware
    annotations"）——自定义 schema 经 create_deep_agent(state_schema=...) 传入，
    聚合列表中排最后，覆盖 deepagents SummarizationState 的同名字段注解。

    None 防御：deepagents 三个写入点（summarization.py:1613/1752/2091）全部
    写非 None 的 new_event，从不写 None 清空；None 仅出现在「从未有过 event」的
    初始 state（BinaryOperatorAggregate 初始 MISSING → 首值直接落位），
    这里保留旧值仅为防御未来版本行为变化。
    """
    return new if new is not None else old


def _merge_skill_disclosures(old, new):
    """`_skill_tools_disclosed` 的合并 reducer（1.0.53 回滚事故修复，2026-11-19）。

    生产装配有两个 SkillsMiddleware 实例（create_deep_agent(skills=) 默认实例 +
    _FreshSkillsMiddleware 追加实例，不同 name 不去重）。deepagents 0.7.24 起
    wrap_model_call **无条件**返回 Command(update={_skill_tools_disclosed:
    record})（skills.py:1342/1364）→ 同一 model task 同 super-step 双写；父类
    声明是 LastValue（PrivateStateAttr 非 reducer）→ InvalidUpdateError，
    每轮必炸（1.0.53 上线 14 分钟回滚；Postgres checkpoint_writes 实证同
    task_id idx=1/2 两行同 key，thread GY34035:2b3a6c6d）。

    本 reducer 使字段成为 BinaryOperatorAggregate channel（识别规则同
    _summarization_event_last_wins：Annotated meta 末尾 2 参 callable）：
    双写逐个 fold。语义 = 两实例披露记录（tool→skill 名映射）取并集、同键
    后者赢——两实例读同一 state 做披露，record 本就同源，并集无信息损失。

    None 容错为防御性（0.7.24 两个写点恒传 dict，含空 {}）；reducer 返回
    dict 不会是 None，通道首值由 BinaryOperatorAggregate 直接落位。
    """
    return {**(old or {}), **(new or {})}


class GeesunAgentState(DeepAgentState):
    """geesun 侧 agent state schema：覆盖 deepagents 的两个并发写通道。

    ⚠ 必须经 create_deep_agent(state_schema=GeesunAgentState) 传入才生效——
    聚合语义见 _summarization_event_last_wins docstring。运行时 import 不可
    移入 TYPE_CHECKING：private_state_field_names 用 get_type_hints 解析
    注解，TYPE_CHECKING-only 名字会导致该 schema 被跳过并告警（deepagents
    _state.py:27-33）。"""

    _summarization_event: Annotated[
        NotRequired[SummarizationEvent | None],
        PrivateStateAttr,
        _summarization_event_last_wins,
    ]

    # 0.7.24 skills 披露双实例双写（事故复盘见 _merge_skill_disclosures
    # docstring）。注解形态与 _summarization_event 同构：NotRequired 在
    # Annotated 内层、reducer 居 meta 末尾——顺序错会被 langgraph 静默当作
    # LastValue（tests/core/test_skill_disclosure_channel.py 行为级锁死）。
    _skill_tools_disclosed: Annotated[
        NotRequired[dict[str, str]],
        PrivateStateAttr,
        _merge_skill_disclosures,
    ]


def _strip_system_messages(request):
    """过滤 request.messages 中的 SystemMessage（model 调用前最终清洗）。

    vLLM 要求所有 system 消息连续位于开头；主 system 由 request.system_message
    字段承载（memory/skills middleware 用 append_to_system_message 注入），
    messages 中出现 system 均为异常数据——历史 checkpoint 可能残留旧版 P0
    收敛注入的 SystemMessage（2026-08-19 实测重启恢复后第一轮即 400
    'System message must be at the beginning'）。

    返回清洗后的新 request；无 system 时返回 None（调用方直接用原 request）。
    """
    raw = request.messages
    clean = [m for m in raw if getattr(m, "type", "") != "system"]
    if len(clean) != len(raw):
        logger.warning(
            "[DIAG] wrap_model_call 清洗 %d 条中间 SystemMessage（残留: %s）",
            len(raw) - len(clean),
            [getattr(m, "type", "?") for m in raw],
        )
        return request.override(messages=clean)
    return None


class _FreshSkillsMiddleware(SkillsMiddleware):
    """每轮强制重扫 skill 源，绕过 deepagents 的 checkpoint 固化。

    问题（2026-08-19 实测，Postgres checkpoint_writes 实证）：
    deepagents 默认 SkillsMiddleware 的 before_agent/abefore_agent 在
    `"skills_metadata" in state` 时直接 return None（skills.py:960/1006）——
    skills_metadata 只标 PrivateStateAttr（控制 input/output schema 省略），
    无 EphemeralValue，会被 Postgres checkpoint 持久化。会话首扫后永不重扫，
    会话创建之后新上传的 skill 永远不可见（tech-spec-pdf-diff 8-18 19:43
    上传，会话 dda7a644 首扫 15:45，此后 agent 上下文永远没有该 skill，
    只能自写逐页 diff 脚本自由发挥，引发死循环）。

    本子类职责：
    - before_agent/abefore_agent：每轮重新扫描 sources 并刷新 state 里的
      skills_metadata（与旧值相同则返回 None，避免每 step 重复写 checkpoint）；
    - modify_request：no-op（不重复注入 system prompt）——注入仍由
      create_deep_agent 内部按 skills= 参数装配的默认 SkillsMiddleware 完成，
      它读取的是本实例刷新后的 state。

    装配：create_agent 保留 skills= 参数（默认实例负责注入 + GP subagent
    覆盖），本实例追加进 middleware 列表（name=_FreshSkillsMiddleware，
    与默认 SkillsMiddleware 不同名，langchain factory 按 name 去重不冲突）。
    """

    def _scan(self, state, backend):
        all_skills: dict[str, dict] = {}
        errors: list[str] = []
        for source_path in self.sources:
            source_skills, source_error = _list_skills_with_errors(backend, source_path)
            if source_error is not None:
                errors.append(source_error)
            for skill in source_skills:
                all_skills[skill["name"]] = skill
        skills = list(all_skills.values())
        if state.get("skills_metadata") == skills and not errors:
            return None
        update = SkillsStateUpdate(skills_metadata=skills)
        if errors:
            logger.warning("[FRESH_SKILLS] 加载错误: %s", errors)
            update["skills_load_errors"] = errors
        return update

    async def _ascan(self, state, backend):
        all_skills: dict[str, dict] = {}
        errors: list[str] = []
        for source_path in self.sources:
            source_skills, source_error = await _alist_skills_with_errors(backend, source_path)
            if source_error is not None:
                errors.append(source_error)
            for skill in source_skills:
                all_skills[skill["name"]] = skill
        skills = list(all_skills.values())
        if state.get("skills_metadata") == skills and not errors:
            return None
        update = SkillsStateUpdate(skills_metadata=skills)
        if errors:
            logger.warning("[FRESH_SKILLS] 加载错误: %s", errors)
            update["skills_load_errors"] = errors
        return update

    def before_agent(self, state, runtime, config):
        """每轮重扫（不做 checkpoint 固化 skip）。"""
        # 0.7 变化（2026-11-19）：backend 改为构造时存储的 self._backend，
        # _get_backend(state, runtime, config) 已不存在（0.6.12 旧 API）。
        backend = self._backend
        return self._scan(state, backend)

    async def abefore_agent(self, state, runtime, config):
        """每轮重扫（异步版，不做 checkpoint 固化 skip）。"""
        backend = self._backend
        return await self._ascan(state, backend)

    def modify_request(self, request):
        """no-op：注入交给默认 SkillsMiddleware，避免 system prompt 重复两段。"""
        return request


class _SummarizationAccurate(SummarizationMiddleware):
    """精确计数版 Summarization：实现与默认完全一致，token_counter 更准。

    ⚠ 挂载机制（2026-11-19 动作三，deepagents 0.7）：本类实例包在
    _SweepThenSummarize 组合件里、经 user middleware 传入，由 0.7 的
    _apply_custom_middleware 按 name="SummarizationMiddleware" 原位替换主链
    与 gp subagent 的默认实例（单实例）。0.6.12 时代的 monkey-patch 方案已
    摘除——彼时无官方替换入口（user middleware 传法双实例 GY35362 / 同名
    duplicate 断言 1.0.48），历史方案见 git c49806d 与模块头注释。

    差异：挂载用 model.get_num_tokens 精确计数的 token_counter（中文 token 低估修复）。
    2026-08-17 追加：① cutoff 越界防御（恢复会话时 cutoff > 消息数的 bug）；
    ② 摘要生成后注入"当前会话真实资源清单"，避免 agent 恢复后 read_file 猜路径。
    """
    def __init__(self, *args, inventory_provider=None, partial_token_counter=None, **kwargs):
        super().__init__(*args, **kwargs)
        # 回调返回"当前会话真实资源清单"文本（uploads/reports 文件 + 沙箱状态），
        # 由 create_agent 注入闭包（持有 user_id/session_id/sandbox 上下文）
        self._inventory_provider = inventory_provider
        self._install_partial_token_counter(partial_token_counter)

    def _install_partial_token_counter(self, partial_token_counter) -> None:
        """补上框架只在"默认 counter 分支"才做的语义拆分（keep 用 fraction 的前提）。

        langchain `agents/middleware/summarization.py:344-352` 把计数分成两个语义：

        | 调用点 | 传入的 messages | 需要的语义 |
        |---|---|---|
        | `_should_summarize`（trigger 判断） | **整个请求**的完整列表 | 这个请求占多少 token |
        | `_find_token_based_cutoff`（保留预算二分） | `messages[mid:]` **任意切片** | 这个子集占多少 token |

        框架知情，但**只在 `token_counter is count_tokens_approximately` 时**自动拆分
        （给 partial 加 `use_usage_metadata_scaling=False`）；一旦传自定义 counter，
        它直接 `self._partial_token_counter = token_counter`（同一函数）。
        ⚠ 0.7.24/langchain 1.4.4 复核（2026-11-19）：语义原样保留
        （langchain summarization.py:382-389，_find_token_based_cutoff:712 仍用
        _partial_token_counter），本 hack 继续有效。

        本项目的 `_engine_grounded_counter` 是**为 trigger 语义写的**——命中每会话缓存时
        直接返回引擎真实 prompt_tokens，**忽略 messages 入参**。若 keep 用 fraction/tokens，
        二分每步都拿到同一个数 → 全部走 `left = mid + 1` → `cutoff_candidate` 被顶到
        `len(messages) - 1` → **压缩后只保留最后 1 条消息**。

        2026-09-12 实测（`tests/spikes/summarization_keep_semantics.py`，ast 提取 langchain
        真实方法执行）：30 条消息 / 窗口 262144 / keep fraction 0.10 下，
        未拆分 → cutoff 29（保留 1 条）；拆分后 → cutoff 22（保留 8 条，符合预算）。

        拆分后的语义是安全的：trigger 仍用引擎真值（准确度不丢），只有"保留多少"的二分
        用子集感知的本地估算——它只决定切在哪，多留几条不会 400，而少留才会丢上下文。
        """
        if partial_token_counter is None:
            return
        helper = getattr(self, "_lc_helper", None)
        if helper is None:
            logger.warning(
                "[DIAG] 未找到 _lc_helper，_partial_token_counter 未替换；"
                "keep 若为 fraction/tokens 会退化，请检查 deepagents 版本",
            )
            return
        helper._partial_token_counter = partial_token_counter

    def _partition_messages(self, conversation_messages, cutoff_index):
        """防御：checkpoint 恢复时 cutoff_index 可能超过当前消息数（实测 404 > 21），
        超界会导致剩余切片为空并产生异常路径。裁剪到消息总数后再走默认逻辑。"""
        n = len(conversation_messages)
        if cutoff_index > n:
            logger.warning(
                "[DIAG] summarization cutoff_index=%s > messages=%s，裁剪到 %s",
                cutoff_index, n, n,
            )
            cutoff_index = n
        return super()._partition_messages(conversation_messages, cutoff_index)

    def _build_new_messages_with_path(self, summary, file_path):
        """在摘要消息后追加"真实资源清单"（HumanMessage），agent 恢复时不猜路径。

        用 HumanMessage 而非 SystemMessage：OpenAI 兼容 API（vLLM）要求 system 消息
        连续位于开头，恢复的 checkpoint 消息里可能混有 system（2026-08-17 实测
        insert(0) 后仍 400 'System message must be at the beginning'）；
        HumanMessage 无 role 位置约束，语义上也更贴近"对话中的资源事实陈述"。
        """
        msgs = super()._build_new_messages_with_path(summary, file_path)
        if self._inventory_provider is not None:
            try:
                inventory = self._inventory_provider()
                if inventory:
                    msgs.append(HumanMessage(content=inventory))
            except Exception as e:
                logger.warning("[DIAG] inventory 注入失败: %s", e)
        return msgs

    def _apply_event_to_messages(self, messages, event):
        """恢复会话时应用已保存的 summarization event（修复 2026-08-17 实测两问题）。

        ① checkpoint 里保存的 cutoff_index 是历史值（如 404），恢复后的消息数
           远小于它（如 21/32）→ 原实现（@staticmethod）超界时只返回
           [summary_msg]，把保留消息全丢，agent 恢复后无上下文可循 →
           read_file 猜路径 5 连败触发 M3；
        ② 恢复路径原本没有资源清单注入（原注入只在"新触发 summarization"
           _build_new_messages_with_path 时发生），这里补齐。
        """
        if event is None:
            return list(messages)
        try:
            summary_msg = event["summary_message"]
            cutoff_idx = event["cutoff_index"]
        except (KeyError, TypeError) as exc:
            logger.warning("Malformed _summarization_event (missing keys): %s", exc)
            return list(messages)

        n = len(messages)
        if cutoff_idx > n:
            # 修复①：超界不丢保留消息（原实现 return [summary_msg]）
            logger.warning(
                "[DIAG] _apply_event_to_messages: cutoff=%s > messages=%s，保留全部消息",
                cutoff_idx, n,
            )
            result = [summary_msg, *messages]
        else:
            result = [summary_msg]
            result.extend(messages[cutoff_idx:])

        # 诊断：打印消息类型序列，定位 400 'System message must be at the beginning' 来源
        if logger.isEnabledFor(logging.DEBUG) or True:
            types = [type(m).__name__ for m in result]
            logger.warning(
                "[DIAG] 恢复后消息序列: count=%d types=%s",
                len(types), types[:10],
            )

        # 修复②：恢复路径同样注入资源清单（用 HumanMessage，无 role 位置约束；
        # SystemMessage 会与 checkpoint 历史中可能存在的 system 消息冲突导致 400）
        if self._inventory_provider is not None:
            try:
                inventory = self._inventory_provider()
                if inventory:
                    result.append(HumanMessage(content=inventory))
            except Exception as e:
                logger.warning("[DIAG] inventory 注入失败(恢复路径): %s", e)
        return result

    # ─── 中间 SystemMessage 兜底清洗（2026-08-19 04:xx 第三次 400 修复）───
    # wrap_model_call 是 model 调用前的最后一环：此时 request.messages 已包含
    # checkpoint 恢复的历史消息 + 本轮的注入/新消息，是"最终完整列表"。
    # vLLM 要求所有 system 连续位于开头，历史 checkpoint 里残留的中间 system
    # （旧版 P0 收敛注入 SystemMessage 等）会永久触发 400——在入口（_drain_astream）
    # 清洗覆盖不到 deepagents 内部恢复的历史，必须在此最终清洗。
    # 主 system 在 request.system_message 字段（memory/skills 用 append_to_system_message
    # 注入），messages 中出现 system 本就是异常数据，全部过滤安全。

    def wrap_model_call(self, request, handler):
        cleaned = _strip_system_messages(request)
        if cleaned is not None:
            request = cleaned
        return super().wrap_model_call(request, handler)

    async def awrap_model_call(self, request, handler):
        cleaned = _strip_system_messages(request)
        if cleaned is not None:
            request = cleaned
        return await super().awrap_model_call(request, handler)
import base64
from pathlib import Path
from langchain.messages import trim_messages, SystemMessage, HumanMessage
from src.core.config import settings
from src.core.model import create_model, switch_model, file_to_image, model_call_guard, get_engine_prompt_tokens
from src.core.prompts.plc_auditor import PLC_AUDITOR_SYSTEM_PROMPT
from src.core.tool_output_budget import ToolOutputBudgetMiddleware

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


# ─── deepagents 0.7 SummarizationMiddleware 挂载（user middleware 原位替换）───
# 背景（2026-11-19 动作三：deepagents 0.6.12 → 0.7.24 升级）：
#   0.6.12 时代靠 monkey-patch deepagents.graph.create_summarization_middleware
#   接管默认实例创建位——0.7 之前没有官方替换入口，user middleware 传法必然
#   双实例并存（GY35362 双写 brick），同名又撞 langchain factory duplicate 断言
#   （1.0.48 生产 500）。0.7 引入 _apply_custom_middleware（graph.py:211）：
#   按 .name 原位替换主链中间件并保持栈序 ⇒ patch 摘除，恢复 user middleware
#   传法（历史方案见 git c49806d）。
#
# ⚠ name 匹配语义（0.7 summarization.py:534，升级 0.8 前必看）：
#   _DeepAgentsSummarizationMiddleware.name 是 property，仅当
#   type(self) is _DeepAgentsSummarizationMiddleware 时才返回
#   "SummarizationMiddleware"，**子类回退 type(self).__name__**。
#   ⇒ _SweepThenSummarize 必须显式覆盖 name="SummarizationMiddleware"，
#   否则 _apply_custom_middleware 匹配不到、默认实例不被替换，双实例并存
#   （0.6 时代事故原样复发）。
#
# 为什么仍是组合中间件而不是裸 _SummarizationAccurate：
#   0.7 主链默认 Summarization 槽位在 core 段（graph.py:912，user tail 之前 =
#   wrap 链更外层），_apply_custom_middleware 的替换发生在原位 ⇒ 裸实例仍会
#   跑到 ToolOutputBudget **外面**，破坏 budget 模块头论证过的位置不变量
#   （"budget 必须先瘦身，Summarization/guard 才算得准"）。组合件把 budget 的
#   wrap_model_call（幂等 sweep）与 summarization 的 wrap_model_call 手动按原序
#   组合，等价于把这两个钩子按原相对位置放在默认槽位。
#   state_schema=SummarizationState：graph.py:962 的 private_state_field_names
#   只扫最终列表 middleware 的 state_schema 属性，组合件不带会把
#   SummarizationState 的私有字段（_summarization_event/_summarization_session_id）
#   从 private_state_keys 收集里藏掉。带上后与"裸实例直接在列表里"完全等价；
#   GeesunAgentState（state_schema= 参数）的 reducer 覆盖依旧后到后赢。
#
# gp subagent（graph.py:850 _gp_inheritable）：user middleware 里 name 命中
#   gp 原始栈的实例同样被 _apply_custom_middleware 原位替换 ⇒ gp 的摘要也
#   升级为精确计数 + 0.6 trigger + budget sweep（0.6 patch 时代同语义，知情
#   接受）。gp 与主链同会话同沙箱，inventory_provider 闭包语义成立；每次
#   create_deep_agent 新建实例，无共享态。


class _SweepThenSummarize(AgentMiddleware):
    """wrap_model_call 组合件：budget 瘦身 → summarization（保原链序）。

    只组合这两个钩子；其余钩子（budget 的 wrap_tool_call 等）仍由 user
    middleware 列表里的同一个 budget 实例提供。budget 实例共享 ⇒ 其
    wrap_model_call 会在组合件（外层）和 user middleware（内层）各跑一次，
    sweep 幂等（已 externalize 的 stub 再扫一遍是 no-op），无害。

    ⚠ name 必须 == "SummarizationMiddleware"（0.7 _apply_custom_middleware 按
    .name 字符串匹配做原位替换；_DeepAgentsSummarizationMiddleware 的 name
    property 对子类回退 type(self).__name__，见模块头注释）。忘写/写错 =
    默认实例不被替换 = 双实例并存。
    """

    # deepagents graph.py:962 private_state_field_names 扫此属性（见模块头）；
    # SummarizationState 的 _summarization_event/_summarization_session_id 注解
    # 随后被 GeesunAgentState 的 reducer 版覆盖（factory 聚合 base_state 排最后）。
    state_schema = SummarizationState

    # 0.7 _apply_custom_middleware 按 .name 匹配替换（graph.py:211/231）。
    name = "SummarizationMiddleware"

    def __init__(self, sweep, summarization):
        self._sweep = sweep
        self._sum = summarization

    def wrap_model_call(self, request, handler):
        return self._sweep.wrap_model_call(
            request,
            lambda req: self._sum.wrap_model_call(req, handler),
        )

    async def awrap_model_call(self, request, handler):
        return await self._sweep.awrap_model_call(
            request,
            lambda req: self._sum.awrap_model_call(req, handler),
        )


# 版本闸门（fail-fast，1.0.48 教训：错配必须 import 时炸，不能等运行时静默错行为）：
# 挂载依赖 0.7 的 _apply_custom_middleware（graph.py:211 按 .name 原位替换）与
# _DeepAgentsSummarizationMiddleware 的内部结构（_lc_helper/_partition_messages/
# _build_new_messages_with_path/_apply_event_to_messages，已在 0.7.24 逐一核对）。
# pyproject pin >=0.7,<0.8。升级 0.8 或降级 0.6 时必须重新核对本文件模块头注释
# 的挂载语义（0.6 的历史方案 monkey-patch 见 git c49806d）。
import importlib.metadata as _im

_deepagents_ver = _im.version("deepagents")
_major_minor = tuple(int(p) for p in _deepagents_ver.split(".")[:2])
if _major_minor != (0, 7):
    raise RuntimeError(
        f"deepagents {_deepagents_ver} 与本项目的 SummarizationMiddleware 挂载不兼容："
        "user middleware 原位替换依赖 0.7 的 _apply_custom_middleware（按 .name 匹配），"
        "_SweepThenSummarize/_SummarizationAccurate 依赖 0.7 wrapper 的内部结构"
        "（_lc_helper 等）。降级 0.6 需恢复 monkey-patch 方案（git c49806d），"
        "升级 0.8 需重新核对面（见 agent.py 模块头注释）。"
    )

logger.info(
    "[SUMMARY-MOUNT] deepagents %s：_SweepThenSummarize(name=SummarizationMiddleware) "
    "经 user middleware 传入，_apply_custom_middleware 按 name 原位替换主链/gp 默认实例",
    _deepagents_ver,
)

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
    # /home/、/tmp/：沙箱内路径——write_file 走 sandbox backend 的 e2b 上传通道直写沙箱（仅 UTF-8 文本），
    #   让 AI 直接 write_file 写脚本/中间文件，不必绕 execute heredoc（也更安全，无 shell 注入面）
    # /large_tool_results/（2026-10-10 补）：大工具结果转存的**唯一落点**。它此前
    #   不在白名单，而 routes 表里配了 StateBackend() —— 白名单与路由表脱节，导致
    #   deepagents 自带的 offload 在 4863afff 现场被本类拒写（14:32:03 日志
    #   `[VALIDATED_CB] 拒绝写入: path=/large_tool_results/chatcmpl-tool-...`），
    #   45.8MB 原文原地留在 state。补上后：ToolOutputBudgetMiddleware 的转存、
    #   以及库自带 offload（若触发）都写这里；路由指向真实磁盘（见 routes 表）。
    #
    # ⚠️ skill 写入位置（2026-10-23 变更）：原先白名单里是全局 `/skills/__agent__/`
    #   —— 所有用户的 agent 自创 skill 都堆进同一个目录：跨用户互相可见/可加载、
    #   同名互相覆盖、无法按人删除。现改为**每个用户各自一份**，故不能再把
    #   `__agent__` 留在字面量白名单里（字面量只能表达"所有人共用这个前缀"）。
    #   用户维度前缀由实例属性 `_user_skill_prefix` 表达，判定见 `_is_writable`。
    ALLOWED_WRITE_PREFIXES = {
        "/reports/", "/workspace/memories/", "/conversation_history/",
        "/home/", "/tmp/",
        "/large_tool_results/",
    }

    # 用户 skill 目录模板：实例用 `.format(user_id=...)` 展开成真实前缀。
    #
    # 为什么留一个**类级字面量**（而不是只在 __init__ 里拼字符串）：
    # tests/services/test_backend_route_whitelist.py 是纯 AST 契约测试，只认类属性里的
    # 字面量；路由表 ↔ 白名单的归类闭合性必须能被它看见，否则 `/skills/__user_*__/`
    # 会重新变成"有路由、无人认领"的第四态（正是 4863afff 事故的形态）。
    USER_SKILL_PREFIX_TEMPLATE = "/skills/__user_{user_id}__/"

    # 沙箱内仍禁写的路径（/home/ /tmp/ 已放行直写沙箱；这些是系统级/挂载路径，AI 不应碰）
    SANDBOX_PATH_PREFIXES = ("/root/", "/mnt/", "/code/", "/var/")
    # 虚拟文件系统内只读路径：命中即提示改 reports（/skills/ 需细分提示，见 _reject_hint）
    VIRTUAL_READONLY_PREFIXES = ("/uploads/", "/skills/", "/workspace/agent-memory/")

    # B2: glob 扫描保险丝超时（秒）——同步/异步 glob 超过该时长即中止返回错误，
    # 防止任何意外长扫描（含未拦截的宽泛模式）阻塞 asyncio 事件循环
    GLOB_TIMEOUT_SEC = 10

    # ─── C｜execute 输出体积闸门（2026-10-10）───
    # 事故：会话 4863afff 里模型跑的 OCR 脚本把结果（含 base64 图片）整坨 print 到
    # stdout，单次 execute 返回 45,777,272 字符（45.8MB）。deepagents 的 offload
    # 安全网（/large_tool_results/，阈值 NUM_CHARS_PER_TOKEN×20000 = 80000 字符）
    # 本该把它搬出 messages，却被本类 ALLOWED_WRITE_PREFIXES 拒绝写入 →
    # 45.8MB 原地留在 LangGraph state → 下一轮模型调用必然撑爆窗口
    # （同时 28MB span 撞 OTLP/alloy 4MB 上限）。闸门设在**源头**：任何单次
    # execute 的结果都不许无界增长。
    #
    # ⚠ 为什么闸门必须放在本类，而不是 langchain-cubesandbox 的 `execute()`：
    # deepagents 的 read/ls/grep/glob/write/edit **全部复用** `self.execute()`
    # （backends/sandbox.py:629/668/698/813/857/956/987），并按结构化格式解析其
    # 输出（_parse_read_output / _parse_ls_output / _parse_grep_output /
    # _parse_glob_output / _check_preflight_result）。在底层 execute 上截断会把
    # 提示文本追加进 JSON 尾部 → 解析失败 → read_file / grep / ls / write 全线
    # 报错。而本类是**唯一**的工具层执行入口（见下方 A 段注释：CompositeBackend
    # .execute 原文 "execution is not path-routable — it always delegates to the
    # default backend"），内部文件系统管道不经此处 ⇒ 截断只作用于"模型自己发的
    # shell 命令"。
    #
    # 取值依据：deepagents 自身 read() 的上限是 MAX_OUTPUT_BYTES = 500KiB
    # （backends/sandbox.py:108），其 offload 阈值为 80000 字符；参考 deer-flow
    # fail-closed 兜底思路取 200000 字符，**不采用**其沙箱侧 10MiB——中文 token
    # 更贵、我们的上下文预算更小。<= 0 表示不限制（仅排障用）。
    MAX_EXECUTE_OUTPUT_CHARS = 200_000
    # 截断保留比例：尾部 50%。尾部不能少——脚本的最终结论与 traceback 都在尾部
    # （本例 45.8MB 的 tail 恰是 `Result type: <class 'list'>`），且 stderr 被拼在
    # output 末尾（langchain-cubesandbox/langchain_cubesandbox/sandbox.py:471），
    # 天然落在尾部保留区。
    EXECUTE_OUTPUT_TAIL_RATIO = 0.5

    # ─── A｜execute 通道护栏（2026-09-29）───
    # `/reports`、`/uploads` 是**虚拟文件系统路径**：write_file 命中路由直写宿主卷，
    # 而 execute（shell）不参与路径路由、落到沙箱，且 create_sandbox **未挂载任何
    # 宿主目录** ⇒ 命令里出现这两个路径一律是"通道用错"，不是"目录没建好"。
    # 完整事故链与设计取舍见 src/core/execution_guard.py 模块头。
    #
    # 只匹配**路径字面量**（后跟 `/`、空白、引号、命令分隔符或行尾），
    # 不误伤 `/reportsX` 这类同前缀目录名。
    # ⚠️ 滑网面（诚实记录，别当完备保证）：变量拼接、base64 解码后执行、通配符
    #    `/re*ts`、`cd / && cd reports`、进程内 open() 写别的路径 —— A 只覆盖
    #    "字面量出现"这一高频形态；**不丢产物由 B（收尾差集回收）保证**。
    # 🔭 治本方案 C（未做，理由与触发条件见 execution_guard.py 模块头）：
    #    把宿主 report_root/{uid}/{sid}（可写）+ upload_root/{uid}/{sid}（只读）
    #    bind-mount 进沙箱 ⇒ 通道不对称消失，A/B 退化为冗余保险。
    EXECUTE_VIRTUAL_PATH_RE = re.compile(
        r"/(?:reports|uploads)(?=[/\s'\"`;|&)]|$)", re.MULTILINE
    )

    def __init__(self, default, routes, *, user_id: str = "", session_id: str = ""):
        super().__init__(default=default, routes=routes)
        self._report_prefix = f"/reports/{user_id}/{session_id}/" if user_id and session_id else "/reports/<user_id>/<session_id>/"
        # A/B 共用的会话身份：A 的举报（execution_guard.note_*）与日志都需要它。
        # 注意不能从 default backend 反推——本地兜底路径下 default 是 LocalShellBackend。
        self._user_id = user_id
        self._session_id = session_id
        # 当前用户可写的 skill 根（agent 自创 skill 的默认落点，2026-10-23 起）。
        # ⚠️ user_id 为空时必须留空串，且 `_is_writable` 要显式判空：
        #    `"x".startswith("")` 恒为 True —— 不判空等于把整台机器的路径全放行。
        self._user_skill_prefix = (
            self.USER_SKILL_PREFIX_TEMPLATE.format(user_id=user_id) if user_id else ""
        )
        # 记忆存储路由（StoreBackend）：不参与「无 path 的全路由枚举」，理由见
        # `_glob_target` 与 `ls`/`als` 覆写处的注释（2026-11-18 glob/ls 崩链）。
        self._store_route_prefixes = {
            prefix for prefix, backend in routes.items()
            if isinstance(backend, StoreBackend)
        }

    # ─── 写权限判定（单一入口）───

    def _is_writable(self, file_path: str) -> bool:
        """统一的"这条路径能不能写"判据。

        单一入口的原因：`write` / `awrite` / `edit` / `aedit` 四条通道各自都要判，
        任何一处漏掉都会留下旁路（`edit_file` 曾因此完全绕过白名单，可改
        `/skills/__system__/` 系统预装 skill）。
        """
        if self._user_skill_prefix and file_path.startswith(self._user_skill_prefix):
            return True
        return any(file_path.startswith(p) for p in self.ALLOWED_WRITE_PREFIXES)

    def _skill_name_of(self, file_path: str) -> str | None:
        """若路径落在当前用户的 skill 目录内，返回其 skill 名（第一段）；否则 None。"""
        if not self._user_skill_prefix or not file_path.startswith(self._user_skill_prefix):
            return None
        rest = file_path[len(self._user_skill_prefix):]
        skill_name = rest.split("/", 1)[0]
        return skill_name or None

    def _validate_skill_md_write(self, file_path: str, content: str) -> str | None:
        """写 SKILL.md 时校验 frontmatter；返回错误提示（None = 通过）。

        为什么必须在**写入通道**校验：deepagents 扫描端遇到坏 frontmatter 只做
        静默 skip（`_list_skills_with_errors`），模型会看到 write_file 成功、
        用户以为建好了，实际这个 skill 永远不会被加载 —— 与 2026-08-19 上传通道
        那个 bug 同一形态，只是换了个入口。

        校验挂在 `write` 通道：0.6 的 write 拒绝已存在文件（更新只能走 edit_file）；
        0.7 起 write 为 create-or-overwrite，SKILL.md 的**整体重写也会经过本校验**，
        覆盖面反而更大。`edit_file` 通道的旧/新片段无法拼出完整 frontmatter，
        不校验（已在 AGENTS.md 中要求编辑时保持 frontmatter 完整）。
        """
        if not file_path.endswith("/SKILL.md"):
            return None
        skill_name = self._skill_name_of(file_path)
        if not skill_name:
            return None

        validation = validate_skill_md(content)
        if not validation["valid"]:
            return (
                f"SKILL.md frontmatter 校验失败（{validation['error']}）。"
                f"必须写成：'---\\nname: {skill_name}\\ndescription: <一句话说明用途与触发场景>\\n---'，"
                f"且 name 必须与目录名 '{skill_name}' 完全一致，否则该 skill 不会被加载。"
                f"请修正后重新 write_file 到 '{file_path}'。"
            )
        if validation["name"] != skill_name:
            return (
                f"SKILL.md 的 name 字段是 '{validation['name']}'，与目录名 '{skill_name}' 不一致。"
                f"deepagents 以目录名索引 skill，两者必须完全相同；"
                f"请把 name 改成 '{skill_name}'，或把文件写到 "
                f"'{self._user_skill_prefix}{validation['name']}/SKILL.md'。"
            )
        return None

    # ─── D｜目录列举收敛：根目录给清单、记忆存储不参与全路由枚举（2026-11-18）───
    #
    # 事故（会话 GY35362:c6a0eebd，08:36）：
    #   模型先 `ls("/")`（得到 `path_not_found`，白跑两步），再
    #   `glob('/uploads/GY35362/c6a0eebd/*')` 且**没传 path** —— CompositeBackend
    #   对 path=None 的 glob 会遍历**全部路由**，其中包括 /workspace/memories、
    #   /workspace/agent-memory 两个 StoreBackend。deepagents 0.6.12 的
    #   StoreBackend 只有同步 ls/glob（内部 `store.search`），而我们原来的 store
    #   包装器只有异步接口 ⇒ `... object has no attribute 'search'`：两次 glob 被
    #   工具层兜成失败，紧接一次 ls 未捕获，直接把 SSE 流打断成「任务执行失败」。
    #
    # 三层处理：
    #   ① 根因在本类之外：`ReconnectingAsyncPostgresStore` 已补齐同步契约；
    #   ② 本类让记忆存储**不参与无 path 的全路由枚举** —— 即便①修好，遍历
    #      记忆命名空间也会把全部条目的每条消息分页拉进内存再本地匹配，
    #      纯属浪费（agent 对记忆只有「按已知路径 read_file」这一种真实需求）；
    #   ③ `ls` 加异常边界（工具层可失败，但不许把异常抛进 agent 流），且根目录
    #      直接返回路由目录清单，不再被 default（sandbox）的 path_not_found 短路。

    def _glob_target(self):
        """无 path 的 glob 该作用在哪个 backend 上（剔除记忆存储路由）。

        有 path 且非 "/" 时 CompositeBackend 只扫单个路由，原样返回 self；
        否则返回一个路由表过滤后的 CompositeBackend 副本 —— glob/aglob 只用到
        `default` + `routes`，构造副本比改自身状态安全（同一 backend 会被多个
        并发工具调用共享）。
        """
        if not self._store_route_prefixes:
            return self
        return CompositeBackend(
            default=self.default,
            routes={
                prefix: backend
                for prefix, backend in self.routes.items()
                if prefix not in self._store_route_prefixes
            },
        )

    def _root_dir_entries(self) -> list[dict]:
        """虚拟文件系统根目录的一级目录清单（去重、保留 `/xxx/` 形态）。

        取 routes 表而不是真去问各 backend：路由表就是"这台虚拟 FS 长什么样"的
        定义；问 default（沙箱）只会得到 path_not_found（sandbox 没有虚拟根）。
        """
        dirs: dict[str, None] = {}
        for prefix in self.routes:
            head = prefix.strip("/").split("/", 1)[0]
            if head:
                dirs[f"/{head}/"] = None
        return [
            {"path": path, "is_dir": True, "size": 0, "modified_at": ""}
            for path in dirs
        ]

    def ls(self, path: str) -> LsResult:
        """根目录走清单；其余原样转发，异常收敛成 error 结果。"""
        if path in ("/", ""):
            return LsResult(entries=self._root_dir_entries())
        try:
            return super().ls(path)
        except Exception as e:  # noqa: BLE001  # 工具边界：失败要能被模型看见
            logger.warning("[VALIDATED_CB] ls 异常: path=%s err=%s", path, e)
            return LsResult(error=f"ls 失败: {e}")

    async def als(self, path: str) -> LsResult:
        """异步版 `ls`：同语义，改用 `super().als`。"""
        if path in ("/", ""):
            return LsResult(entries=self._root_dir_entries())
        try:
            return await super().als(path)
        except Exception as e:  # noqa: BLE001
            logger.warning("[VALIDATED_CB] als 异常: path=%s err=%s", path, e)
            return LsResult(error=f"ls 失败: {e}")

    def _reject_glob_scan(self, pattern: str, path: str | None) -> str | None:
        """B1: 拦截 '**' 全盘扫描（2026-08-18 根因修复）。

        CompositeBackend.glob 在 path 为 None 或 "/" 时会遍历 default（sandbox）
        + 全部路由 backend 做同步 rglob；配合 '**' 通配符 = 对虚拟文件系统全量递归，
        单条工具调用即可把 asyncio 事件循环冻死（实测 2026-08-18：agent 调
        glob '**/tech-spec-pdf-diff/**' 后整个后端 6 分钟无响应、CLOSE_WAIT 堆积）。

        拦截条件：
        - pattern 含 '**'（平台规则本就禁止该通配符）
        - 且 path 未限定到具体路由（None 或 "/" 会触发全路由遍历）
        命中即返回拒绝提示（含修正指引），不执行实际扫描。
        """
        if "**" not in pattern:
            return None
        if path and path != "/":
            # 已限定到具体路径/路由 → CompositeBackend 只扫单 backend，范围可控，放行
            return None
        return (
            f"glob 模式 '{pattern}' 含 '**' 全盘通配符且未限定搜索路径，平台禁止对虚拟文件系统"
            f"做 '**' 递归扫描（会扫描 sandbox + 全部路由，严重阻塞服务）。"
            f"请改为限定路径：glob(pattern='{pattern}', path='/skills/__system__/') "
            f"或直接用 ls / read_file 在具体目录（/uploads/、/reports/）下精确操作。"
        )

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """B1 拦截全盘 '**' 扫描；B2 同步扫描放线程池并限时，防止冻结事件循环。"""
        hint = self._reject_glob_scan(pattern, path)
        if hint:
            logger.warning("[VALIDATED_CB] 拒绝全盘 glob: pattern=%s path=%s", pattern, path)
            return GlobResult(error=f"拒绝执行: {hint}", matches=[])

        # B2 保险丝：同步 glob（CompositeBackend 内部 rglob/sandbox execute 均同步）
        # 放 ThreadPoolExecutor 执行并限时；超时不等待线程（shutdown(wait=False)），
        # 事件循环最多阻塞 GLOB_TIMEOUT_SEC，不会永久冻结。
        target = self if (path and path != "/") else self._glob_target()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(CompositeBackend.glob, target, pattern, path)
        try:
            return future.result(timeout=self.GLOB_TIMEOUT_SEC)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "[VALIDATED_CB] glob 超时(>%ss)已中止: pattern=%s path=%s",
                self.GLOB_TIMEOUT_SEC, pattern, path,
            )
            return GlobResult(
                error=f"glob 扫描超时（>{self.GLOB_TIMEOUT_SEC}s），已中止。请缩小搜索范围："
                f"限定 path 到具体目录，或避免宽泛模式。",
                matches=[],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[VALIDATED_CB] glob 异常: pattern=%s err=%s", pattern, e)
            return GlobResult(error=f"glob 失败: {e}", matches=[])
        finally:
            # 关键：不等待未完成线程（线程自身最终会被解释器清理），
            # 否则 TimeoutError 后 with/join 会再次阻塞
            pool.shutdown(wait=False)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        """B1 拦截全盘 '**' 扫描；B2 异步版用 asyncio.wait_for 限时。"""
        hint = self._reject_glob_scan(pattern, path)
        if hint:
            logger.warning("[VALIDATED_CB] 拒绝全盘 glob: pattern=%s path=%s", pattern, path)
            return GlobResult(error=f"拒绝执行: {hint}", matches=[])

        try:
            target = self if (path and path != "/") else self._glob_target()
            return await asyncio.wait_for(
                CompositeBackend.aglob(target, pattern, path),
                timeout=self.GLOB_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[VALIDATED_CB] aglob 超时(>%ss)已中止: pattern=%s path=%s",
                self.GLOB_TIMEOUT_SEC, pattern, path,
            )
            return GlobResult(
                error=f"glob 扫描超时（>{self.GLOB_TIMEOUT_SEC}s），已中止。请缩小搜索范围："
                f"限定 path 到具体目录，或避免宽泛模式。",
                matches=[],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[VALIDATED_CB] aglob 异常: pattern=%s err=%s", pattern, e)
            return GlobResult(error=f"glob 失败: {e}", matches=[])

    def _reject_grep_scan(self, path: str | None) -> str | None:
        """B1: 拦截 grep 无路径限定的全路由遍历（与 glob 同构风险）。

        CompositeBackend.grep 在 path 为 None 或 "/" 时同样会遍历 default（sandbox）
        + 全部路由 backend 做同步全量扫描（FilesystemBackend.grep 内部 rglob("*")）。
        grep 没有 '**' 通配符，但"无限定路径"与 glob '**' 是同一个危险面，
        故对 path=None/"/" 的 grep 也直接拒绝，要求限定到具体目录。
        """
        if path and path != "/":
            return None
        return (
            "grep 未限定搜索路径，平台禁止对虚拟文件系统做全盘全文搜索"
            f"（会扫描 sandbox + 全部路由，严重阻塞服务）。"
            f"请改为限定路径：grep(pattern='{path or ''}', path='/skills/__system__/') "
            f"或指定具体目录（/uploads/、/reports/、/workspace/memories/ 等）。"
        )

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """B1 拦截无路径限定的全路由 grep；B2 同步扫描放线程池并限时。"""
        hint = self._reject_grep_scan(path)
        if hint:
            logger.warning("[VALIDATED_CB] 拒绝全盘 grep: path=%s", path)
            return GrepResult(error=f"拒绝执行: {hint}")

        # B2 保险丝：同步 grep 放线程池执行并限时，防止冻结事件循环
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(super().grep, pattern, path, glob)
        try:
            return future.result(timeout=self.GLOB_TIMEOUT_SEC)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "[VALIDATED_CB] grep 超时(>%ss)已中止: pattern=%s path=%s",
                self.GLOB_TIMEOUT_SEC, pattern, path,
            )
            return GrepResult(
                error=f"grep 搜索超时（>{self.GLOB_TIMEOUT_SEC}s），已中止。"
                f"请缩小搜索范围：限定 path 到具体目录。"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[VALIDATED_CB] grep 异常: pattern=%s err=%s", pattern, e)
            return GrepResult(error=f"grep 失败: {e}")
        finally:
            pool.shutdown(wait=False)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """B1 拦截无路径限定的全路由 grep；B2 异步版用 asyncio.wait_for 限时。"""
        hint = self._reject_grep_scan(path)
        if hint:
            logger.warning("[VALIDATED_CB] 拒绝全盘 grep: path=%s", path)
            return GrepResult(error=f"拒绝执行: {hint}")

        try:
            return await asyncio.wait_for(
                super().agrep(pattern, path, glob), timeout=self.GLOB_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[VALIDATED_CB] agrep 超时(>%ss)已中止: pattern=%s path=%s",
                self.GLOB_TIMEOUT_SEC, pattern, path,
            )
            return GrepResult(
                error=f"grep 搜索超时（>{self.GLOB_TIMEOUT_SEC}s），已中止。"
                f"请缩小搜索范围：限定 path 到具体目录。"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[VALIDATED_CB] agrep 异常: pattern=%s err=%s", pattern, e)
            return GrepResult(error=f"grep 失败: {e}")

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
        # 别家用户的 skill 目录（自己的目录在白名单里，根本走不到这里）
        if file_path.startswith("/skills/__user_"):
            return (
                f"路径 '{file_path}' 不是当前用户的 skill 目录。"
                f"skill 只能写入自己的目录：'{self._user_skill_prefix}<skill_name>/SKILL.md'"
            )
        # 历史共享 skill 层：2026-10-23 起冻结只读
        if file_path.startswith("/skills/__agent__"):
            return (
                f"路径 '{file_path}' 是历史共享 skill 层（已冻结，只读），不可修改。"
                f"新建/更新 skill 请写入当前用户目录 "
                f"'{self._user_skill_prefix}<skill_name>/SKILL.md'"
            )
        # 系统预装 skill：只读
        if file_path.startswith("/skills/__system__"):
            return (
                f"路径 '{file_path}' 是系统预装 skill（只读），不可修改。"
                f"如需新技能，请写入当前用户目录 "
                f"'{self._user_skill_prefix}<skill_name>/SKILL.md'"
            )
        if file_path.startswith(self.VIRTUAL_READONLY_PREFIXES):
            return (
                f"路径 '{file_path}' 为只读（输入/技能/共享规则）。"
                f"交付物请写入 '{self._report_prefix}<文件名>'"
            )
        return (
            f"只能写入到以下路径: {', '.join(sorted(self.ALLOWED_WRITE_PREFIXES))}"
            f"，以及你自己的 skill 目录 '{self._user_skill_prefix}'。"
            f"当前会话交付目录为 '{self._report_prefix}'"
        )

    def _reject_directory_write(self, file_path: str) -> str | None:
        """拦截把 file_path 写成目录的 write_file 调用。

        2026-08-21 事故根因：模型调用 write_file 时 file_path 被传成目录
        （/reports/uid/sid/ 或 /reports/uid/sid，漏了文件名），后端照写/报错，
        前端 deriveGeneratedFiles 照单派生 → 空白文件名卡片 + HEAD 307/401 →
        "文件不可用"。这里只对 /reports/ 交付物做严格校验（交付物必须带文件名，
        前端按文件名渲染卡片/预览），命中即返回带正确示例的提示，逼模型重试。
        """
        if not file_path.startswith("/reports/"):
            return None
        # 尾斜杠 = 目录
        if file_path.endswith("/"):
            return (
                f"路径 '{file_path}' 是目录而非文件：write_file 的 file_path 缺少文件名。"
                f"请使用带文件名（含扩展名）的完整路径，例如 "
                f"'{self._report_prefix}技术协议差异对比报告.md' 或 "
                f"'{self._report_prefix}report.html'。"
            )
        # 路径就是会话交付目录本身（去掉尾斜杠后相等）
        if file_path == self._report_prefix.rstrip("/"):
            return (
                f"路径 '{file_path}' 是会话交付目录本身，write_file 需要写入具体文件。"
                f"请使用带文件名（含扩展名）的完整路径，例如 "
                f"'{self._report_prefix}report.md'。"
            )
        # 最后一段是会话 ID 风格（8~32 位 hex、无扩展名）→ 目录
        last = file_path.rsplit("/", 1)[-1]
        if last and re.fullmatch(r"[0-9a-fA-F]{8,32}", last):
            return (
                f"路径 '{file_path}' 疑似目录（最后一段 '{last}' 无文件名/扩展名）。"
                f"write_file 的 file_path 必须指向具体文件，例如 "
                f"'{self._report_prefix}report.json'。"
            )
        # 最后一段为空（防御）
        if not last:
            return (
                f"路径 '{file_path}' 未指定文件名，write_file 无法写入。"
                f"请使用带文件名（含扩展名）的完整路径，例如 '{self._report_prefix}report.md'。"
            )
        return None

    def write(self, file_path: str, content: str) -> WriteResult:
        if not self._is_writable(file_path):
            hint = self._reject_hint(file_path)
            logger.warning(
                "[VALIDATED_CB] 拒绝写入: path=%s (只允许 %s + %s) | hint=%s",
                file_path, self.ALLOWED_WRITE_PREFIXES, self._user_skill_prefix, hint,
            )
            return WriteResult(
                error=f"拒绝写入: {hint}",
                path=file_path,
            )
        dir_hint = self._reject_directory_write(file_path)
        if dir_hint:
            logger.warning(
                "[VALIDATED_CB] 拒绝目录写入: path=%s | hint=%s",
                file_path, dir_hint,
            )
            return WriteResult(
                error=f"拒绝写入: {dir_hint}",
                path=file_path,
            )
        skill_hint = self._validate_skill_md_write(file_path, content)
        if skill_hint:
            logger.warning(
                "[VALIDATED_CB] 拒绝写入 SKILL.md: path=%s | hint=%s",
                file_path, skill_hint,
            )
            return WriteResult(
                error=f"拒绝写入: {skill_hint}",
                path=file_path,
            )
        return super().write(file_path, content)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        if not self._is_writable(file_path):
            hint = self._reject_hint(file_path)
            logger.warning(
                "[VALIDATED_CB] 拒绝写入: path=%s (只允许 %s + %s) | hint=%s",
                file_path, self.ALLOWED_WRITE_PREFIXES, self._user_skill_prefix, hint,
            )
            return WriteResult(
                error=f"拒绝写入: {hint}",
                path=file_path,
            )
        dir_hint = self._reject_directory_write(file_path)
        if dir_hint:
            logger.warning(
                "[VALIDATED_CB] 拒绝目录写入: path=%s | hint=%s",
                file_path, dir_hint,
            )
            return WriteResult(
                error=f"拒绝写入: {dir_hint}",
                path=file_path,
            )
        skill_hint = self._validate_skill_md_write(file_path, content)
        if skill_hint:
            logger.warning(
                "[VALIDATED_CB] 拒绝写入 SKILL.md: path=%s | hint=%s",
                file_path, skill_hint,
            )
            return WriteResult(
                error=f"拒绝写入: {skill_hint}",
                path=file_path,
            )
        return await super().awrite(file_path, content)

    # ─── edit / aedit：此前完全绕过白名单（2026-10-23 修）───
    #
    # 事故形态：tool 层 `edit_file` 走 `middleware/filesystem.py → resolved_backend.aedit`，
    # `FilesystemBackend.edit/aedit` 自己有独立的读改写逻辑（不调 `write`），而本类只覆写了
    # write/awrite ⇒ 白名单、目录校验、二进制拦截对 edit 全部失效，模型可以直接改
    # `/skills/__system__/` 的系统预装 skill、改 `/uploads/` 下的用户原件。
    # 不修的话「`__agent__` 冻结只读」只是没写到的约束，不是事实。

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        if not self._is_writable(file_path):
            hint = self._reject_hint(file_path)
            logger.warning(
                "[VALIDATED_CB] 拒绝编辑: path=%s (只允许 %s + %s) | hint=%s",
                file_path, self.ALLOWED_WRITE_PREFIXES, self._user_skill_prefix, hint,
            )
            return EditResult(error=f"拒绝写入: {hint}", path=file_path, occurrences=0)
        return super().edit(
            file_path, old_string, new_string, replace_all=replace_all
        )

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        if not self._is_writable(file_path):
            hint = self._reject_hint(file_path)
            logger.warning(
                "[VALIDATED_CB] 拒绝编辑: path=%s (只允许 %s + %s) | hint=%s",
                file_path, self.ALLOWED_WRITE_PREFIXES, self._user_skill_prefix, hint,
            )
            return EditResult(error=f"拒绝写入: {hint}", path=file_path, occurrences=0)
        return await super().aedit(
            file_path, old_string, new_string, replace_all=replace_all
        )

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

    # ─── A｜execute / aexecute 通道护栏（2026-09-29）───
    # 拦截点为什么唯一且必经：deepagents 的 execute 工具只调
    # `resolved_backend.execute/aexecute`，而 `resolved_backend` 就是本类
    # （CompositeBackend.execute 原文："execution is not path-routable — it always
    # delegates to the default backend"）。详见 src/core/execution_guard.py。
    def _reject_execute_virtual_path(self, command: str) -> str | None:
        """命中"命令引用虚拟路径"→ 返回拒绝提示（含正确通道），否则 None。"""
        if not command or not self.EXECUTE_VIRTUAL_PATH_RE.search(command):
            return None
        return execution_guard.virtual_path_write_hint(command, self._report_prefix)

    def _note_execute_violation(self, command: str, *, via_async: bool) -> None:
        """B｜登记"本轮发生过越界写尝试"，供 API 层收尾时触发沙箱产物回收。

        只举报、不做任何沙箱 I/O：这里是工具调用热路径，同步版本做网络往返会占住
        事件循环（见 execution_guard.note_execute_path_violation docstring）。
        best-effort：举报失败绝不影响"拒绝"本身——拒绝是安全动作，举报只是救济线索。
        """
        try:
            execution_guard.note_execute_path_violation(
                self._user_id, self._session_id, command
            )
            logger.warning(
                "[VALIDATED_CB] 拒绝 execute 引用虚拟路径(%s): cmd=%s",
                "async" if via_async else "sync",
                command[:200],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[VALIDATED_CB] 登记 execute 越界信号失败: %s", e)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """A：拒绝引用 /reports、/uploads 的命令（虚拟路径在沙箱内不存在）。

        拒绝姿势选 `ValueError` 而非 `ExecuteResponse(exit_code=1)`：
        `ExecuteResponse` 无 error 字段，且工具层**恒定**回 `status="success"`
        （只把 exit_code 拼进正文），会被当作成功。`ValueError` 是工具层显式
        捕获并转 `ToolMessage(status="error")` 的预留通道。

        ⚠️ 签名必须原样保留关键字 `timeout`：工具层用
        `execute_accepts_timeout(type(executable))` 判定，而 `executable` 就是本类
        （`middleware/filesystem.py` sync/async 两处）。签名丢了 timeout，模型带
        timeout 的调用会被工具层直接拒掉，护栏立刻变成功能回归。
        """
        hint = self._reject_execute_virtual_path(command)
        if hint is not None:
            self._note_execute_violation(command, via_async=False)
            raise ValueError(hint)
        return self._clamp_execute_output(super().execute(command, timeout=timeout))

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """A 的异步版（模型实际走这条：工具层优先用 coroutine 分支）。"""
        hint = self._reject_execute_virtual_path(command)
        if hint is not None:
            self._note_execute_violation(command, via_async=True)
            raise ValueError(hint)
        return self._clamp_execute_output(
            await super().aexecute(command, timeout=timeout)
        )

    @staticmethod
    def _clamp_execute_output(
        resp: ExecuteResponse,
        *,
        limit: int | None = None,
    ) -> ExecuteResponse:
        """C：单次 execute 输出超限则头尾保留 + 明确通知，并置 `truncated=True`。

        设计取舍（三条，都是刻意的）：
        1. **不改 exit_code**：命令成功/失败的事实与输出体积无关，动它会让模型
           误判（以为命令本身失败了）。
        2. **不抛异常**：这是兜底闸门，自身出错时宁可放行原文（上游还有
           middleware 兜底），绝不能把一次成功的命令变成 error。
        3. **截断必须自报**：通知写在正文里，模型一定能看到自己被截断了；
           本例的事故正是"没人告诉任何一方输出有多离谱"。

        诚实说明：e2b 的 `CommandResult` **无 truncated 字段**
        （`e2b/sandbox/commands/command_handle.py:37` 只有 stderr/stdout/exit_code/
        error），所以 langchain-cubesandbox 里的
        `getattr(result, "truncated", False)` 恒为 False —— 这个标志此前从未真正
        生效过，由本闸门首次赋予真实语义（工具层会据此追加
        "[Output was truncated due to size limits]"）。
        """
        limit = (
            ValidatedCompositeBackend.MAX_EXECUTE_OUTPUT_CHARS
            if limit is None
            else limit
        )
        output = resp.output or ""
        if limit <= 0 or len(output) <= limit:
            return resp
        total = len(output)
        notice = (
            f"\n\n[输出超限已截断：原始输出 {total} 字符，超过单次上限 {limit} 字符。"
            f"已保留开头与结尾，中间部分被丢弃。"
            f"需要完整内容时，请把命令输出重定向到文件后分段读取，例如："
            f"`<你的命令> > /tmp/out.txt 2>&1`，再用 "
            f"read_file(file_path='/tmp/out.txt', offset=0, limit=200) 逐段取回。]\n"
        )
        if len(notice) >= limit:
            # 上限比通知还小（极端配置）：牺牲正文，优先保住
            # "模型一定知道自己看到的不完整"这条不变量。
            notice = f"\n\n[输出超限已截断：原始输出 {total} 字符，超过上限 {limit} 字符。]"
        budget = max(limit - len(notice), 0)
        tail_keep = int(budget * ValidatedCompositeBackend.EXECUTE_OUTPUT_TAIL_RATIO)
        head_keep = budget - tail_keep
        clamped = output[:head_keep] + notice + (output[total - tail_keep:] if tail_keep else "")
        return ExecuteResponse(
            output=clamped,
            exit_code=resp.exit_code,
            truncated=True,
        )


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
        # 大工具结果转存 → **真实磁盘**（2026-10-10 改：原为 StateBackend()）
        #
        # 为什么必须落盘而不是留在 state：
        #   原路由 StateBackend() 只是把内容从 messages channel 挪到 state 的 files
        #   channel —— 不再进模型上下文（prompt 只拼 messages），但**内容仍在 state 里**，
        #   每次 checkpoint 都要全量序列化落 Postgres。4863afff 那条 45.8MB 的
        #   checkpoint_writes 就是这么来的。
        # 为什么落在 agent_workspace 下（而非新增独立根目录）：
        #   ① 该目录在生产 compose 里已挂载（`${AGENT_DATA_ROOT}/agent:/data/agent`），
        #      落盘即持久化，**不必新增 .env 键 / 改 compose**（少一处双源审计面）；
        #   ② 它**不在** `_build_inventory_provider` 的扫描范围（只扫 upload_root /
        #      report_root）⇒ 转存文件不会污染"当前会话真实资源清单"。
        # 虚拟路径仍是 /large_tool_results/<name>：该前缀已写进 deepagents 的系统提示词
        # （middleware/filesystem.py:548），模型据此用 read_file / grep 取回。
        "/large_tool_results/": FilesystemBackend(
            root_dir=f"{settings.agent_workspace}/tool_results/{user_id}/{session_id}/",
            virtual_mode=True,
        ),
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
    model = await create_model()
    # 给模型挂会话 ID：model_call_guard 据此把引擎真实 prompt_tokens 写回正确的会话缓存
    model._session_id = session_id

    # ─── SummarizationMiddleware（历史 offload）───
    # 触发用 fraction（0.8 × model.profile["max_input_tokens"]）——vLLM 自定义模型名
    # 不在 langchain 注册表 → profile 默认 None → 原 fraction 触发失效，故 create_model /
    # switch_model 已注入 profile={"max_input_tokens": resolve_max_len(...)}（见 model.py）。
    # 计数用 _engine_grounded_counter：优先读引擎真实 prompt_tokens（model_call_guard 每轮
    # 从 usage_metadata 写入的每会话缓存，含视觉 token，100% 准），仅 cold 首轮无缓存时退化
    # _safe_token_counter（本地 tiktoken 估算，会被中文 + 图片低估，但 400 兜底会纠正）。
    # 注：langchain-openai 对自定义模型名（Qwen3.6-35B-A3B）未实现
    # get_num_tokens_from_messages（2026-08-31 实测抛 NotImplementedError 直接杀流），故裸方法
    # 不可用；_safe_token_counter 改逐条 get_num_tokens（tiktoken cl100k，实测可用）+ 字符兜底。
    def _safe_token_counter(messages, *, tools=None) -> int:
        """安全 token 计数：get_num_tokens_from_messages 对自定义模型名未实现，
        逐条 get_num_tokens + 字符估算兜底，杜绝 NotImplementedError 杀流。"""
        total = 0
        for m in messages:
            content = str(getattr(m, "content", "")) if getattr(m, "content", None) else ""
            if not content:
                continue
            try:
                total += model.get_num_tokens(content)
            except Exception:
                total += len(content)  # 兜底：1 字符≈1 token（足够保守）
        return total

    def _engine_grounded_counter(messages, *, tools=None) -> int:
        """Summarization 触发用计数器：优先引擎真实 prompt_tokens（每会话缓存），
        cold 首轮无缓存才退化本地估算。闭包捕获 session_id 读缓存。"""
        real = get_engine_prompt_tokens(session_id)
        if real is not None:
            return real
        return _safe_token_counter(messages, tools=tools)

    def _build_inventory_provider(user_id, session_id, sandbox):
        """构造"当前会话真实资源清单"回调，随摘要注入 SystemMessage。

        数据源：settings.upload_root/report_root 下的真实文件 + 沙箱状态。
        目的：会话恢复（summarization）后 agent 直接照清单干活，
        不再 read_file 猜测 /uploads/ 下不存在的沙箱脚本/skill 文件
        （2026-08-17 实测：恢复后 read_file extract_pdf.py 等 5 连败触发 M3）。
        """
        def provider() -> str:
            lines = ["【当前会话真实资源清单】（系统注入，直接使用，勿猜测路径）"]
            up = Path(settings.upload_root) / user_id / session_id
            try:
                files = sorted(p.name for p in up.iterdir()) if up.exists() else []
            except Exception:
                files = []
            lines.append(f"- 输入文件 /uploads/{user_id}/{session_id}/: {files or '（空）'}")
            rp = Path(settings.report_root) / user_id / session_id
            try:
                reports = sorted(p.name for p in rp.iterdir()) if rp.exists() else []
            except Exception:
                reports = []
            lines.append(f"- 报告 /reports/{user_id}/{session_id}/: {reports or '（空）'}")
            sid = getattr(sandbox, "sandbox_id", "") if sandbox else ""
            lines.append(f"- 沙箱: {'活跃 ' + sid if sid else '无（execute 不可用）'}")
            lines.append("- 沙箱内文件（/home/user/）用 MCP 工具 copy/upload/download，不要 read_file 虚拟路径")
            return "\n".join(lines)
        return provider

    # 触发用 fraction（见下），不再需要 tokens 阈值推导；summarization_trigger_tokens
    # 仅作为无 profile 时的下限保护（config 值）。
    #
    # F2（2026-11-17）：trigger 0.8 → 0.6。f508e75c 复盘（PPT 全量迭代工作负载）：
    # 该会话单轮增量 ~60k token（pptx XML dump / 图片 / 大 md 读取），0.8 阈值下
    # headroom 仅 0.2×262144≈52k，一轮巨型 tool result 即可跨过 → Summarization
    # 全程未触发，15:09 cache=205380（距 trigger 仅差 4335），15:18 直接 262145 溢出
    # 400。0.6 下 headroom≈105k，正常 PPT 负载（+60k/轮）也能在触发点前被拦住。
    # 代价：压缩频率升高（约每 2-3 轮一次 vs 原来 4-5 轮），换恢复能力，值得。
    #
    # keep 用 fraction 而非条数（2026-09-12 改）：
    #   ① 框架默认就是这个形态（deepagents summarization.py:282 `("fraction", 0.10)`，
    #      仅在模型无 profile 的退化分支才用 `("messages", 6)`）。本项目有 profile，
    #      原先写 `("messages", 10)` 是 2026-08-12 随 trigger=("tokens", 200000) 一起引入的
    #      （a885849）；8-28 trigger 改成 fraction 时 keep 未同步审视。
    #   ② 条数没有硬上界：窗口 262144 下，生产单条最大 278 KB ≈ 8 万 token →
    #      "保留 10 条"在该会话上等于保留约 80 万 token，压缩后仍然超窗口。
    #      fraction 0.10 = 26214 token 是硬上界（按平均 2.4k token/条 ≈ 11 条，
    #      与原值均值几乎一致，等于白拿上界）。参照：deepagents 默认 0.10、
    #      deepseek-harness compaction-basic 默认 retainRatio 0.16（面向单条小的
    #      coding session，本项目单条偏大，故取 0.10 而非 0.16）。
    #   ③ ⚠ 必须同时传 partial_token_counter，否则二分退化（详见
    #      _install_partial_token_counter 与 tests/spikes/summarization_keep_semantics.py）。
    # 动作三（2026-11-19，deepagents 0.7）：实例直接构造、经 user middleware 传入，
    # _apply_custom_middleware 按 name="SummarizationMiddleware" 原位替换主链/gp
    # 默认实例（0.6.12 的 monkey-patch 方案已摘除，见模块级注释块）。
    summarization_mw = _SummarizationAccurate(
        model=model,
        backend=backend,
        trigger=("fraction", 0.6),  # 按 model.profile["max_input_tokens"] 的 0.6 触发（F2，2026-11-17 由 0.8 下调）
        keep=("fraction", 0.10),    # 最近 10% 窗口原样保留（有硬上界，见上）
        token_counter=_engine_grounded_counter,  # trigger：引擎真实 prompt_tokens 优先，cold 才退化估算
        partial_token_counter=_safe_token_counter,  # keep 二分：必须子集感知，不可沿用引擎值
        inventory_provider=_build_inventory_provider(user_id, session_id, sandbox),
    )

    # 工具结果体积闸门实例提前构造：同一实例进 user middleware（wrap_tool_call
    # 源侧瘦身，位置论证见下）+ 传给组合件 _SweepThenSummarize（wrap_model_call
    # 模型侧瘦身，替换默认实例槽位后先于 Summarization 执行，保持"先瘦身再算数"
    # 不变量）。组合件被 0.7 _apply_custom_middleware 按 name 替换进 core 栈。
    budget_mw = ToolOutputBudgetMiddleware(
        backend=backend,
        enabled=settings.tool_output_budget_enabled,
        externalize_min_chars=settings.tool_output_externalize_min_chars,
        fallback_max_chars=settings.tool_output_fallback_max_chars,
    )

    summary_composite = _SweepThenSummarize(budget_mw, summarization_mw)

    return _build_deep_agent(
        model=model,
        tools=tools,
        backend=backend,
        skills=skills,
        middleware=[
            # ★ 每轮重扫 skill 源（绕过 checkpoint 固化），排在链首：
            # 其 abefore_agent 先刷新 state["skills_metadata"]，随后默认
            # SkillsMiddleware.modify_request 注入的就是最新 skill 清单
            # （2026-08-19 实证：默认固化导致新上传 skill 永不进上下文）
            _FreshSkillsMiddleware(backend=backend, sources=skills),
            switch_model,
            file_to_image,
            # ★ 工具结果体积闸门（2026-10-10 新增）。位置是**论证过的**，不是随手插的：
            #   ① wrap_tool_call（源侧瘦身）必须排在这里：相对 deepagents 的
            #      FilesystemMiddleware 我们必然在内层（用户中间件被插在
            #      base stack 之后）—— 内层正是需要的：只有在那里改写 result，
            #      改写才能随返回上行并写进 state。详见 src/core/tool_output_budget.py。
            #   ② wrap_model_call（模型侧兜底 sweep）由同一实例经组合件
            #      _SweepThenSummarize 提供（见下）：0.7 _apply_custom_middleware
            #      按 name 把组合件替换进默认 Summarization 槽位（core 栈，wrap 链
            #      更外层），先于 Summarization 计数执行，保持"先瘦身再算数"
            #      不变量；本位置的 wrap_model_call 再跑一遍是幂等 no-op，留作纵深防御。
            #   ③ 必须在 file_to_image **之后**：后者把图片 file block 转
            #      image_url（Qwen 视觉），先转再瘦身，避免把待转的 base64
            #      文本当超限结果处置。
            budget_mw,
            # ★ 动作三（2026-11-19，deepagents 0.7）：Summarization 恢复经 user
            #   middleware 传入。组合件 name="SummarizationMiddleware" 会被
            #   _apply_custom_middleware 按 name 原位替换进主链/gp 默认 Summarization
            #   槽位（单实例），列表内位置不影响最终栈序（替换发生在 core 槽位原位）。
            #   组合序（budget sweep 外层 → summarization 内层）见 _SweepThenSummarize。
            summary_composite,
            # ★ 按总字符估 prompt_tokens 并动态收紧 max_tokens：budget 已在
            #   其前（组合件外层 + 本列表）瘦身，guard 才算得准。
            model_call_guard,
            # ★ 链尾守卫之一：防"工具全成功但整体不收敛"烧满 recursion_limit。
            # 两段式：≥warn_threshold 次重复 → 软提醒（wrap_model_call 注入 human msg）；
            # ≥hard_limit 次 → 剥离 tool_calls 逼模型出纯文本（不抛异常，SSE 不中断）。
            # 阈值从 settings 读（config.py loop_detect_*）。2026-09-08 移植 deer-flow。
            LoopDetectionMiddleware(
                warn_threshold=settings.loop_detect_warn_threshold,
                hard_limit=settings.loop_detect_hard_limit,
                tool_freq_warn=settings.loop_detect_tool_freq_warn,
                tool_freq_hard_limit=settings.loop_detect_tool_freq_hard,
                # ★ 2026-10-17 补：绝对次数阈值会被第二种工具结构性稀释
                #   （83345f0c：execute 累计 24 次但 20 窗口内最多 16 < hard=20，
                #    硬停在结构上不可能触发），故加占比 + 交替循环两个形态判据，
                #   且只在"窗口内零交付物"时生效（防误杀跑 build/批量处理类长任务）。
                tool_freq_ratio=settings.loop_detect_tool_ratio_threshold,
                min_window_fill=settings.loop_detect_min_window_fill,
                alternating_min_run=settings.loop_detect_alternating_min_run,
                # ★ 2026-10-20 补：以上判据都只看「调用形态」，而 submit-and-poll
                #   型工具（ocr_pdf_to_text）的正常形态**就是**同参反复续查
                #   （262 页 ≈110 分钟 ⇒ 约 22 次 wait_seconds=300 的续查）。
                #   只看形态必然把大任务误杀在第 5 次（add0229b 实测：模型被
                #   硬停后只能回答"PDF 没上传/任务失败"）。豁免判据看**结果侧**
                #   有没有客观进展（status=running / pages_ocred 增长），真死
                #   循环的输出是立即报错或空返回，区分度清晰。
                progress_exempt_max=settings.loop_detect_progress_exempt_max,
            ),
            # ★★ 必须在**列表末尾**（LoopDetection 之后）。2026-09-11 新增，
            # 堵「模型吐空响应但流程当正常结束」——生产会话 GY24428:0f6d781d
            # 末次调用 336s 后返回完全空响应（content 空 + tool_calls 空），
            # loop 直接判结束，M3 完成门看到 /reports 为空 → 输出「本轮任务未产出
            # 任何交付物…请检查是否遗漏 download_from_sandbox / write_file 步骤」，
            # 把「模型没说话」误报成「你没下载作业」。详见 src/core/terminal_response.py。
            #
            # 为什么必须在末尾（非风格问题，是正确性前提）：
            # langchain 1.3.13 factory.py:1738 `add_edge("model", w_after_model[-1].name)`
            # + `range(len-1, 0, -1)` → **after_model 逆序执行，最后注册的最先看到模型输出**。
            # 若注册在 LoopDetection 之前，LoopDetection 会先给消息追加软提醒/硬停文案
            # （loop_detection.py 的 `_apply` 同款注入），使其 content 非空，
            # 本中间件的判空即被绕过 → **反而漏掉真正的空响应**。
            #
            # 代价（已实测确认可接受）：`jump_to="model"` 会短路 after_model 静态链，
            # 被判空的那一条消息不会再流经 LoopDetection（实测模型调用 2 次、
            # LoopDetection 仅 1 次）。两者职责天然不重叠——空响应轮次没有 tool_calls
            # 参与循环判定，交给本中间件独立处置更干净。
            TerminalResponseMiddleware(
                max_retries=settings.terminal_response_max_retries,
            ),
        ],
        checkpointer=checkpointer,
    )


def _build_deep_agent(*, model, tools, backend, skills, middleware, checkpointer):
    """create_deep_agent 薄封装：保留原调用参数与位置论证注释（动作二 2026-11-17）。

    动作三（2026-11-19，deepagents 0.7）：SummarizationMiddleware（经
    _SweepThenSummarize 组合件，name="SummarizationMiddleware"）已在 middleware
    列表里传入，由 0.7 _apply_custom_middleware 按 name 原位替换主链/gp 默认
    实例——列表内位置不影响最终栈序（替换发生在 core 槽位原位）。

    D3（2026-11-19）：显式声明 general-purpose 子代理（subagents=），目的只有一个
    ——给它挂 SubagentBudgetMiddleware 保险丝。要点与等价性论证（对照 0.7.24
    graph.py L696-815 声明式 spec 处理流水线）：

    · 为什么必须显式声明：0.7.24 没有任何途径给声明式子代理 bind recursion_limit
      （SubAgent spec 无 config 字段、create_sub_agent 全程不 bind config），
      而子代理经 ensure_config 自动继承父 recursion_limit ⇒ 主图 1000 步会被
      单个 task 调用完整继承。deepagents 对显式同名 spec 的处理是**不再自动加
      默认 gp**（graph.py L823），其余装配（默认 middleware 栈、tools/
      permissions/interrupt_on 继承、profile 文案处理）与自动注入路径**同一条
      流水线**（同一循环体 L687-815），故行为等价。
    · 与自动注入路径的唯一差异要手动补齐：`_gp_inheritable` 只发生在自动路径
      （L849），显式 spec 不会自动继承主链的自定义 SummarizationMiddleware
      （_SweepThenSummarize）——下面按 name 显式挑出来放进 spec，否则子代理
      会退回原生 SummarizationMiddleware，丢掉主链修过的 sweep-then-summarize
      语义（3692c67/328ac0a/7942e6b 手术史同款病根）。
    · 文案用导入常量（DEFAULT_GENERAL_PURPOSE_DESCRIPTION/DEFAULT_SUBAGENT_PROMPT）
      而非抄写，升级自动跟随 deepagents 默认演化。
    """
    # 与自动注入路径 _gp_inheritable 同语义：只继承与 gp 默认槽位同名的自定义
    # 中间件（当前即 _SweepThenSummarize，name="SummarizationMiddleware"）。
    _gp_middleware = [m for m in middleware if m.name == "SummarizationMiddleware"]
    _gp_middleware.append(
        SubagentBudgetMiddleware(max_model_calls=settings.subagent_max_model_calls)
    )
    _gp_spec: dict = {
        "name": "general-purpose",
        "description": DEFAULT_GENERAL_PURPOSE_DESCRIPTION,
        "system_prompt": DEFAULT_SUBAGENT_PROMPT,
        "middleware": _gp_middleware,
        # model/tools/permissions/interrupt_on 一律不写：声明式流水线自动继承
        # 主代理的（graph.py L698/L705/L779/L787），写了反而固化。
    }
    if skills:
        _gp_spec["skills"] = skills
    return create_deep_agent(
        model=model,
        tools=tools,
        backend=backend,
        system_prompt=PLC_AUDITOR_SYSTEM_PROMPT,
        skills=skills,
        memory=[AGENTS_MD_PATH],
        middleware=middleware,
        subagents=[_gp_spec],
        interrupt_on={
            "write_file": False,
            "read_file": False,
            "edit_file": False,
        },
        # F4（2026-11-17）：自定义 state schema——_summarization_event 改
        # last-write-wins（BinaryOperatorAggregate），堵 pending writes 重放
        # 撞车致 InvalidUpdateError 的线程 brick（GY35362:3c4b5588）。
        # 官方扩展点：聚合列表中 base_state 排最后、覆盖 middleware 注解
        # （langchain factory.py:1229 注释明确支持此用法）。
        state_schema=GeesunAgentState,
        checkpointer=checkpointer,
        # messages_modifier=trim_messages(
        #     max_tokens=200000,
        #     strategy="last",
        #     token_counter=model,
        #     include_system=True,
        # ),
        debug=True,
    )
