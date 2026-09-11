"""终点响应守卫 middleware —— 兜住「模型吐空响应但流程当正常结束」的静默失败。

背景（2026-09-10 生产实锤，会话 GY24428:0f6d781d）：
    末次模型调用耗时 **336 秒**后返回**完全空响应**（content 空 + tool_calls 空），
    deepagents loop 直接判结束 → M3 完成门看到 /reports 为空 → 输出
    「本轮任务未产出任何交付物（/reports 为空）/ 请检查是否遗漏 download_from_sandbox
    / write_file 步骤」。**归因完全错误**——模型连一个 tool_call 都没发出，
    却把责任推给「是不是漏了下载步骤」，用户看到的是「你没交作业」。

    336s × ~195 tok/s ≈ 65.5k tokens ≈ model_max_tokens=65536 → 高度怀疑
    是 thinking 吃光输出预算被 finish_reason=length 截断在 think 段内
    （--reasoning-parser qwen3 下全部 token 落 reasoning 字段，content 恒空）。

移植来源：deer-flow `TerminalResponseMiddleware`
    （packages/harness/deerflow/agents/middlewares/terminal_response_middleware.py，
    224 行，2026-09-10 精读）。这是我们这个 bug 的 1:1 现成解。

**但 deer-flow 有两个盲区，正好都落在我们这次的故障点上，照抄不够**：
  1) 它的姊妹中间件 ModelLengthFinishReasonMiddleware 显式排除空 content
     （`if not _has_visible_content(last): return None`）→ 接不住我们的「纯 reasoning 截断」；
  2) deepseek-harness 判空靠 `order.length === 0`，而 reasoning block 计入 order
     → 同样接不住。
    正解 = deer-flow 的检测位置 + dsh 的重试强度 + 我们独有的 reasoning 感知判定。

**我们独有的四分支判定**（ReasoningChatOpenAI 把 thinking 分离进
`additional_kwargs["reasoning_content"]`，与 content 天然可分辨）：
    ┌─ ① 正常：content 非空 → 放行
    ├─ ② thinking 截断：content 空 + reasoning 非空 + finish_reason=length
    │     ← 本次故障形态（思考吃光预算被硬截断）
    ├─ ③ 思考完没说：content 空 + reasoning 非空 + finish_reason=stop
    └─ ④ 彻底空：content 空 + reasoning 也空

三条**必须遵守**的实现约束（全部有实测支撑，见 docs/finish-reason-forensics-2026-09-11.html）：

  1) **注册位置：必须在 LoopDetectionMiddleware 之后（middleware 列表末尾）**。
     langchain 1.3.13 实测（factory.py:1738 `add_edge("model", w_after_model[-1])`
     + `range(len-1,0,-1)`）：**after_model 逆序执行，最后注册的最先看到模型输出**。
     若注册在 LoopDetection 之前，LoopDetection 可能已给消息追加软提醒/硬停文案，
     会让本中间件的 content 非空判定被误触发、**反而错过真正的空响应**。

  2) **sync 与 async 两个 hook 都必须加 `@hook_config(can_jump_to=["model"])`**。
     实测（factory.py:1978 `if can_jump_to:`）：不带该装饰器时走静态边
     `add_edge(name, default_destination)`，**根本不读 `state["jump_to"]`，且不报错**。
     只给 sync 加、async 漏掉的话，我们走 `astream`（异步路径）时会**静默失效** ——
     症状是「空消息被删了但模型没重跑直接结束」，比不修更糟。

  3) **恢复提示用 `wrap_model_call` + `request.override()` 注入，不要写 state**。
     实测（.workbuddy/spikes/inject_state_spike.py）：`request.override()` 注入的消息
     只对本轮模型调用可见、**不写回 agent state**，因此不会进 checkpoint、
     不会被 chat.py:443-528 的 `_persist_session` 落库污染历史。
     （对照：after_model 返回 `{"messages": [...]}` 会进 state 并落库。）
     这也是 loop_detection.py:410-412 已用的成熟模式。

**重试预算语义**：沿用 deer-flow 的「**每次 run 只给 1 次**」而非「每条空消息 1 次」——
deer-flow 源码注释明确写了这个陷阱：
    "A retry that calls another tool must not refresh the budget and create
     an unbounded empty -> retry -> tool loop."
1 次而非 dsh 的 5 次：我们单次调用最坏 336s，1 次重试最坏 ~11 分钟；
5 次将超过 28 分钟，用户无法接受（2026-09-11 用户拍板）。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

# ─── 分支配额 ───

#: 每次 run 允许的自动重试次数（deer-flow 用 1，deepseek-harness 用 5）
#: 取 1 的理由：单次调用最坏 336s，1 次重试最坏 ~11 分钟；5 次超 28 分钟不可接受。
_MAX_RETRIES_PER_RUN = 1

#: 重试仍空时落盘的降级文案（面向用户，必须说人话且不误导）。
#: 做成函数而非常量：max_retries 现在是可配置项（settings.terminal_response_max_retries），
#: 硬编码「已自动重试 1 次」在 max_retries=0 时会撒谎（一次都没重试）、
#: 在 >1 时又少报次数。
def _fallback_content(retries: int) -> str:
    head = (
        "模型本轮没有产出可用的回复内容。"
        if retries <= 0
        else f"模型本轮没有产出可用的回复内容（已自动重试 {retries} 次仍未成功）。"
    )
    return (
        head
        + "这通常是因为单次思考过程过长、超出了输出上限而被截断。"
        "建议：把任务拆小一些重试，或换用其他模型。"
    )


#: 默认预算下的文案（仅为兼容旧引用/测试保留，实际使用 _fallback_content）
_FALLBACK_CONTENT = _fallback_content(_MAX_RETRIES_PER_RUN)

#: 窗口大小：只保留最近 N 个 run 的预算，防长驻服务内存泄漏
_BUDGET_WINDOW = 500

# ─── 恢复提示（按分支定制，均以 <system_reminder> 包裹）───

_RECOVERY_PROMPT_THINKING_TRUNCATED = (
    "<system_reminder>\n"
    "你上一轮的思考过程过长，占满了本次调用的全部输出额度，导致最终回复为空"
    "（provider 已返回 finish_reason=length 截断信号）。\n"
    "工具结果已在对话中给出。请**直接输出面向用户的最终结论**，不要重复展开推导过程，"
    "也不要再次调用工具，除非确实缺少必要信息。\n"
    "</system_reminder>"
)

_RECOVERY_PROMPT_THINKING_ONLY = (
    "<system_reminder>\n"
    "你上一轮已经完成了思考，但没有输出任何面向用户可见的正文内容。\n"
    "请根据你的思考和已有工具结果，**直接给出简洁的最终答复**。"
    "不要再次调用工具，除非确实缺少必要信息。\n"
    "</system_reminder>"
)

_RECOVERY_PROMPT_FULLY_EMPTY = (
    "<system_reminder>\n"
    "你上一轮的回复为空。请检视对话中已有的工具执行结果，"
    "给出一个面向用户的最终答复。若确实尚未开始执行任务，请调用必要的工具推进。\n"
    "</system_reminder>"
)

#: finish_reason == "tool_calls" 时说明模型本意是调工具，不归本中间件管
_TOOL_CALL_FINISH_REASONS = frozenset({"tool_calls", "function_call"})

#: 分支常量（便于日志统计与测试断言）
BRANCH_OK = "ok"
BRANCH_THINKING_TRUNCATED = "thinking_truncated"
BRANCH_THINKING_ONLY = "thinking_only"
BRANCH_FULLY_EMPTY = "fully_empty"


# ══════════════════════════════════════════════════════════════════
# 判定辅助
# ══════════════════════════════════════════════════════════════════


def _has_visible_content(message: AIMessage) -> bool:
    """模型是否产出了用户可见正文（对齐 deer-flow 的同名函数）。

    兼容 str / list（OpenAI content parts）两种形态。
    """
    content = message.content
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str) and block.strip():
                return True
            if isinstance(block, dict):
                if block.get("type") in {"text", "output_text"}:
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        return True
    return False


def _reasoning_text(message: AIMessage) -> str:
    """取出 thinking 文本。

    ReasoningChatOpenAI（model.py:251-282）把 provider 的推理字段写进
    `additional_kwargs["reasoning_content"]`，与 content 严格分离。
    另兼容 deepseek-harness 风格的 `reasoning` 键。
    """
    kwargs = getattr(message, "additional_kwargs", None) or {}
    for key in ("reasoning_content", "reasoning"):
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            return value
        # 少数 provider 用 list[dict]
        if isinstance(value, list):
            parts = [
                b.get("text", "")
                for b in value
                if isinstance(b, dict)
            ]
            joined = "".join(p for p in parts if isinstance(p, str))
            if joined.strip():
                return joined
    return ""


def _finish_reason(message: AIMessage) -> str:
    """读取 finish_reason。

    落点实测（langchain_openai 1.2.1 / langchain-core 1.4.9）：
      - langchain_openai/chat_models/base.py:1362 先把流式 finish_reason 放进
        `generation_info`；
      - langchain_core/language_models/chat_models.py:2109（异步）/ :781（同步）执行
        `chunk.message.response_metadata = _gen_info_and_msg_metadata(chunk)`，
        再经 `chat_models.py:2677` 的 `{**generation_info, **response_metadata}` 合并。
    所以**聚合后的 AIMessage 上读 `response_metadata["finish_reason"]` 是可用的**
    （生产实证：Langfuse GENERATION output 里
     `"response_metadata": {"finish_reason": "tool_calls", ...}`；
     本项目 loop_detection.py:357 也已在用同一路径）。
    """
    meta = getattr(message, "response_metadata", None) or {}
    value = meta.get("finish_reason")
    return str(value) if value else ""


def _has_tool_call_intent(message: AIMessage) -> bool:
    """模型是否有调工具意图或畸形工具调用（对齐 deer-flow）。

    有 tool_call 意图说明这不是「终点响应」问题，交给正常工具路由处理。
    """
    if getattr(message, "tool_calls", None):
        return True
    if getattr(message, "invalid_tool_calls", None):
        return True
    kwargs = getattr(message, "additional_kwargs", None) or {}
    if kwargs.get("tool_calls") or kwargs.get("function_call"):
        return True
    return _finish_reason(message) in _TOOL_CALL_FINISH_REASONS


def _tool_result_in_current_turn(messages: list[Any]) -> bool:
    """本轮（最近一条真实用户消息之后）是否已有工具执行结果。

    对齐 deer-flow `_tool_result_in_current_turn`：只兜「工具跑完了但没总结」的场景，
    避免把「压根没开始/无用户消息的内部调用」也纳进来。
    跳过带 hide_from_ui 标记的注入消息（那些不是真实用户消息）。
    """
    latest_user_index = -1
    for index, message in enumerate(messages):
        if not isinstance(message, HumanMessage):
            continue
        if (getattr(message, "additional_kwargs", None) or {}).get("hide_from_ui"):
            continue
        latest_user_index = index
    if latest_user_index == -1:
        # 无真实用户消息（内部/定时调用）：不看历史工具结果，交给 M3 门判定
        return False
    return any(
        isinstance(message, ToolMessage)
        for message in messages[latest_user_index + 1 :]
    )


def classify_empty_response(message: AIMessage) -> str:
    """四分支判定（本中间件相对两个参考仓的核心增量）。

    返回 BRANCH_* 常量之一。
    """
    if _has_visible_content(message):
        return BRANCH_OK
    if _has_tool_call_intent(message):
        return BRANCH_OK  # 交给工具路由，不算空响应

    reasoning = _reasoning_text(message)
    finish_reason = _finish_reason(message)

    if reasoning:
        if finish_reason == "length":
            return BRANCH_THINKING_TRUNCATED
        return BRANCH_THINKING_ONLY
    return BRANCH_FULLY_EMPTY


def _recovery_prompt_for(branch: str) -> str:
    if branch == BRANCH_THINKING_TRUNCATED:
        return _RECOVERY_PROMPT_THINKING_TRUNCATED
    if branch == BRANCH_THINKING_ONLY:
        return _RECOVERY_PROMPT_THINKING_ONLY
    return _RECOVERY_PROMPT_FULLY_EMPTY


# ══════════════════════════════════════════════════════════════════
# Middleware
# ══════════════════════════════════════════════════════════════════


class TerminalResponseMiddleware(AgentMiddleware[AgentState]):
    """空响应终点守卫：重试 max_retries 次，仍空则落盘可读降级文案。

    处置流程（单次 run 内，预算由 settings.terminal_response_max_retries 控制，默认 1）：
        预算未用尽 → 删掉空 AIMessage（RemoveMessage）+ jump_to="model"
                      + wrap_model_call 注入分支定制的恢复提示（用户不可见）
        预算用尽   → 把 content 改写为降级文案（_fallback_content），并打标
                      additional_kwargs["terminal_response_fallback"]=True
    """

    def __init__(self, max_retries: int = _MAX_RETRIES_PER_RUN) -> None:
        super().__init__()
        self.max_retries = max(0, int(max_retries))
        self._lock = threading.Lock()
        # thread_id -> 已用重试次数
        self._retry_counts: OrderedDict[str, int] = OrderedDict()
        # thread_id -> 待注入的恢复提示（下一轮 wrap_model_call 消费后即清）
        self._pending_prompts: OrderedDict[str, str] = OrderedDict()
        # 统计（便于生产日志确认是否真的在生效）
        self._stats: dict[str, int] = {}

    # ─── 预算与提示队列 ───

    @staticmethod
    def _key(runtime: Runtime) -> str:
        """取 run 隔离键。

        用 runtime.execution_info.thread_id（langgraph/runtime.py:39）——
        Runtime 不含 config（runtime.py:131 明示），所以不能用
        runtime.context["thread_id"]（deer-flow 的做法，对我们不适用）。
        与 loop_detection.py:224-233 保持一致。
        """
        info = getattr(runtime, "execution_info", None)
        thread_id = getattr(info, "thread_id", None) if info is not None else None
        return str(thread_id) if thread_id else "default"

    def _bump(self, key: str) -> None:
        with self._lock:
            self._retry_counts[key] = self._retry_counts.get(key, 0) + 1
            self._retry_counts.move_to_end(key)
            while len(self._retry_counts) > _BUDGET_WINDOW:
                old_key, _ = self._retry_counts.popitem(last=False)
                self._pending_prompts.pop(old_key, None)

    def _count(self, key: str) -> int:
        with self._lock:
            return self._retry_counts.get(key, 0)

    def _reset(self, key: str) -> None:
        with self._lock:
            self._retry_counts.pop(key, None)
            self._pending_prompts.pop(key, None)

    def _queue_prompt(self, key: str, prompt: str) -> None:
        with self._lock:
            self._pending_prompts[key] = prompt
            self._pending_prompts.move_to_end(key)
            while len(self._pending_prompts) > _BUDGET_WINDOW:
                self._pending_prompts.popitem(last=False)

    def _drain_prompt(self, key: str) -> str | None:
        with self._lock:
            return self._pending_prompts.pop(key, None)

    def _note(self, branch: str) -> None:
        with self._lock:
            self._stats[branch] = self._stats.get(branch, 0) + 1

    def stats(self) -> dict[str, int]:
        """只读统计快照（测试/运维用）。"""
        with self._lock:
            return dict(self._stats)

    # ─── 核心判定 ───

    def _apply(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = list((state or {}).get("messages") or [])
        if not messages:
            return None
        last = messages[-1]
        if not isinstance(last, AIMessage):
            return None

        branch = classify_empty_response(last)
        if branch == BRANCH_OK:
            return None

        # 只兜「本轮已有工具结果」的终点场景；其它形态交给 M3 完成门与模型自身处理
        if not _tool_result_in_current_turn(messages):
            logger.info(
                "[Guard] 空响应但本轮无工具结果，跳过终点守卫: branch=%s thread=%s",
                branch, self._key(runtime),
            )
            return None

        key = self._key(runtime)
        used = self._count(key)
        self._note(branch)

        if used < self.max_retries:
            self._bump(key)
            self._queue_prompt(key, _recovery_prompt_for(branch))
            # 删掉空消息，避免它留在 checkpoint 历史里污染后续上下文；
            # 注意：RemoveMessage 是写 state 的操作，这是期望行为（用户不该看到空白气泡）。
            message_updates = [RemoveMessage(id=last.id)] if getattr(last, "id", None) else []
            logger.warning(
                "[Guard] 检出空响应（终点守卫第 %d/%d 次）→ 删除并重试: "
                "branch=%s finish_reason=%r reasoning_len=%d thread=%s",
                used + 1, self.max_retries, branch,
                _finish_reason(last), len(_reasoning_text(last)), key,
            )
            return {"messages": message_updates, "jump_to": "model"}

        # 预算用尽：写可读降级文案（不再抛异常，SSE 不中断）
        additional_kwargs = dict(getattr(last, "additional_kwargs", None) or {})
        additional_kwargs.update(
            {
                "terminal_response_fallback": True,
                "terminal_response_branch": branch,
            }
        )
        fallback = last.model_copy(
            update={
                "content": _fallback_content(self.max_retries),
                "additional_kwargs": additional_kwargs,
                # 清掉 tool 元数据，确保序列化为纯 assistant 文本
                "tool_calls": [],
                "invalid_tool_calls": [],
            }
        )
        logger.error(
            "[Guard] 空响应自动重试已用尽，落盘降级文案: branch=%s finish_reason=%r "
            "reasoning_len=%d thread=%s",
            branch, _finish_reason(last), len(_reasoning_text(last)), key,
        )
        return {"messages": [fallback]}

    # ─── hooks ───

    @override
    def before_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        # 每个新 run 重置预算（deer-flow 同款语义：after_agent 可能被
        # Command(goto=END) 绕过，所以 here 也重置一次）
        self._reset(self._key(runtime))
        return None

    @override
    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self.before_agent(state, runtime)

    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._reset(self._key(runtime))
        return None

    @override
    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self.after_agent(state, runtime)

    # ⚠ 两个 hook 都必须带 @hook_config：不带时 langchain 走静态边、
    #   静默忽略 state["jump_to"] 且不报错（factory.py:1978 实测）。
    #   漏掉 async 会导致 astream 路径失效 → 空消息被删但模型不重跑。
    @hook_config(can_jump_to=["model"])
    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        return self._apply(state, runtime)

    @hook_config(can_jump_to=["model"])
    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        return self._apply(state, runtime)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._augment_request(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._augment_request(request))

    def _augment_request(self, request: ModelRequest) -> ModelRequest:
        """把排队的恢复提示追加到出站消息末尾。

        用 `request.override(...)` 而非写 state：实测该注入只对本轮模型调用可见、
        **不写回 state**，因此不会进 checkpoint、不会被 _persist_session 落库污染历史
        （.workbuddy/spikes/inject_state_spike.py 对照实测）。

        用 HumanMessage 而不是 SystemMessage：规避 vLLM
        「System message must be at the beginning」400（chat.py:1264-1267 有记录）。
        附 hide_from_ui 标记，便于日志/调试分辨系统注入消息（当前持久化路径
        不读该标记，但因为不进 state 所以无泄漏风险）。
        """
        runtime = getattr(request, "runtime", None)
        if runtime is None:
            return request
        key = self._key(runtime)
        prompt = self._drain_prompt(key)
        if not prompt:
            return request
        reminder = HumanMessage(
            content=prompt,
            name="terminal_response_recovery",
            additional_kwargs={"hide_from_ui": True},
        )
        logger.info("[Guard] 注入恢复提示（用户不可见）: thread=%s", key)
        return request.override(messages=[*request.messages, reminder])
