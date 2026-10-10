# -*- coding: utf-8 -*-
"""②A 硬停标记链路 —— **镜像自带代码**的功能级验证（1.0.23 发布闸门 L1 最强判据）。

与 tests/core/test_loop_detection.py 的分工：
  · 那个跑在 pytest 里，验证「本机源码」的逻辑；
  · 本探针跑在**生产同款镜像**里（配 run 时不给 -v src），验证「出厂代码」的逻辑，
    且不依赖 pytest（生产镜像没有 pytest）。
把探针喂给 tests、把源码留给镜像 —— 这样「镜像里打包的那份代码」才是被测对象。

判据只看末行 `N PASS / 0 FAIL`（退出码在容器里有噪声，不可信）。

用法（本机构建机上）:
  docker run --rm -v "D:/workspace/geesun_agent/tests:/app/tests:ro" \
    --entrypoint sh 172.16.220.74:8333/geesun_ai/geesun-agent:1.0.23 \
    -c "cd /app && /app/.venv/bin/python tests/spikes/probe_loop_stop_1023.py"
  （关键：**不挂载 -v src**，脚本里的 src 才来自镜像）
"""
from __future__ import annotations

import inspect
import sys
from types import SimpleNamespace

sys.path.insert(0, "/app")

from langchain_core.messages import AIMessage  # noqa: E402

from src.core.loop_detection import (  # noqa: E402
    LOOP_FORCED_STOP_KEY,
    STOP_REASON_REPEAT_CALLS,
    STOP_REASON_TOOL_FREQUENCY,
    LoopDetectionMiddleware,
    LoopDetectionState,
)

_PASS = 0
_FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("  PASS %s%s" % (name, ("  | " + str(detail)) if detail else ""))
    else:
        _FAIL += 1
        print("  FAIL %s%s" % (name, ("  | " + str(detail)) if detail else ""))


_RUNTIME = SimpleNamespace(execution_info=SimpleNamespace(thread_id="probe_thread"))


def _call(name: str, args: dict) -> dict:
    return {"name": name, "args": args, "id": "call_%s_%s" % (name, abs(hash(str(args))) % 99999),
            "type": "tool_call"}


def _ai(*tool_calls) -> AIMessage:
    return AIMessage(content="", tool_calls=list(tool_calls))


def _state_with(messages) -> dict:
    return {"messages": list(messages)}


# ── 确认被测代码来自镜像，而不是某个挂载的本地副本 ────────────────────
src_file = inspect.getsourcefile(LoopDetectionState)
print("被测文件: %s" % src_file)
check("[0] 被测文件在镜像内 /app/src 下（未挂载本地源码）", "/app/src" in src_file, src_file)

# ── [1] state schema 暴露标记键（langchain 会据此合并进 graph state）──
check("[1] LoopDetectionState 注解含 loop_forced_stop",
      "loop_forced_stop" in LoopDetectionState.__annotations__,
      str(list(LoopDetectionState.__annotations__)))
check("[1] middleware.state_schema is LoopDetectionState",
      LoopDetectionMiddleware.state_schema is LoopDetectionState)

# ── [2] Layer 1 硬停：写标记 + 剥空 tool_calls ────────────────────────
mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=3)
call = _call("read_file", {"path": "/reports/u/s/a.txt"})
result = None
for _ in range(3):
    result = mw._apply(_state_with([_ai(call)]), _RUNTIME)
check("[2] 第 3 次同参调用触发硬停（returns update）", result is not None)
if result:
    marker = result.get(LOOP_FORCED_STOP_KEY) or {}
    check("[2] update 含 loop_forced_stop 标记", bool(marker), str(marker)[:140])
    check("[2] reason == repeat_calls", marker.get("reason") == STOP_REASON_REPEAT_CALLS,
          str(marker.get("reason")))
    check("[2] count >= 3", (marker.get("count") or 0) >= 3, str(marker.get("count")))
    check("[2] tool_names == ['read_file']", marker.get("tool_names") == ["read_file"],
          str(marker.get("tool_names")))
    check("[2] at 为数值时间戳（供跨轮复盘）", isinstance(marker.get("at"), (int, float)),
          str(marker.get("at")))
    check("[2] 原有语义不变：tool_calls 被剥空",
          result.get("messages") and result["messages"][0].tool_calls == [],
          str(result.get("messages") and result["messages"][0].tool_calls))
else:
    check("[2] 硬停标记链路（整段）", False, "result 为 None")

# ── [3] Layer 2 硬停：reason 可与 Layer 1 区分 ────────────────────────
mw2 = LoopDetectionMiddleware(warn_threshold=99, hard_limit=99,
                              tool_freq_warn=2, tool_freq_hard_limit=3)
r2 = None
for i in range(3):
    r2 = mw2._apply(_state_with([_ai(_call("read_file", {"path": "/reports/u/s/ch%d.txt" % i}))]),
                    _RUNTIME)
m2 = (r2 or {}).get(LOOP_FORCED_STOP_KEY) or {}
check("[3] 换参高频触发硬停且 reason == tool_frequency",
      m2.get("reason") == STOP_REASON_TOOL_FREQUENCY, str(m2.get("reason")))
check("[3] 带 tool_name（复盘可定位到具体工具）", m2.get("tool_name") == "read_file",
      str(m2.get("tool_name")))

# ── [4] 软提醒段不得写标记（否则会把正常轮次误终止）──────────────────
mw3 = LoopDetectionMiddleware(warn_threshold=2, hard_limit=5)
mw3._apply(_state_with([_ai(call)]), _RUNTIME)
r3 = mw3._apply(_state_with([_ai(call)]), _RUNTIME)
check("[4] 软提醒（warn 段）不写标记", r3 is None, str(r3))

# ── [5] 非工具消息不触发 ──────────────────────────────────────────────
mw4 = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
check("[5] 纯文本 AIMessage 不触发",
      mw4._apply(_state_with([AIMessage(content="just text")]), _RUNTIME) is None)

# ── [6] before_agent 开轮清零（防上一轮硬停误杀本轮）────────────────
mw5 = LoopDetectionMiddleware()
stale = _state_with([])
stale[LOOP_FORCED_STOP_KEY] = {"reason": STOP_REASON_REPEAT_CALLS, "count": 5}
check("[6] 有残留标记 → 开轮写入 {key: None}",
      mw5.before_agent(stale, _RUNTIME) == {LOOP_FORCED_STOP_KEY: None},
      str(mw5.before_agent(stale, _RUNTIME)))
check("[6] 干净 state → 不做无谓 state 写入", mw5.before_agent(_state_with([]), _RUNTIME) is None)

# ── [7] 常量集中定义（调用方不得硬编码字符串）────────────────────────
check("[7] 键值字面量与常量一致", LOOP_FORCED_STOP_KEY == "loop_forced_stop",
      LOOP_FORCED_STOP_KEY)

print("-" * 74)
print("%d PASS / %d FAIL" % (_PASS, _FAIL))
sys.exit(0 if _FAIL == 0 else 1)
