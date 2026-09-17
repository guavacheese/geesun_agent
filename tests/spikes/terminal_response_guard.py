"""Spike: 验证 TerminalResponseMiddleware 的四分支判定与端到端拦截行为。

运行环境：**生产同款镜像**（.venv 是 Linux 布局，Windows 本机 Python 用不了）：
  docker run --rm -v 'D:/workspace/geesun_agent/src:/app/src:ro' \
    -v 'D:/workspace/geesun_agent/tests:/app/tests:ro' --entrypoint sh \
    172.16.220.74:8333/geesun_ai/geesun-agent:1.0.6 \
    -c 'cd /app && /app/.venv/bin/python tests/spikes/terminal_response_guard.py'

覆盖：
  A) 纯函数 classify_empty_response 的四分支（+ tool_call 豁免、content parts 兼容）
  B) 端到端：真实 create_agent + 脚本化模型，验证
     空响应 → 删消息 + jump_to=model → 模型重跑 → 恢复成功
     （同场校验：注入的恢复提示不进入 state，即不污染历史）
  C) 端到端：两次都空 → 落盘降级文案
  D) 介入门槛与工具结果解耦：模型侧分支（thinking_truncated / thinking_only）
     无工具结果也介入；fully_empty 无工具结果仍不介入；恢复提示分场景文案
  E) hook_config 生效（jump_to 不是静默失效）+ 图上存在 after_model → model 条件边
  F) 降级信号登记表 + 诚实文案（A 方案：让 API 层跳过 M3 的"漏了下载步骤"误归因）
"""

from __future__ import annotations

import sys
from typing import Any

sys.path.insert(0, "/app")

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from src.core.terminal_response import (
    BRANCH_FULLY_EMPTY,
    BRANCH_OK,
    BRANCH_THINKING_ONLY,
    BRANCH_THINKING_TRUNCATED,
    TerminalResponseMiddleware,
    classify_empty_response,
    fallback_notice,
    pop_fallback_signal,
    quick_mode_notice,
)

PASS = 0
FAIL = 0


def check(label: str, actual: Any, expected: Any) -> None:
    global PASS, FAIL
    ok = actual == expected
    if ok:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}\n      期望: {expected!r}\n      实际: {actual!r}")


def check_true(label: str, cond: bool) -> None:
    check(label, bool(cond), True)


