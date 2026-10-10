"""生产装配路径验证：create_deep_agent 上「工具契约反查」是否真的拿到真契约。

为什么必须单独验这一条：
    E（2026-10-10）的机制前提是「``wrap_model_call`` 收到的 ``ModelRequest.tools``
    就是 agent 实际持有的工具全集，且其 ``args_schema`` 可解析」。上面的单测是直接
    构造假工具对象喂给 ``_learn_tool_schemas`` —— 那验证的是"给我一个工具我能解析"，
    **不是**"生产链路会不会把工具给我"。
    deepagents 会自己追加一串 middleware（TodoList / Filesystem / Skills /
    Summarization…），工具集是它组装后才进 ModelRequest 的；完全可能出现
    tools 为空、工具被包成别的形态、或 args_schema 被替换等情况。
    aaf95335 的教训正是"验证要用生产实际跑的那条装配路径"。

判据：
    1. create_deep_agent 真实跑一轮后，middleware 缓存里出现 read_file
    2. read_file 的契约解析结果为 deepagents 真实签名 file_path/offset/limit
    3. 端到端正向：连续读 6 个**不同**文件 → 不得硬停（旧实现下第 5 次必然硬停）
    4. 端到端反向：连续读**同一**文件同一段 → 必须硬停（检测能力没被一起废掉）

运行（容器内，见 deep_agent_loop_marker.py 同款方式）：
    docker run --rm -v D:/workspace/geesun_agent:/mnt -w /mnt \
      -e PYTHONPATH=/mnt/.testdeps --entrypoint /app/.venv/bin/python \
      172.16.220.74:8333/geesun_ai/geesun-agent:1.0.25 \
      tests/spikes/deep_agent_tool_contract.py
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from src.core.loop_detection import (
    LOOP_FORCED_STOP_KEY,
    _PATH_FIELD_CANDIDATES,
    _resolve_field,
    _SPAN_FIELD_CANDIDATES,
    _START_FIELD_CANDIDATES,
    LoopDetectionMiddleware,
)

_ids = itertools.count(1)
FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


class _ReadFileModel(BaseChatModel):
    """按预设路径序列反复调用 read_file，用完则收尾出纯文本。"""

    paths: list[str] = []
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "deep-contract-fake"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ANN001, ANN003
        self.calls += 1
        if self.calls <= len(self.paths):
            msg = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {
                            "file_path": self.paths[self.calls - 1],
                            "offset": 0,
                            "limit": 100,
                        },
                        "id": f"call_{next(_ids)}",
                    }
                ],
            )
        else:
            msg = AIMessage(content="已用现有结果作答。")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _run(model: _ReadFileModel, mw: LoopDetectionMiddleware, thread_id: str):
    """跑一轮，返回 (是否硬停, 硬化标记)。"""
    from deepagents import create_deep_agent

    agent = create_deep_agent(
        model=model,
        middleware=[mw],
        system_prompt="你是测试用 agent。",
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
    stopped = False
    for _mode, data in agent.stream(
        {"messages": [HumanMessage("开始")]}, config=config, stream_mode=["updates"]
    ):
        if _mode != "updates":
            continue
        for _node, out in data.items():
            if isinstance(out, dict) and out.get(LOOP_FORCED_STOP_KEY):
                stopped = True
    return stopped, agent.get_state(config).values.get(LOOP_FORCED_STOP_KEY)


def main() -> int:
    mw = LoopDetectionMiddleware(
        warn_threshold=3, hard_limit=5, tool_freq_warn=99, tool_freq_hard_limit=99
    )

    print("=== 3) 端到端正向：连续读 6 个不同文件（旧实现下第 5 次必然硬停）===")
    model = _ReadFileModel(paths=[f"/reports/f{i}.py" for i in range(6)])
    stopped, marker = _run(model, mw, "contract-diff-files")
    check("读 6 个不同文件未被判循环", not stopped, f"marker={marker}")

    print("=== 1) 契约是否在真实装配路径上被学到 ===")
    check(
        "middleware 缓存里出现 read_file",
        "read_file" in mw._tool_fields,
        f"已学 {len(mw._tool_fields)} 个工具: {sorted(mw._tool_fields)[:8]}",
    )
    fields = mw._schema_fields_for("read_file")
    check("read_file 契约已解析（非 None）", fields is not None, f"fields={sorted(fields) if fields else None}")

    print("=== 2) 解析结果是否等于 deepagents 真实签名 ===")
    if fields:
        path_field = _resolve_field({}, fields, _PATH_FIELD_CANDIDATES)
        start_field = _resolve_field({}, fields, _START_FIELD_CANDIDATES)
        span_field = _resolve_field({}, fields, _SPAN_FIELD_CANDIDATES)
        check("路径槽位 = file_path", path_field == "file_path", f"实际 {path_field}")
        check("起始槽位 = offset", start_field == "offset", f"实际 {start_field}")
        check("跨度槽位 = limit", span_field == "limit", f"实际 {span_field}")

    print("=== 4) 端到端反向：连续读同一文件同一段（必须仍被硬停）===")
    mw2 = LoopDetectionMiddleware(
        warn_threshold=3, hard_limit=5, tool_freq_warn=99, tool_freq_hard_limit=99
    )
    model2 = _ReadFileModel(paths=["/reports/same.py"] * 6)
    stopped2, marker2 = _run(model2, mw2, "contract-same-file")
    check("真重复读被硬停", stopped2, f"marker={marker2}")

    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败: {FAILS}")
        return 1
    print("✅ 生产装配路径（create_deep_agent）四项判据全通")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
