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

**与 M3 完成门的分工（2026-09-16 新增）**：本中间件只管到「模型层」，
下游的 M3 完成门按 `/reports` 磁盘差集判零产出——两者若各说各话，
就会出现「模型压根没产出」+「是不是漏了下载步骤」这种**归因错位**的重复提示
（生产会话 de18ad37 实锤）。故本中间件落降级文案时登记 `_FALLBACK_SIGNALS`，
API 层 `pop_fallback_signal(thread_id)` 命中即**让完成门闭嘴**，
改发 `fallback_notice()` 的诚实文案。

**介入门槛与工具结果解耦（2026-09-17 新增，修正 deer-flow 的继承缺口）**
原样照搬 deer-flow 的 `if not _tool_result_in_current_turn(messages): return None`
会漏掉最该救的一类（生产会话 e9c77ce9 第二轮实锤）：
    同一天同一会话的两轮，**同样的 thinking_truncated**，只因「本轮有没有工具结果」
    一个走了守卫（删空消息 + 注入恢复提示 → **6 秒产出正文**），另一个被直接放过
    （空消息落库 → 用户看到空白气泡、界面"突然停止"，因为降级信号也没登记）。
第二轮的形态是**纯对话追问**（用户："描述的不准确，应该是 8 个纯蓝色实心小点组成的
矩形框"），压根没有工具调用——但思考照样在 325s 里烧光 65536 输出额度。
⇒ 「输出预算被思考吃光」与工具状态**正交**，工具结果不该当门槛。
现改由 `_should_guard()` 分流：模型侧分支（thinking_truncated / thinking_only）
无条件介入，其余分支保持 deer-flow 语义。
（deer-flow 之所以踩不到：它接 DeepSeek，reasoning 通常不占 output 配额；
 且它有测试 test_empty_response_without_tool_result_is_not_retried **锁死了**
 这个行为——我们的场景是它的设计盲区，不是移植偏差。）
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
#:
#: 2026-09-17 由 1 上调至 2（方案 A）。关键点：两次重试承担**不同性质**的恢复动作，
#: 而不是把同一招重复两次 ——
#:   第 1 次（无损）：注入恢复提示。模型仍在思考模式，只是被要求「别光想、直接写结论」，
#:              推理能力不受损。生产实证有效：e9c77ce9 同会话第一轮实测「6 秒就产出了正文」。
#:   第 2 次（有损）：关闭思考模式兜底（`enable_thinking=false`），保证一定有正文产出，
#:              代价是丢掉推理链 ⇒ 产出必须向用户标注为「快速模式」（见 quick_mode_notice）。
#: 顺序不可颠倒 —— 「省 token」不等于「该先上」：先用无损的，无损救不回来再上有损的。
#: （若本值被调回 1，`_should_disable_thinking` 会让唯一那次直接关思考，
#:  语义退化为「预算不够时直接上有损招」，不会出现「无损用完、有损没机会上」的空转。）
#:
#: 耗时上限（如实更新；2026-09-11 拍板「最坏 ~11 分钟」时 max_retries=1）：
#:   理论最坏 = 首轮 336s + 重试① 336s + 重试②（关思考后只写正文，通常远小于 336s）≈ 17 分钟；
#:   典型路径 = 首轮 336s + 重试①「6 秒出正文」≈ 5.7 分钟（绝大多数情况第 1 次就结束）。
_MAX_RETRIES_PER_RUN = 2

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


@dataclass(frozen=True)
class _PendingRecovery:
    """排队中的一次恢复重试（下一轮 wrap_model_call 消费后即清）。"""

    #: 注入给模型的恢复提示正文（用户不可见）
    prompt: str
    #: 本次重试是否关闭思考模式（有损兜底）；False = 只注入提示（无损）
    disable_thinking: bool
    #: 第几次重试（从 1 起），用于日志与「快速模式」文案
    attempt: int