# ─────────────────────────────────────────────────────────────
# A) 纯函数四分支判定
# ─────────────────────────────────────────────────────────────
def scenario_a() -> None:
    print("\n=== A) classify_empty_response 四分支 ===")

    # ① 正常
    check(
        "① 有正文 → ok",
        classify_empty_response(AIMessage(content="报告已生成。", id="a1")),
        BRANCH_OK,
    )

    # ② thinking 截断（本次生产故障形态）
    check(
        "② content空 + reasoning非空 + finish_reason=length → thinking_truncated",
        classify_empty_response(
            AIMessage(
                content="",
                additional_kwargs={"reasoning_content": "让我先看看这份 PDF…" * 5},
                response_metadata={"finish_reason": "length"},
                id="a2",
            )
        ),
        BRANCH_THINKING_TRUNCATED,
    )

    # ③ 思考完没说
    check(
        "③ content空 + reasoning非空 + finish_reason=stop → thinking_only",
        classify_empty_response(
            AIMessage(
                content="",
                additional_kwargs={"reasoning_content": "我想完了"},
                response_metadata={"finish_reason": "stop"},
                id="a3",
            )
        ),
        BRANCH_THINKING_ONLY,
    )

    # ④ 彻底空
    check(
        "④ content空 + reasoning空 → fully_empty",
        classify_empty_response(AIMessage(content="", id="a4")),
        BRANCH_FULLY_EMPTY,
    )

    # tool_call 豁免（有 tool_calls）
    check(
        "有 tool_calls → ok（交给工具路由）",
        classify_empty_response(
            AIMessage(
                content="",
                tool_calls=[{"name": "t", "args": {}, "id": "c1", "type": "tool_call"}],
                id="a5",
            )
        ),
        BRANCH_OK,
    )

    # tool_call 豁免（finish_reason 是 tool_calls 但结构里没有）
    check(
        "finish_reason=tool_calls → ok",
        classify_empty_response(
            AIMessage(content="", response_metadata={"finish_reason": "tool_calls"}, id="a6")
        ),
        BRANCH_OK,
    )

    # 畸形工具调用豁免
    check(
        "invalid_tool_calls → ok",
        classify_empty_response(
            AIMessage(content="", invalid_tool_calls=[{"name": "t"}], id="a7")
        ),
        BRANCH_OK,
    )

    # list 形态 content（content parts）
    check(
        "content parts 里有文本 → ok",
        classify_empty_response(
            AIMessage(
                content=[{"type": "text", "text": "正文"}],
                id="a8",
            )
        ),
        BRANCH_OK,
    )
    check(
        "content parts 全空 → fully_empty",
        classify_empty_response(
            AIMessage(content=[{"type": "text", "text": "   "}], id="a9")
        ),
        BRANCH_FULLY_EMPTY,
    )

    # reasoning 用 list 形态
    check(
        "reasoning 为 list[dict] 也能识别 → thinking_only",
        classify_empty_response(
            AIMessage(
                content="",
                additional_kwargs={"reasoning_content": [{"text": "思考中"}]},
                response_metadata={"finish_reason": "stop"},
                id="a10",
            )
        ),
        BRANCH_THINKING_ONLY,
    )

    # 兼容 reasoning 键名
    check(
        "additional_kwargs['reasoning'] 键也识别 → thinking_truncated",
        classify_empty_response(
            AIMessage(
                content="",
                additional_kwargs={"reasoning": "thinking..."},
                response_metadata={"finish_reason": "length"},
                id="a11",
            )
        ),
        BRANCH_THINKING_TRUNCATED,
    )
    print(f"  —— A 段完成 ——")


# ─────────────────────────────────────────────────────────────
# 脚本化模型（模拟四种收尾形态）
# ─────────────────────────────────────────────────────────────
class ScriptedModel(BaseChatModel):
    replies: list[AIMessage] = []
    cursor: int = 0
    seen: list[list[str]] = []
    #: 每次 _generate 收到的 kwargs（含 factory 经 bind/bind_tools 透传的 model_settings）
    seen_kwargs: list[dict] = []
    #: 最近一次 bind_tools 透传的 kwargs —— 生产用的 ChatOpenAI 会把它们 bind 进请求，
    #: 此处如实留存，供断言「关思考参数（extra_body）是否真的下达」。**每次覆盖而非累积**，
    #: 才能反映"本轮"的绑定参数（累积会让第 3 轮关过思考后，第 4 轮仍看到 extra_body）。
    bound_kwargs: dict = {}

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append([type(m).__name__ for m in messages])
        self.seen_kwargs.append({**dict(self.bound_kwargs), **kwargs})
        idx = min(self.cursor, len(self.replies) - 1)
        object.__setattr__(self, "cursor", self.cursor + 1)
        return ChatResult(generations=[ChatGeneration(message=self.replies[idx])])

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        object.__setattr__(self, "bound_kwargs", dict(kwargs))
        return self


@tool
def ping(x: str) -> str:
    """Echo tool."""
    return f"pong:{x}"


def empty_ai(msg_id: str, finish: str = "length") -> AIMessage:
    return AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "很长的思考过程" * 10},
        response_metadata={"finish_reason": finish, "model_name": "Qwen3.6-35B-A3B"},
        id=msg_id,
    )


