# -*- coding: utf-8 -*-
"""子代理模型调用预算中间件（D3，2026-11-19）。

## 它替代什么：为什么不是 bind recursion_limit

方案原本想给 general-purpose 子代理 bind 一个小 `recursion_limit`（~200）当
保险丝，防止子代理在主图一次 `task` 工具调用里黑屏狂奔（11-19 事故后主图
上限提到 1000，子代理经 langgraph `ensure_config` 自动继承父 config ⇒ 不拦
就是同样 1000 步 × 每次工具调用 ≈6 super-step 的墙钟黑洞）。

但 deepagents 0.7.24 **没有任何官方途径给声明式 spec 绑 config**（实测
subagents.py）：

- `SubAgent` TypedDict 字段只有 name/description/tools/model/middleware/
  interrupt_on/skills/permissions/response_format/system_prompt/mode，无
  config/recursion_limit 字段（subagents.py:84-205）；
- 声明式路径 `_compile_spec → create_sub_agent → create_agent(...)` 全程
  不 bind config（subagents.py:553-608）；
- `CompiledSubAgent` 路径能 `.with_config({"recursion_limit": N})`，但要调用方
  自备整条默认 middleware 栈（Filesystem/Summarization/Patch/Skills/缓存/
  profile），等于复刻 graph.py L824-886 的内部逻辑，升级必碎。

于是换成**行为等价**的实现：after_model 数子代理自己消息列表里的 AIMessage
条数（= 模型调用数），到上限即剥 tool_calls 逼子代理产出最终报告文本。与
deer-flow 子代理守卫同款（guard 一律不 raise，剥 tool_calls 让循环自然结束），
也与本项目 LoopDetectionMiddleware 硬停同一机制（直接复用其
`_build_hard_stop_update`/`_append_text`，剥离时连 additional_kwargs 里的
原生 tool_calls 与 finish_reason 一起清干净——这是被 a2719d7b 事故验证过的
形状）。

## 计数为什么从消息派生而不是实例计数器

同一个编译后的 gp runnable 会被并发 `task` 调用复用（模型可以在一条消息里
发多个 tool_call），实例属性计数器会互相污染；而子代理每次调用都是独立
state，消息列表长度天然是"本次调用内第几次模型调用"，零共享状态、并行安全。
注意子代理 state 是全新线程（`_validate_and_prepare_state` 用
`[HumanMessage(description)]` 重开 messages），不存在继承父历史导致一进来就
超预算的问题。

## 与主图 LoopDetection 的边界

本中间件只管**量**（总调用数封顶），不管**形**（重复/交替）；子代理内目前
没有 LoopDetection（0.7.24 自动注入的默认 gp 同样没有——`_gp_inheritable`
只继承与默认槽位同名的自定义中间件）。子代理内的空转检测列为后续立项。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from typing_extensions import override

from src.core.loop_detection import LoopDetectionMiddleware

logger = logging.getLogger(__name__)


class SubagentBudgetMiddleware(AgentMiddleware[Any, Any, Any]):
    """给子代理设模型调用数上限，到量剥 tool_calls 逼出最终报告。

    Args:
        max_model_calls: 允许的最大模型调用数。达到该数时产生的最后一条
            带 tool_calls 的 AI 会被剥成纯文本（附系统说明），子代理随即
            以这条文本作为返回报告结束。
        label: 日志/注入文案里的子代理名（默认 general-purpose）。
    """

    def __init__(self, max_model_calls: int, *, label: str = "general-purpose") -> None:
        if max_model_calls < 1:
            raise ValueError(f"max_model_calls 必须 ≥1，收到 {max_model_calls}")
        self._max = max_model_calls
        self._label = label

    # ─── 判定核心（sync/async 共用，纯 state 派生，无共享状态）───

    def _apply(self, state: dict) -> dict | None:
        messages = state.get("messages") or []
        if not messages:
            return None
        last = messages[-1]
        # 只管"还想继续调工具"的模型输出；已经在写文本 = 收尾中，不拦。
        if not isinstance(last, AIMessage) or not getattr(last, "tool_calls", None):
            return None
        # AIMessage 条数 = 本次子代理调用内的模型调用数（含这一条）。
        ai_calls = sum(1 for m in messages if isinstance(m, AIMessage))
        if ai_calls < self._max:
            return None
        tool_names = ", ".join(
            {str(tc.get("name", "?")) for tc in (last.tool_calls or [])}
        )
        logger.warning(
            "[BUDGET] 子代理 %s 模型调用达到上限 %d 次 → 剥 tool_calls 强制收尾"
            "（被拦工具: %s）",
            self._label, self._max, tool_names[:160],
        )
        content = LoopDetectionMiddleware._append_text(
            last.content,
            f"\n\n[系统] 子代理已达本次任务的模型调用上限（{self._max} 次），"
            "工具调用被禁用。请立即基于已完成的工作输出最终报告："
            "已完成内容、产物文件路径、未完成部分及原因。",
        )
        # 复用主链硬停的剥离构造（清 tool_calls/additional_kwargs/finish_reason
        # 的完整形状），保证子代理最终报告序列化为纯 assistant 文本。
        stripped = last.model_copy(
            update=LoopDetectionMiddleware._build_hard_stop_update(last, content)
        )
        return {"messages": [stripped]}

    @override
    def after_model(self, state: Any, runtime: Any) -> dict | None:
        return self._apply(state)

    @override
    async def aafter_model(self, state: Any, runtime: Any) -> dict | None:
        return self._apply(state)