def _should_disable_thinking(branch: str, attempt: int, max_retries: int) -> bool:
    """本次恢复重试是否关闭思考模式 —— **有损的那一招只在最后一次出手**。

    规则：只有分支为 `thinking_truncated`、且已经是最后一次重试时才关。

    - `thinking_truncated`（输出预算被思考吃光）：同样的输入 + 同样的预算 ⇒ 不关就
      必然再次烧穿，最后一次机会赌不起，关思考是唯一出路。
    - `thinking_only`（思考正常收尾、只是没写正文）：**不关**。注入恢复提示通常就够，
      关思考只会白损复杂任务的推理质量，没有收益。
    - `max_retries == 1` 时 `attempt >= max_retries` 在第 1 次即成立 ⇒ **直接关**。
      这是有意为之：只有一次机会时「先无损后有损」排不出来，与其让有损招永远上不了场
      （L0 变死代码），不如把唯一机会用在最可能成功的那招上。
    """
    if branch != BRANCH_THINKING_TRUNCATED:
        return False
    return attempt >= max(1, int(max_retries))


def _mark_quick_mode_message(message: AIMessage, attempt: int) -> dict[str, Any] | None:
    """给「关思考兜底产出」的正文打结构化标记，供前端/下游识别这是快速模式产物。

    ⚠️ 只在消息带 id 时打标：langchain 的 add_messages reducer **按 id 覆盖**，
    而 `model_copy` 出来的消息若 id 为空会被当成**新消息**追加 → 正文重复出现。
    拿不到 id 就放弃打标（可见性由 quick_mode_notice 的完成事件兜底，不受影响）。
    """
    if not getattr(message, "id", None):
        logger.warning("[Guard] 关思考产出消息无 id，跳过结构化标记（避免正文重复落库）")
        return None
    kwargs = dict(getattr(message, "additional_kwargs", None) or {})
    kwargs["terminal_response_quick_mode"] = True
    kwargs["terminal_response_quick_mode_attempt"] = attempt
    return {"messages": [message.model_copy(update={"additional_kwargs": kwargs})]}

# ─── 恢复提示（按分支定制，均以 <system_reminder> 包裹）───

_RECOVERY_PROMPT_THINKING_TRUNCATED = (
    "<system_reminder>\n"
    "你上一轮的思考过程过长，占满了本次调用的全部输出额度，导致最终回复为空"
    "（provider 已返回 finish_reason=length 截断信号）。\n"
    "工具结果已在对话中给出。请**直接输出面向用户的最终结论**，不要重复展开推导过程，"
    "也不要再次调用工具，除非确实缺少必要信息。\n"
    "</system_reminder>"
)