def _seed_with_tool_result(agent, model) -> dict:
    """构造「本轮已有工具结果」的初始 state。"""
    return {
        "messages": [
            HumanMessage(content="分析这份 PDF", id="h1"),
            AIMessage(
                content="我先读文件",
                tool_calls=[{"name": "ping", "args": {"x": "A"}, "id": "tc1", "type": "tool_call"}],
                id="ai_tc",
            ),
            ToolMessage(content="pong:A", tool_call_id="tc1", id="tm1"),
        ]
    }


# ─────────────────────────────────────────────────────────────
# B) 端到端：空 → 重试 → 恢复
# ─────────────────────────────────────────────────────────────
def scenario_b() -> None:
    print("\n=== B) 端到端：空响应 → 删消息并重试 → 恢复成功 ===")
    model = ScriptedModel(
        replies=[
            empty_ai("empty1", "length"),          # 第 1 次：空
            AIMessage(content="根据工具结果，结论是 X。", id="ok1"),  # 第 2 次：恢复
        ]
    )
    mw = TerminalResponseMiddleware()
    agent = create_agent(model=model, tools=[ping], middleware=[mw])

    result = agent.invoke(_seed_with_tool_result(agent, model))
    msgs = result["messages"]

    check("模型被调用 2 次（触发了一次重试）", model.cursor, 2)
    check_true(
        "空消息 id 已从历史中移除",
        "empty1" not in [getattr(m, "id", None) for m in msgs],
    )
    check("末条消息为恢复后的正文", msgs[-1].content, "根据工具结果，结论是 X。")

    # 关键：注入的恢复提示必须在第 2 次模型调用时可见
    check_true(
        "第 2 次模型调用看到了恢复提示",
        len(model.seen) >= 2 and len(model.seen[1]) > len(model.seen[0]),
    )

    # 关键：注入的提示不能进入 state（不污染历史）
    injected_in_state = [
        m for m in msgs
        if (getattr(m, "additional_kwargs", None) or {}).get("hide_from_ui")
    ]
    check("注入的恢复提示未进入 state（不污染历史）", len(injected_in_state), 0)

    stats = mw.stats()
    check("统计记到 thinking_truncated 分支", stats.get(BRANCH_THINKING_TRUNCATED), 1)


# ─────────────────────────────────────────────────────────────
# C) 端到端：两次都空 → 降级文案
# ─────────────────────────────────────────────────────────────
def scenario_c() -> None:
    print("\n=== C) 端到端：重试后仍空 → 落盘降级文案 ===")
    # max_retries=2（2026-09-17 方案 A 起）：两次重试都试过才落降级文案。
    # 注意第 1 次重试是无损的（仅注入提示），第 2 次才关思考 —— 两者都救不回来才降级。
    model = ScriptedModel(replies=[
        empty_ai("e1", "length"), empty_ai("e2", "length"), empty_ai("e3", "length"),
    ])
    mw = TerminalResponseMiddleware()
    agent = create_agent(model=model, tools=[ping], middleware=[mw])

    result = agent.invoke(_seed_with_tool_result(agent, model))
    last = result["messages"][-1]

    check("模型被调用 3 次（首轮 + 2 次重试用尽）", model.cursor, 3)
    check_true("末条是降级文案（非空）", bool(str(last.content).strip()))
    check_true("降级文案提到「已自动重试」", "自动重试" in str(last.content))
    check(
        "打了 terminal_response_fallback 标记",
        (last.additional_kwargs or {}).get("terminal_response_fallback"),
        True,
    )
    check(
        "标记了触发的分支",
        (last.additional_kwargs or {}).get("terminal_response_branch"),
        BRANCH_THINKING_TRUNCATED,
    )
    check_true("tool_calls 已清空（序列化为纯文本）", not last.tool_calls)


