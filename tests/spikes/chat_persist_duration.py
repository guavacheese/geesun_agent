"""Spike: 验证 chat.py 持久化时长字段逻辑（2026-09-08 加 reasoning_duration_ms / turn_duration_ms）。

不依赖 pytest，直接 python tests/spikes/chat_persist_duration.py 运行。

复刻 _persist_session 内 entry 构造的核心逻辑（与 src/api/endpoints/chat.py 同步更新），
跑一组（输入 → 期望 entry）对照表，全部通过 → 持久化字段写入逻辑正确。

如果 chat.py 改了 entry 构造逻辑，本脚本也要同步改；失败时说明两边失同步。
"""

from __future__ import annotations

from datetime import datetime, timezone


def build_entry(
    msg_id: str | None,
    role: str,
    content: str,
    reasoning: str,
    tool_calls: list | None,
    reasoning_started_at_ms: int | None,
    reasoning_ended_at_ms: int | None,
) -> dict:
    """复刻 chat.py _persist_session 内的 entry 构造逻辑（2026-09-08 版）。

    只复刻 reasoning_duration_ms / reasoning_started_at 写入部分；其他字段省略。
    """
    entry: dict = {
        "id": msg_id,
        "role": role,
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if reasoning:
        entry["reasoning"] = reasoning
    if role == "ai" and tool_calls:
        entry["tool_calls"] = tool_calls

    # 持久化时长字段（与 chat.py:515-527 同步）
    if (
        role == "ai"
        and reasoning
        and reasoning_started_at_ms is not None
        and reasoning_ended_at_ms is not None
    ):
        entry["reasoning_duration_ms"] = max(
            0, reasoning_ended_at_ms - reasoning_started_at_ms
        )
        entry["reasoning_started_at"] = datetime.fromtimestamp(
            reasoning_started_at_ms / 1000, tz=timezone.utc
        ).isoformat()
    return entry


def append_turn_duration(history: list[dict], turn_started_at_ms: int, turn_ended_at_ms: int) -> None:
    """复刻 chat.py turn_duration_ms 写入：写到 history 最后一条 AI 消息。"""
    if turn_started_at_ms is None or turn_ended_at_ms is None:
        return
    turn_duration_ms = max(0, turn_ended_at_ms - turn_started_at_ms)
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "ai":
            history[i]["turn_duration_ms"] = turn_duration_ms
            break


# ─── 对照表 ───
cases = [
    # 1. AI 消息带 reasoning + 两个时间戳都有 → 写入 reasoning_duration_ms 和 started_at
    {
        "name": "AI 带 reasoning + 完整时间戳",
        "input": dict(
            msg_id="ai-1", role="ai", content="答", reasoning="思考内容",
            tool_calls=None,
            reasoning_started_at_ms=1000, reasoning_ended_at_ms=3500,
        ),
        "expect": {
            "id": "ai-1", "role": "ai", "content": "答",
            "reasoning": "思考内容",
            "reasoning_duration_ms": 2500,  # 3500 - 1000
            "reasoning_started_at": datetime.fromtimestamp(1.0, tz=timezone.utc).isoformat(),
        },
    },
    # 2. AI 消息无 reasoning → 不写 reasoning_duration_ms（即便有时间戳）
    {
        "name": "AI 无 reasoning → 不写时长字段",
        "input": dict(
            msg_id="ai-2", role="ai", content="答", reasoning="",
            tool_calls=None,
            reasoning_started_at_ms=1000, reasoning_ended_at_ms=3500,
        ),
        "expect": {
            "id": "ai-2", "role": "ai", "content": "答",
            # 注意：reasoning 为空串 → 不进 if reasoning → entry 不含 reasoning 键
            # 同时不进 reasoning_duration 写入分支
        },
    },
    # 3. AI 消息有 reasoning 但只有 started_at → 不写 duration
    {
        "name": "reasoning 存在但 ended_at 为 None → 不写 duration",
        "input": dict(
            msg_id="ai-3", role="ai", content="答", reasoning="思考",
            tool_calls=None,
            reasoning_started_at_ms=1000, reasoning_ended_at_ms=None,
        ),
        "expect": {
            "id": "ai-3", "role": "ai", "content": "答",
            "reasoning": "思考",
            # 不写 reasoning_duration_ms
        },
    },
    # 4. user 消息带 reasoning_content（异常数据）→ 不写 duration
    {
        "name": "user 消息不进 reasoning_duration 分支",
        "input": dict(
            msg_id="u-1", role="user", content="问", reasoning="",
            tool_calls=None,
            reasoning_started_at_ms=1000, reasoning_ended_at_ms=3500,
        ),
        "expect": {
            "id": "u-1", "role": "user", "content": "问",
        },
    },
    # 5. ended_at 早于 started_at（异常）→ max(0, ...) 保底为 0
    {
        "name": "时间戳顺序异常 → duration 保底为 0",
        "input": dict(
            msg_id="ai-5", role="ai", content="答", reasoning="思考",
            tool_calls=None,
            reasoning_started_at_ms=3500, reasoning_ended_at_ms=1000,  # 倒序
        ),
        "expect": {
            "id": "ai-5", "role": "ai", "content": "答",
            "reasoning": "思考",
            "reasoning_duration_ms": 0,
            "reasoning_started_at": datetime.fromtimestamp(3.5, tz=timezone.utc).isoformat(),
        },
    },
]


# ─── 运行 ───
def main() -> int:
    print("=== Spike: chat.py 持久化时长字段验证 ===\n")
    failed = 0
    for i, case in enumerate(cases, 1):
        actual = build_entry(**case["input"])
        expected = case["expect"]
        # 比较实际 entry 的所有键是否与 expected 一致（expected 是子集）
        ok = True
        for k, v in expected.items():
            if actual.get(k) != v:
                ok = False
                print(f"[FAIL #{i}] {case['name']}: key={k!r} expected={v!r} got={actual.get(k)!r}")
        # 额外键（created_at）忽略
        for k in actual:
            if k not in expected and k != "created_at":
                # 注意：这里要严格——actual 不应有 expected 里没有的键
                # 但 created_at 是 datetime 对象不能比较，单独忽略
                ok = False
                print(f"[FAIL #{i}] {case['name']}: extra key={k!r} value={actual.get(k)!r}")
        if ok:
            print(f"[PASS #{i}] {case['name']}")
        else:
            failed += 1
            print(f"         actual={actual}\n")

    # ─── turn_duration_ms 测试 ───
    print("\n=== turn_duration_ms 测试 ===\n")
    history = [
        {"id": "u-1", "role": "user", "content": "问"},
        {"id": "ai-1", "role": "ai", "content": "答1", "reasoning": "思考1"},
        {"id": "ai-2", "role": "ai", "content": "答2", "reasoning": "思考2"},
    ]
    append_turn_duration(history, turn_started_at_ms=1000, turn_ended_at_ms=5500)
    expected_turn = 4500
    # 最后一条 AI（ai-2）应有 turn_duration_ms
    last_ai = history[-1]
    actual_turn = last_ai.get("turn_duration_ms")
    if actual_turn == expected_turn:
        print(f"[PASS] turn_duration_ms 写到 history 最后一条 AI: {actual_turn}ms")
    else:
        failed += 1
        print(f"[FAIL] expected {expected_turn}, got {actual_turn}")
        print(f"       history[-1] = {last_ai}")

    # 中间 AI（ai-1）不应有 turn_duration_ms
    middle_ai = history[1]
    if "turn_duration_ms" not in middle_ai:
        print(f"[PASS] turn_duration_ms 不写到中间 AI 消息")
    else:
        failed += 1
        print(f"[FAIL] 中间 AI 不应有 turn_duration_ms: {middle_ai}")

    # turn_started_at_ms 为 None → 不写
    history2 = [{"id": "ai-1", "role": "ai", "content": "答"}]
    append_turn_duration(history2, turn_started_at_ms=None, turn_ended_at_ms=5000)
    if "turn_duration_ms" not in history2[0]:
        print(f"[PASS] turn_started_at=None → 不写 turn_duration_ms")
    else:
        failed += 1
        print(f"[FAIL] turn_started_at=None 时不应写: {history2[0]}")

    print(f"\n=== 总结: {failed} 失败 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())