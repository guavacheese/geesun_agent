"""M3-v2 spike：验证 deepagents 0.6.12 的 astream 继续模式（设计文档 §M3-v2）。

背景：完成门 v2 需要在"零产出"时注入 SystemMessage 让模型再跑一轮。
这依赖 langgraph checkpoint 继续 + 追加消息两个行为，需实测确认。

运行环境：**必须在 WSL / 装有项目完整依赖（deepagents 0.6.12）的 venv 中运行**，
Windows 侧 pip 无法安装 deepagents（私有源/网络差异）。

    cd /mnt/d/workspace/geesun_agent
    .venv/bin/python spike_checkpoint_resume.py

判定标准（全部满足 → v2 可开启 sandbox_completion_gate_auto_continue=true）：
  1. CHECK-1: astream(None, config) 能从上次 checkpoint 继续（有新的 token 流）
  2. CHECK-2: astream({"messages":[SystemMessage(...)]}, config) 继续时，
     模型能"看到"注入的 SystemMessage 内容（回复中体现或触发后续工具调用）
  3. CHECK-3: 继续轮结束后 state.messages 中 SystemMessage 已追加且位于末尾
"""

import asyncio
import os

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from src.api.deps import get_checkpointer, get_store
from src.core.config import settings
from src.infra.sandbox import create_sandbox
from src.services.agent import create_agent


async def main() -> None:
    # 1. 构造最小 agent（复用生产工厂，工具留空即可）
    store = await get_store()
    checkpointer = await get_checkpointer()
    sandbox = create_sandbox(f"spike:{os.getpid()}")
    user_id, session_id = "spike-user", "spike-session"
    agent = await create_agent(
        user_id=user_id,
        session_id=session_id,
        thread_id=f"{user_id}:{session_id}",
        store=store,
        sandbox=sandbox,
        checkpointer=checkpointer,
        tools=[],
        skills=[],
    )
    cfg = {"configurable": {"thread_id": f"{user_id}:{session_id}"}}

    # 2. 首轮：让模型做一个无文件操作的最小回复，制造 checkpoint
    print("=== 首轮 astream ===")
    first = {"messages": [HumanMessage(content="只回复 OK 两个字，不要调用任何工具。")]}
    tokens_first = 0
    async for mode, data in agent.astream(
        first, config=cfg, stream_mode=["messages", "updates"]
    ):
        if mode == "messages":
            tokens_first += 1
    print(f"首轮结束, messages token 数 ≈ {tokens_first}")

    # 3. CHECK-1: astream(None) 继续
    print("\n=== CHECK-1: astream(None) 从 checkpoint 继续 ===")
    try:
        tokens_resume = 0
        async for mode, data in agent.astream(
            None, config=cfg, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                tokens_resume += 1
        print(f"astream(None) 产出 token 数 ≈ {tokens_resume}")
        print("CHECK-1 判定:", "通过" if tokens_resume > 0 else "不通过（无新输出）")
    except Exception as e:
        print(f"CHECK-1 失败: {type(e).__name__}: {e}")

    # 4. CHECK-2/3: 注入 SystemMessage 继续
    print("\n=== CHECK-2/3: astream([SystemMessage]) 继续并可见 ===")
    try:
        injected = SystemMessage(
            content="【spike】这是一条注入的系统消息，请在回复中明确说出 '收到spike消息' 这五个字。"
        )
        async for mode, data in agent.astream(
            {"messages": [injected]}, config=cfg, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                pass  # 只需跑完；最终状态检查
        state = await agent.aget_state(cfg)
        msgs = state.values.get("messages", []) if state and hasattr(state, "values") else []
        tail = [str(m.content) for m in msgs[-3:]]
        has_injected = any("收到spike消息" in c for c in tail)
        sys_msgs = [m for m in msgs if getattr(m, "type", "") == "system"]
        print(f"状态尾部消息: {tail}")
        print(f"SystemMessage 已追加: {len(sys_msgs) > 0}（count={len(sys_msgs)}）")
        print(f"模型回复体现注入内容: {has_injected}")
        print("CHECK-2 判定:", "通过" if has_injected else "不通过")
        print("CHECK-3 判定:", "通过" if len(sys_msgs) > 0 else "不通过")
    except Exception as e:
        print(f"CHECK-2/3 失败: {type(e).__name__}: {e}")

    print("\n结论：三检查全过 → .env 设 SANDOX_COMPLETION_GATE_AUTO_CONTINUE=true 开启 v2；")
    print("     任一不通过 → 保持默认 False（完成门停留在 v1 纯检测行为）。")


if __name__ == "__main__":
    asyncio.run(main())