# ─────────────────────────────────────────────────────────────
# D) 介入门槛：模型侧分支与工具结果解耦
# ─────────────────────────────────────────────────────────────
def scenario_d() -> None:
    """2026-09-17 改写。原断言「本轮无工具结果 → 不介入（交给 M3）」**正是漏洞本身**：
    生产会话 e9c77ce9 第二轮就是这个形态（纯对话追问、本轮零工具调用），
    空消息被放过原样落库 → 用户看到空白气泡 + 界面"突然停止" + 连状态条都没有。

    新语义：模型侧分支（thinking_truncated / thinking_only）与工具状态正交 → 必须介入；
    其余分支（含 fully_empty）保持 deer-flow 语义，无工具结果时不介入。
    """
    print("\n=== D) 介入门槛与工具结果解耦（e9c77ce9 第二轮回归位）===")

    # D1 无工具结果 + thinking_truncated → 介入并重试成功
    model = ScriptedModel(replies=[
        empty_ai("e1", "length"),
        AIMessage(content="图中确有 8 个纯蓝色小点组成矩形框。", id="ok1"),
    ])
    mw = TerminalResponseMiddleware()
    agent = create_agent(model=model, tools=[ping], middleware=[mw])
    result = agent.invoke(
        {"messages": [HumanMessage(content="描述的不准确，应该是8个纯蓝色实心小点组成的矩形框", id="h1")]}
    )
    ids = [getattr(m, "id", None) for m in result["messages"]]
    check("D1 模型被调用 2 次（空响应触发了重试）", model.cursor, 2)
    check_true("D1 空消息已删除（未落库成空白气泡）", "e1" not in ids)
    check_true("D1 恢复后的正文进了 state", "ok1" in ids)
    check("D1 命中 thinking_truncated 分支", mw.stats(), {BRANCH_THINKING_TRUNCATED: 1})

    # D2 无工具结果 + thinking_only(stop) → 同样介入
    model2 = ScriptedModel(replies=[
        empty_ai("e2", "stop"),
        AIMessage(content="直接给结论。", id="ok2"),
    ])
    mw2 = TerminalResponseMiddleware()
    agent2 = create_agent(model=model2, tools=[ping], middleware=[mw2])
    result2 = agent2.invoke({"messages": [HumanMessage(content="你好", id="h1")]})
    check("D2 模型被调用 2 次（thinking_only 也介入）", model2.cursor, 2)
    check_true("D2 空消息已删除", "e2" not in [getattr(m, "id", None) for m in result2["messages"]])

    # D3 无工具结果 + fully_empty → 仍不介入（保持 deer-flow 语义，防误伤内部调用）
    blank = AIMessage(
        content="", additional_kwargs={},
        response_metadata={"finish_reason": "stop"}, id="e3",
    )
    model3 = ScriptedModel(replies=[blank])
    mw3 = TerminalResponseMiddleware()
    agent3 = create_agent(model=model3, tools=[ping], middleware=[mw3])
    result3 = agent3.invoke({"messages": [HumanMessage(content="你好", id="h1")]})
    check("D3 fully_empty 无工具结果 → 仍只调用 1 次", model3.cursor, 1)
    check_true("D3 消息未被删（未介入）",
               "e3" in [getattr(m, "id", None) for m in result3["messages"]])
    check("D3 统计为空（未命中任何分支）", mw3.stats(), {})

    # D4 恢复提示分场景：无工具结果版不得出现「工具结果已在对话中给出」这个错误前提
    from src.core.terminal_response import _recovery_prompt_for

    p_tool = _recovery_prompt_for(BRANCH_THINKING_TRUNCATED, True)
    p_notool = _recovery_prompt_for(BRANCH_THINKING_TRUNCATED, False)
    check_true("D4 有工具结果版含『工具结果已在对话中给出』", "工具结果已在对话中给出" in p_tool)
    check_true("D4 无工具结果版不含该错误前提", "工具结果已在对话中给出" not in p_notool)
    check_true("D4 无工具结果版强调思考简短", "思考过程务必保持简短" in p_notool)
    check_true("D4 无工具结果版保留一次工具出口", "最多再调用一次工具" in p_notool)