#: 无工具结果版本（2026-09-17 新增，会话 e9c77ce9 第二轮回归位）。
#: 上一版只有 post-tool 文案，其中「工具结果已在对话中给出」在本轮压根没调过工具时
#: 是**事实错误**，会把模型带偏；且此时模型往往还没拿到完成任务所需的信息，
#: 一刀切「不要再次调用工具」会堵死它唯一的出路。故单独成文：
#:   - 强调**思考保持简短**（根因就是思考膨胀，这是唯一能改变行为的约束）
#:   - 保留最多一次工具调用的出口
_RECOVERY_PROMPT_THINKING_TRUNCATED_NO_TOOL = (
    "<system_reminder>\n"
    "你上一轮的思考过程过长，占满了本次调用的全部输出额度，导致最终回复为空"
    "（provider 已返回 finish_reason=length 截断信号）。\n"
    "请**直接输出面向用户的最终结论**，思考过程务必保持简短。"
    "若结论依赖尚未获取的信息，最多再调用一次工具；否则请基于已有信息作答。\n"
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
# 降级信号登记表（2026-09-16 新增）——把「真实原因」交给 API 层
# ══════════════════════════════════════════════════════════════════
#
# 背景（生产会话 GY24428:de18ad37 实测）：
#   17:30:07 本中间件落盘降级文案（reasoning 吃满 65536 输出上限被截断）；
#   **同一秒** M3 完成门（chat.py）按 `/reports` 磁盘差集反推，又输出
#   「本轮任务未产出任何交付物（/reports 为空）/ 请检查是否遗漏 download_from_sandbox /
#   write_file 步骤」——**归因错误**：模型一个字都没产出，谈不上"漏了哪一步"。
#   两条消息重复出现，且都指向错误方向，用户拿不到可行动信息。
#
# 修法：本中间件在「预算用尽 → 落盘降级文案」时，顺手登记一条**结构化信号**；
#   API 层（chat.py）在本轮 stream 结束后 `pop_fallback_signal(thread_id)` 取走，
#   若存在则**跳过 M3 完成门的零产出拦截**，改发描述真实原因的完成事件。
#   读取方是唯一消费者，故用 pop（取走即清，避免上一轮信号在下一轮重复上报）。
#
# 为什么不用 runtime.context 传递（deer-flow 的做法）：`invoke_kwargs["context"]`
#   只在 `body.model_override` 存在时才构造（chat.py:844-847），无 override 的常态
#   路径上 context 未必是我们能写的 dict；而本中间件已用 `_key(runtime)`（thread_id）
#   做重试预算隔离，复用同一把键最省心，也不依赖调用方要不要传 context。
_FALLBACK_SIGNALS: OrderedDict[str, dict[str, Any]] = OrderedDict()

#: 信号表容量上限（长驻服务防泄漏；thread_id 语义与重试预算一致）
_FALLBACK_SIGNAL_WINDOW = 500

_fallback_lock = threading.Lock()


def _record_fallback_signal(
    key: str, branch: str, finish_reason: str, reasoning_len: int, retries: int
) -> None:
    with _fallback_lock:
        _FALLBACK_SIGNALS[key] = {
            "branch": branch,
            "finish_reason": finish_reason,
            "reasoning_len": reasoning_len,
            "retries": retries,
        }
        _FALLBACK_SIGNALS.move_to_end(key)
        while len(_FALLBACK_SIGNALS) > _FALLBACK_SIGNAL_WINDOW:
            _FALLBACK_SIGNALS.popitem(last=False)


def pop_fallback_signal(thread_id: str) -> dict[str, Any] | None:
    """取走并清除该 thread 最近一次「空响应降级」信号（无则 None）。

    返回：``{"branch", "finish_reason", "reasoning_len", "retries"}``。
    thread_id 用与 chat.py 相同的 ``f"{user_id}:{session_id}"``。
    """
    with _fallback_lock:
        return _FALLBACK_SIGNALS.pop(str(thread_id), None)


#: 分支 → 「模型层真实发生了什么」的一句话（不猜下游、不谈磁盘）
_NOTICE_REASON = {
    BRANCH_THINKING_TRUNCATED: (
        "模型本轮的思考过程占满了单次输出上限被截断（finish_reason=length），"
        "没有产出任何面向用户的正文。"
    ),
    BRANCH_THINKING_ONLY: "模型本轮只完成了思考，没有输出面向用户的正文内容。",
    BRANCH_FULLY_EMPTY: "模型本轮返回了完全空响应（正文与思考均为空）。",
}


def fallback_notice(branch: str, retries: int) -> tuple[str, str]:
    """返回 ``(reason, hint)``，供 API 层在完成门之外单独上报空响应降级。

    与 `_fallback_content` 的分工：那个写进**对话气泡**（用户必然看到，说明"没内容"），
    这个用于**状态条**（说明"为什么没内容" + 给动作），两者文案不重复。
    hint 只给用户能执行的动作，不再出现「是不是漏了某一步」这类无据推测。
    """
    reason = _NOTICE_REASON.get(branch, _NOTICE_REASON[BRANCH_FULLY_EMPTY])
    if retries > 0:
        reason += f"系统已自动重试 {retries} 次仍未成功。"
    if branch == BRANCH_THINKING_TRUNCATED:
        hint = "建议把任务拆小后重发（如分批处理），或改用输出上限更高的模型。"
    else:
        hint = "建议重发一次；若反复出现，请把会话 ID 反馈给模型服务维护方。"
    return reason, hint


#: 信号表里区分「关思考兜底成功」与「空响应降级失败」的字段。
#: 两者共用 `_FALLBACK_SIGNALS` 同一张表 + pop 语义（读取方是唯一消费者）。
_SIGNAL_QUICK_MODE = "quick_mode"


def _record_quick_mode_signal(key: str, attempt: int) -> None:
    """登记「本轮正文是关思考兜底产出的」信号。

    与 `_record_fallback_signal` 的分工（消费方行为相反，别混）：
      - 那个用于**失败**收尾（正文为空、落降级文案）⇒ 消费方要**拦截** M3 完成门；
      - 本函数用于**成功**收尾（关思考后拿到了正文）⇒ 消费方**不得拦截**完成门
        （明明有产出），只需向用户说明这条回复的性质。
    """
    with _fallback_lock:
        _FALLBACK_SIGNALS[key] = {
            _SIGNAL_QUICK_MODE: True,
            "attempt": int(attempt),
        }
        _FALLBACK_SIGNALS.move_to_end(key)
        while len(_FALLBACK_SIGNALS) > _FALLBACK_SIGNAL_WINDOW:
            _FALLBACK_SIGNALS.popitem(last=False)


def quick_mode_notice(attempt: int) -> tuple[str, str]:
    """返回 ``(reason, hint)``：告知用户本条回复是「关闭深度思考」换来的。

    为什么必须提示而不是静默返回：关思考的产出与正常回复**外观完全一样**，
    用户分辨不出，会把它当完整答案使用。空白气泡是**显性失败**（用户立刻知道出事了，
    会重发、会拆任务），静默降级是**隐性失败** —— 一旦碰上真正依赖长推理链的任务，
    用户拿到看似合理实则浅的答案且不会怀疑。**明着失败安全，静默降级危险。**
    """
    return (
        "模型本轮的思考过程超出了单次输出上限被截断，系统已自动改用「快速模式」"
        "（关闭深度思考）重新生成。下方内容即为快速模式的产出，结论可用，"
        "但深度分析可能不如常规模式完整。",
        "若需要更完整的推理，建议把任务拆小后重发。",
    )


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


#: 「模型侧预算耗尽」分支：根因在模型自身（输出额度被思考吃光），与本轮有没有
#: 调过工具**完全无关**，因此不能拿 `_tool_result_in_current_turn` 当门槛。
#:
#: 依据（生产会话 GY24428:e9c77ce9 第二轮，2026-09-17 08:18→08:23 实测）：
#:   用户追问「描述的不准确，应该是 8 个纯蓝色实心小点组成的矩形框」——本轮**没有**
#:   任何工具调用，模型在 325s 里把 65536 输出预算全烧在思考里（reasoning 93634 字符），
#:   content 为空、finish_reason='length'。旧逻辑因「本轮无工具结果」直接放过 →
#:   空 AIMessage 原样落库 → 用户只看到空白气泡；且降级信号未登记，
#:   chat.py 的 pop_fallback_signal 取不到 → 连状态条文案都没有，表现为"突然停止"。
#:   对照同会话第一轮：同样的 thinking_truncated，但本轮有工具结果 → 走守卫
#:   （删空消息 + 注入恢复提示）→ **6 秒就产出了正文**。可见这道门槛挡掉的
#:   恰恰是最该救的那一类。
_MODEL_SIDE_BRANCHES = frozenset({BRANCH_THINKING_TRUNCATED, BRANCH_THINKING_ONLY})


def _should_guard(branch: str, has_tool_result: bool) -> bool:
    """守卫是否介入该分支。

    - **模型侧预算耗尽**（thinking_truncated / thinking_only）→ 无条件介入。
      这两个分支的判据本身已足够特异（content 空 + reasoning 非空 + 有/无
      length 截断信号），且与工具状态正交，不需要再用工具结果做过滤。
    - **其余分支**（含 fully_empty）→ 保持 deer-flow 语义，只兜「本轮已有工具结果」
      的终点场景；无工具结果时交给 M3 完成门与模型自身处理，避免把「压根没开始 /
      无真实用户消息的内部调用」也纳进来。
    """
    if branch in _MODEL_SIDE_BRANCHES:
        return True
    return has_tool_result


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


def _recovery_prompt_for(branch: str, has_tool_result: bool = True) -> str:
    """按分支（+ 本轮有无工具结果）挑恢复提示。

    has_tool_result 默认 True 保持旧调用点兼容；thinking_truncated 的两个版本
    差异见 _RECOVERY_PROMPT_THINKING_TRUNCATED_NO_TOOL 的说明。
    """
    if branch == BRANCH_THINKING_TRUNCATED:
        return (
            _RECOVERY_PROMPT_THINKING_TRUNCATED
            if has_tool_result
            else _RECOVERY_PROMPT_THINKING_TRUNCATED_NO_TOOL
        )
    if branch == BRANCH_THINKING_ONLY:
        return _RECOVERY_PROMPT_THINKING_ONLY
    return _RECOVERY_PROMPT_FULLY_EMPTY


# ══════════════════════════════════════════════════════════════════
# Middleware
# ══════════════════════════════════════════════════════════════════


class TerminalResponseMiddleware(AgentMiddleware[AgentState]):
    """空响应终点守卫：重试 max_retries 次，仍空则落盘可读降级文案。

    介入门槛（`_should_guard`）：模型侧分支（thinking_truncated / thinking_only）
    无条件介入；其余分支只兜「本轮已有工具结果」的终点场景。

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
        # thread_id -> 待注入的恢复重试（下一轮 wrap_model_call 消费后即清）
        self._pending_prompts: OrderedDict[str, _PendingRecovery] = OrderedDict()
        # thread_id -> 本轮模型调用被关思考的「重试序号」（_augment_request 记、_apply 取）。
        # 存在的原因：after_model 只看得到「模型返回了什么」，看不到「这是第几次重试、
        # 有没有关思考」——正文非空时无从判断该不该打「快速模式」标记。故由
        # wrap_model_call 在注入时留下凭据，after_model 消费后立即清除。
        self._quick_mode_pending: OrderedDict[str, int] = OrderedDict()
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
                self._quick_mode_pending.pop(old_key, None)

    def _count(self, key: str) -> int:
        with self._lock:
            return self._retry_counts.get(key, 0)

    def _reset(self, key: str) -> None:
        with self._lock:
            self._retry_counts.pop(key, None)
            self._pending_prompts.pop(key, None)
            self._quick_mode_pending.pop(key, None)

    def _queue_prompt(self, key: str, pending: _PendingRecovery) -> None:
        with self._lock:
            self._pending_prompts[key] = pending
            self._pending_prompts.move_to_end(key)
            while len(self._pending_prompts) > _BUDGET_WINDOW:
                self._pending_prompts.popitem(last=False)

    def _drain_prompt(self, key: str) -> _PendingRecovery | None:
        with self._lock:
            return self._pending_prompts.pop(key, None)

    def _mark_quick_mode(self, key: str, attempt: int) -> None:
        """记下「本轮模型调用被关了思考」（由 _augment_request 在注入时调用）。"""
        with self._lock:
            self._quick_mode_pending[key] = int(attempt)
            self._quick_mode_pending.move_to_end(key)
            while len(self._quick_mode_pending) > _BUDGET_WINDOW:
                self._quick_mode_pending.popitem(last=False)

    def _take_quick_mode(self, key: str) -> int | None:
        """取走并清除「本轮关过思考」的凭据（由 _apply 在正文非空时调用）。

        取走即清、不区分结果：即使本轮仍没产出正文（还要继续重试），凭据也不该留到
        下一次 —— 下一次的 _augment_request 会重新决定并按需写入。
        """
        with self._lock:
            return self._quick_mode_pending.pop(key, None)

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
            # 正文非空 ⇒ 若上一轮模型调用是「关思考兜底」，本条就是快速模式产物：
            # 登记信号（让 API 层告知用户）+ 给消息打结构化标记（供前端识别）。
            # 这是方案 A 第②条的落点 —— 关思考的产出必须留痕，否则用户会把
            # 「快速模式的结果」当成完整答案使用（比空白气泡更危险的隐性失败）。
            key_ok = self._key(runtime)
            quick_attempt = self._take_quick_mode(key_ok)
            if quick_attempt is None:
                return None
            _record_quick_mode_signal(key_ok, quick_attempt)
            logger.warning(
                "[Guard] 关思考兜底成功产出正文（第 %d 次重试，快速模式）"
                "→ 登记提示信号: thread=%s",
                quick_attempt, key_ok,
            )
            return _mark_quick_mode_message(last, quick_attempt)

        # 分流：模型侧预算耗尽的分支与「有没有工具结果」无关，必须兜底；
        # 其余形态保持 deer-flow 语义，只兜「本轮已有工具结果」的终点场景。
        has_tool_result = _tool_result_in_current_turn(messages)
        if not _should_guard(branch, has_tool_result):
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
            attempt = used + 1
            disable_thinking = _should_disable_thinking(branch, attempt, self.max_retries)
            self._queue_prompt(
                key,
                _PendingRecovery(
                    prompt=_recovery_prompt_for(branch, has_tool_result),
                    disable_thinking=disable_thinking,
                    attempt=attempt,
                ),
            )
            # 删掉空消息，避免它留在 checkpoint 历史里污染后续上下文；
            # 注意：RemoveMessage 是写 state 的操作，这是期望行为（用户不该看到空白气泡）。
            message_updates = [RemoveMessage(id=last.id)] if getattr(last, "id", None) else []
            logger.warning(
                "[Guard] 检出空响应（终点守卫第 %d/%d 次）→ 删除并重试: "
                "branch=%s finish_reason=%r reasoning_len=%d thread=%s 恢复方式=%s",
                attempt, self.max_retries, branch,
                _finish_reason(last), len(_reasoning_text(last)), key,
                "关闭思考（有损兜底）" if disable_thinking else "注入恢复提示（无损）",
            )
            return {"messages": message_updates, "jump_to": "model"}

        # 预算用尽：写可读降级文案（不再抛异常，SSE 不中断）
        finish_reason = _finish_reason(last)
        reasoning_len = len(_reasoning_text(last))
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
        # 登记结构化信号：让 API 层能跳过 M3 完成门的「零产出 → 猜漏了下载步骤」
        # 误归因路径，改由 fallback_notice 输出模型层的真实原因（见模块头注释）。
        _record_fallback_signal(key, branch, finish_reason, reasoning_len, self.max_retries)
        logger.error(
            "[Guard] 空响应自动重试已用尽，落盘降级文案并登记降级信号: branch=%s "
            "finish_reason=%r reasoning_len=%d thread=%s",
            branch, finish_reason, reasoning_len, key,
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
        """把排队的恢复重试追加到出站请求（提示注入 + 按需关思考）。

        用 `request.override(...)` 而非写 state：实测该注入只对本轮模型调用可见、
        **不写回 state**，因此不会进 checkpoint、不会被 _persist_session 落库污染历史
        （.workbuddy/spikes/inject_state_spike.py 对照实测）。
        `model_settings` 的覆盖同理只作用于本轮（factory 每轮重新 bind）。

        用 HumanMessage 而不是 SystemMessage：规避 vLLM
        「System message must be at the beginning」400（chat.py:1264-1267 有记录）。
        附 hide_from_ui 标记，便于日志/调试分辨系统注入消息（当前持久化路径
        不读该标记，但因为不进 state 所以无泄漏风险）。

        两种恢复动作（见 `_should_disable_thinking`）：
          - 无损：只注入提示，模型仍在思考模式 —— 靠 `request.override(messages=...)`；
          - 有损：额外关思考 —— 靠 `request.override(model_settings=...)` 带 `extra_body`
            （唯一可用通道，验证见 `tests/spikes/extra_body_channel_probe.py`）。
        """
        runtime = getattr(request, "runtime", None)
        if runtime is None:
            return request
        key = self._key(runtime)
        pending = self._drain_prompt(key)
        if pending is None:
            return request
        reminder = HumanMessage(
            content=pending.prompt,
            name="terminal_response_recovery",
            additional_kwargs={"hide_from_ui": True},
        )
        overrides: dict[str, Any] = {"messages": [*request.messages, reminder]}
        if pending.disable_thinking:
            # 「关思考」的唯一可用通道（本仓 model.py 里 extra_body / chat_template_kwargs
            # 均 grep 零命中）：model_settings → langchain factory `model.bind(**model_settings)`
            # （.venv/.../langchain/agents/factory.py:1404，**全量 bind、无白名单过滤**）
            # → langchain-openai 合进 payload（base.py `payload = {**self._default_params, **kwargs}`）
            # → OpenAI SDK 识别 `extra_body` 并展平进请求体顶层。
            # 该链路已用本地 mock server 捕获真实请求体验证（4/4 PASS）：
            # tests/spikes/extra_body_channel_probe.py。
            overrides["model_settings"] = {
                **request.model_settings,
                "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
            }
            # 留凭据：after_model 只看得到「模型返回了什么」，看不到「这一次关没关思考」，
            # 正文非空时无从判断该不该打「快速模式」标记。
            self._mark_quick_mode(key, pending.attempt)
            logger.warning(
                "[Guard] 第 %d 次重试：关闭思考模式兜底（enable_thinking=false，"
                "产出将标注为快速模式）: thread=%s",
                pending.attempt, key,
            )
        logger.info("[Guard] 注入恢复提示（用户不可见）: thread=%s", key)
        return request.override(**overrides)