# ─────────────────────────────────────────────────────────────
# E) hook_config 生效验证
# ─────────────────────────────────────────────────────────────
def scenario_e() -> None:
    print("\n=== E) hook_config(can_jump_to) 生效验证 ===")
    from langchain.agents.factory import _get_can_jump_to

    mw = TerminalResponseMiddleware()
    check(
        "sync after_model 的 can_jump_to",
        _get_can_jump_to(mw, "after_model"),
        ["model"],
    )
    check_true(
        "async aafter_model 也带 __can_jump_to__（防 astream 静默失效）",
        hasattr(TerminalResponseMiddleware.aafter_model, "__can_jump_to__"),
    )

    # 边结构必须含条件边到 model
    model = ScriptedModel(replies=[AIMessage(content="ok", id="x1")])
    agent = create_agent(model=model, tools=[ping], middleware=[TerminalResponseMiddleware()])
    edge_targets = {(e.source, e.target) for e in agent.get_graph().edges}
    check_true(
        "图中存在 after_model -> model 的条件边",
        any("TerminalResponse" in s and t == "model" for s, t in edge_targets),
    )
    print(f"    边: {sorted(f'{s}->{t}' for s, t in edge_targets if 'Terminal' in s or 'model' in s)}")


# ─────────────────────────────────────────────────────────────
# F) 降级信号登记表 + 面向用户的诚实文案（2026-09-16 新增，A 方案回归位）
# ─────────────────────────────────────────────────────────────
def scenario_f() -> None:
    """回归会话 de18ad37 的误归因链：守卫落降级文案时**必须**留下可被 API 层消费的信号，
    且上报文案只讲模型层事实，不再出现「是不是漏了 download_from_sandbox」这类推测。

    反向验证：若把 _record_fallback_signal 那一行删掉，F2/F3 立刻 FAIL。

    生命周期契约（由 chat.py 保证，本 spike 只测登记表语义）：
      开轮 `pop_fallback_signal(thread_id)` 清残留 → 轮内登记 → 轮末消费；
      断连兜底路径（finally）也会取走，避免残留信号被下一轮误当自己的。
    """
    print("\n=== F) 降级信号 + 诚实文案（A 方案回归位）===")

    THREAD = "spike:fallback"
    check("F1 未降级时取不到信号", pop_fallback_signal(THREAD), None)

    model = ScriptedModel(replies=[
        empty_ai("f1", "length"), empty_ai("f2", "length"), empty_ai("f3", "length"),
    ])
    mw = TerminalResponseMiddleware()
    agent = create_agent(model=model, tools=[ping], middleware=[mw])
    result = agent.invoke(
        _seed_with_tool_result(agent, model),
        config={"configurable": {"thread_id": THREAD}},
    )
    check("F2 三次都空 → 预算用尽", model.cursor, 3)
    check_true(
        "F3 末条为降级文案",
        "没有产出可用的回复内容" in str(result["messages"][-1].content),
    )

    sig = pop_fallback_signal(THREAD)
    check_true("F4 降级后登记了信号", isinstance(sig, dict))
    check("F5 信号 branch", (sig or {}).get("branch"), BRANCH_THINKING_TRUNCATED)
    check("F6 信号 finish_reason", (sig or {}).get("finish_reason"), "length")
    check("F7 信号 retries", (sig or {}).get("retries"), 2)
    check_true("F8 信号含 reasoning_len", int((sig or {}).get("reasoning_len") or 0) > 0)
    check("F9 取走即清（不重复上报）", pop_fallback_signal(THREAD), None)
    check("F10 未降级的 thread 仍为空", pop_fallback_signal("spike:other"), None)

    reason, hint = fallback_notice(BRANCH_THINKING_TRUNCATED, 1)
    check_true("F11 reason 说清截断形态", "输出上限" in reason and "finish_reason=length" in reason)
    check_true("F12 reason 带重试次数", "已自动重试 1 次" in reason)
    check_true("F13 hint 给可执行动作", "拆小" in hint)
    for label, blob in (("F14 reason", reason), ("F15 hint", hint)):
        check_true(
            f"{label} 不含'漏了下载步骤'式推测",
            ("download_from_sandbox" not in blob) and ("漏" not in blob),
        )

    # 各分支文案必须互不相同（否则又变成"一句话盖所有情况"）
    notices = {
        b: fallback_notice(b, 1)[0]
        for b in (BRANCH_THINKING_TRUNCATED, BRANCH_THINKING_ONLY, BRANCH_FULLY_EMPTY)
    }
    check("F16 三个非 OK 分支都有专属文案", len(set(notices.values())), 3)
    check(
        "F17 未知 branch 退化为 fully_empty 文案",
        fallback_notice("nonsense", 0)[0],
        fallback_notice(BRANCH_FULLY_EMPTY, 0)[0],
    )
    check_true(
        "F18 retries=0 时不谎称重试过",
        "重试" not in fallback_notice(BRANCH_THINKING_ONLY, 0)[0],
    )
    print(f"    reason: {reason}")
    print(f"    hint  : {hint}")


# ─────────────────────────────────────────────────────────────
# G) 方案 A：重试顺序（无损 → 有损）+ 快速模式标记（2026-09-17 新增）
# ─────────────────────────────────────────────────────────────
def scenario_g() -> None:
    """验证「有损的那一招只在最后一次出手」这条顺序约束，在真实 chain 上成立。

    方案 A 的两个子目标：
      ① **顺序**：第 1 次重试无损（只注入恢复提示，模型仍在思考模式，推理能力不受损）；
                 第 2 次重试有损（关闭思考 `enable_thinking=false` 兜底）。
         ——「省 token」不等于「该先上」：无损的先跑，救不回来再上有损的。
         生产实证支持这个顺序：恢复提示在 8000 / 65536 预算下都能成功
         （e9c77ce9 第一轮「6 秒出正文」），只有 2000 紧预算才失败。
      ② **留痕**：关思考的产出与正常回复**外观完全一致**，必须登记「快速模式」信号
                （供 API 层提示用户），否则用户会把快速模式的结果当完整答案用
                —— 隐性失败比空白气泡（显性失败）更危险。

    反向验证：把 `_should_disable_thinking` 改成恒 False ⇒ G12 立刻 FAIL；
              删掉 `_record_quick_mode_signal` 那一行 ⇒ G14 立刻 FAIL。
    """
    print("\n=== G) 方案 A：重试顺序（无损 → 有损）与快速模式标记 ===")
    from src.core.terminal_response import _should_disable_thinking

    T, O, F = BRANCH_THINKING_TRUNCATED, BRANCH_THINKING_ONLY, BRANCH_FULLY_EMPTY

    # ── G1-G8 纯策略矩阵 ──
    check("G1 截断分支 第1/2次 → 不关思考（无损）", _should_disable_thinking(T, 1, 2), False)
    check("G2 截断分支 第2/2次 → 关思考（有损兜底）", _should_disable_thinking(T, 2, 2), True)
    check("G3 截断分支 第1/3次 → 不关", _should_disable_thinking(T, 1, 3), False)
    check("G4 截断分支 第2/3次 → 不关", _should_disable_thinking(T, 2, 3), False)
    check("G5 截断分支 第3/3次 → 关", _should_disable_thinking(T, 3, 3), True)
    check(
        "G6 预算为 1 时唯一那次直接关（预算不足的退化语义，非死代码）",
        _should_disable_thinking(T, 1, 1), True,
    )
    check(
        "G7 thinking_only 永不关（思考是正常收尾的，关掉只损不益）",
        _should_disable_thinking(O, 5, 5), False,
    )
    check("G8 fully_empty 永不关", _should_disable_thinking(F, 5, 5), False)

    # ── G9-G13 端到端：首轮空 → 重试①空 → 重试②出正文 ──
    model = ScriptedModel(replies=[
        empty_ai("g1", "length"),
        empty_ai("g2", "length"),
        AIMessage(content="快速模式产出的正文。", id="g_ok"),
    ])
    mw = TerminalResponseMiddleware()          # 默认 max_retries=2
    agent = create_agent(model=model, tools=[ping], middleware=[mw])
    THREAD = "spike:quickmode"
    result = agent.invoke(
        _seed_with_tool_result(agent, model),
        config={"configurable": {"thread_id": THREAD}},
    )

    check("G9 模型被调用 3 次（首轮 + 2 次重试）", model.cursor, 3)
    k = model.seen_kwargs
    check("G10 首轮不带 extra_body", (k[0] if k else {}).get("extra_body"), None)
    check(
        "G11 重试①（无损）也不带 extra_body（模型仍在思考模式）",
        (k[1] if len(k) > 1 else {}).get("extra_body"), None,
    )
    check(
        "G12 重试②（有损）带 enable_thinking=false",
        ((k[2] if len(k) > 2 else {}).get("extra_body") or {}).get("chat_template_kwargs"),
        {"enable_thinking": False},
    )
    check_true(
        "G13 重试①②都注入了恢复提示（提示与关思考是两个正交动作）",
        len(model.seen) >= 3 and all(len(model.seen[i]) > 3 for i in (1, 2)),
    )

    # ── G14-G18 快速模式信号与结构化标记 ──
    sig = pop_fallback_signal(THREAD)
    check_true("G14 登记了快速模式信号", bool((sig or {}).get("quick_mode")))
    check("G15 信号记录了是第几次重试", (sig or {}).get("attempt"), 2)
    check("G16 取走即清（不重复上报）", pop_fallback_signal(THREAD), None)

    last = result["messages"][-1]
    check(
        "G17 正文打了 terminal_response_quick_mode 标记（供前端识别）",
        (last.additional_kwargs or {}).get("terminal_response_quick_mode"),
        True,
    )
    check(
        "G18 正文未被重复追加（model_copy 按 id 覆盖而非新增）",
        sum(1 for m in result["messages"] if getattr(m, "id", None) == "g_ok"),
        1,
    )

    # ── G19-G21 快速模式文案 ──
    reason_qm, _hint_qm = quick_mode_notice(2)
    check_true(
        "G19 文案说清「关闭了深度思考」",
        "快速模式" in reason_qm and "关闭深度思考" in reason_qm,
    )
    check_true(
        "G20 文案明确提示深度可能不足（防用户误当完整答案）",
        "可能不如常规模式完整" in reason_qm,
    )
    check_true("G21 文案不谎称失败（它是成功产出）", "没有产出" not in reason_qm)
    print(f"    quick_mode reason: {reason_qm}")

    # ── G22-G23 反向：没关思考的普通成功产出不得误标 ──
    model2 = ScriptedModel(replies=[
        empty_ai("h1", "length"),
        AIMessage(content="常规模式恢复的正文。", id="h_ok"),
    ])
    mw2 = TerminalResponseMiddleware()
    agent2 = create_agent(model=model2, tools=[ping], middleware=[mw2])
    THREAD2 = "spike:normalmode"
    r2 = agent2.invoke(
        _seed_with_tool_result(agent2, model2),
        config={"configurable": {"thread_id": THREAD2}},
    )
    check("G22 第 1 次重试就成功 → 无快速模式信号", pop_fallback_signal(THREAD2), None)
    check(
        "G23 该正文没有快速模式标记（不误标）",
        (r2["messages"][-1].additional_kwargs or {}).get("terminal_response_quick_mode"),
        None,
    )


if __name__ == "__main__":
    print("=" * 74)
    print("TerminalResponseMiddleware spike")
    print("=" * 74)
    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    scenario_f()
    scenario_g()

    print("\n" + "=" * 74)
    print(f"结果: {PASS} PASS / {FAIL} FAIL")
    print("=" * 74)
    sys.exit(1 if FAIL else 0)
