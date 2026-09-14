# -*- coding: utf-8 -*-
"""Spike: 409 并发护栏（租约登记表 + 流式包装 + 删除端点拒绝）—— 2026-09-14。

不依赖 pytest，直接：python tests/spikes/turn_lease_guard.py

本 spike 测**结构与位置关系**，不做 ast exec 复刻（exec 注入 globals 会掩盖
import/接线错误——2026-09-12 的 message_key 漏 import 事故教训）。每条断言
对应一条设计约束：

  1. 登记表的生命周期语义：acquire/beat/release/is_active 齐备；
     陈旧判定（STALE_MS）存在且 is_active 会顺手清除陈旧项——
     否则进程被杀后该会话会永久删不掉。
  2. beat 不得新建条目（幂等）：未登记时 beat 是空操作，避免"心跳把已结束的
     轮次又登记回来"。
  3. chat.py 的包装层：release 必须在 **finally** 中（异常/断连都要注销）；
     beat 挂在 chunk 循环上；`_event_stream_inner` 定义在包装层之前，
     且 StreamingResponse 消费的是包装层。
  4. sessions.py 的护栏位置：409 检查必须在文件预备删除与 DB 事务**之前**
     （拒绝时零副作用），且在 404 存在性检查之后（404 语义优先）。
  5. 既有快照守卫不得被删（纵深防御：护栏是常见路径，守卫是残余窗口兜底）。
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG_PY = ROOT / "src" / "core" / "turn_registry.py"
CHAT_PY = ROOT / "src" / "api" / "endpoints" / "chat.py"
SESS_PY = ROOT / "src" / "api" / "endpoints" / "sessions.py"

_FAILS: list[str] = []
_passed = 0


def check(cond: bool, label: str) -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _FAILS.append(label)
        print(f"  FAIL  {label}")


reg_src = REG_PY.read_text(encoding="utf-8")
chat_src = CHAT_PY.read_text(encoding="utf-8")
sess_src = SESS_PY.read_text(encoding="utf-8")

# ─── 1. 登记表语义 ───
print("\n[1] turn_registry 生命周期语义")
reg_tree = ast.parse(reg_src)
funcs = {n.name: n for n in ast.walk(reg_tree) if isinstance(n, ast.FunctionDef)}
for fn in ("acquire", "beat", "release", "is_active"):
    check(fn in funcs, f"函数存在: {fn}()")
check("STALE_MS = " in reg_src, "陈旧阈值 STALE_MS 为模块常量（可测、可调）")
is_active_src = ast.unparse(funcs["is_active"]) if "is_active" in funcs else ""
check("_active.pop" in is_active_src,
      "is_active 顺手清除陈旧条目（异常终止后不会永久拒绝删除）")
beat_src = ast.unparse(funcs["beat"]) if "beat" in funcs else ""
check("if thread_id in _active" in beat_src,
      "beat 是空操作除非已登记（心跳不会把结束的轮次登记回来）")
release_src = ast.unparse(funcs["release"]) if "release" in funcs else ""
check("_active.pop(thread_id, None)" in release_src, "release 幂等（重复注销无害）")

# ─── 2. chat.py 包装层 ───
print("\n[2] chat.py 租约包装层")
check("from src.core import turn_registry" in chat_src, "显式导入 turn_registry（防漏 import 事故）")
chat_tree = ast.parse(chat_src)
inner_def = wrapper_def = None
acquire_line = beat_line = release_line = persist_line = None
release_in_finally = False
iterates_inner = False
for n in ast.walk(chat_tree):
    if isinstance(n, ast.AsyncFunctionDef):
        if n.name == "_event_stream_inner":
            inner_def = n.lineno
        elif n.name == "event_stream":
            wrapper_def = n.lineno
            # release 是否位于 try/finally 的 finalbody
            for sub in ast.walk(n):
                if isinstance(sub, ast.Try):
                    for stmt in sub.finalbody:
                        for c in ast.walk(stmt):
                            if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "release":
                                release_in_finally = True
                if isinstance(sub, ast.AsyncFor) and isinstance(sub.iter, ast.Call) \
                        and getattr(sub.iter.func, "id", "") == "_event_stream_inner":
                    iterates_inner = True
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
        full = f"{getattr(n.func.value, 'id', '')}.{n.func.attr}"
        if full == "turn_registry.acquire":
            acquire_line = n.lineno
        elif full == "turn_registry.beat":
            beat_line = n.lineno
        elif full == "turn_registry.release":
            release_line = n.lineno
        elif full == "_persist_session":
            persist_line = n.lineno

check(inner_def is not None, f"_event_stream_inner 已定义（L{inner_def}，函数体重命名）")
check(wrapper_def is not None and inner_def and wrapper_def > inner_def,
      f"包装层 event_stream 定义在内部生成器之后（L{wrapper_def} > L{inner_def}）")
check(iterates_inner, "包装层用 `async for ... in _event_stream_inner()` 消费内部生成器")
check(release_in_finally, "release 位于 finally（正常/异常/断连都注销）")
check(acquire_line and beat_line and release_line,
      f"acquire/beat/release 三处调用齐备（L{acquire_line}/L{beat_line}/L{release_line}）")
check(release_line and release_line > inner_def,
      "release 在包装层（源码位置晚于内部生成器）——顺序保证：内部 finally 的"
      "断连持久化先跑完，才注销租约")
check(beat_line and acquire_line and beat_line > acquire_line, "beat 在 acquire 之后")
check("event_stream()," in chat_src, "StreamingResponse 消费的是包装层（event_stream）")
check("session_existed_at_start" in chat_src,
      "既有快照守卫仍在（纵深防御：护栏覆盖常规路径，守卫兜底残余窗口）")

# ─── 3. sessions.py 护栏位置 ───
print("\n[3] delete_session 护栏")
sess_tree = ast.parse(sess_src)
line_404 = line_409 = line_move = line_atomic = None
for n in ast.walk(sess_tree):
    if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) \
            and getattr(n.exc.func, "id", "") == "HTTPException":
        kw = {k.arg: k.value for k in n.exc.keywords}
        v = kw.get("status_code")
        if isinstance(v, ast.Constant):
            if v.value == 404:
                line_404 = n.lineno
            elif v.value == 409:
                line_409 = n.lineno
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
        full = f"{getattr(n.func.value, 'id', '')}.{n.func.attr}"
        if full == "trash.move_to_trash":
            line_move = line_move or n.lineno
        elif full == "store.adelete_session_atomic":
            line_atomic = n.lineno
check("turn_registry.is_active(" in sess_src, "删除端点调用 is_active 判定")
check('f"{user_id}:{session_id}"' in sess_src,
      "thread_id 拼接必须与 chat 侧一致（user:session）——不一致则护栏形同虚设")
check(line_409 is not None, f"409 分支存在（L{line_409}）")
check("有对话正在进行" in sess_src, "409 文案明确（前端原样透出）")
check(line_404 and line_409 and line_404 < line_409,
      "404 存在性检查先于 409（不存在优先返回 404）")
check(line_409 and line_move and line_409 < line_move,
      f"409 在文件预备删除之前（L{line_409} < L{line_move}）——拒绝时零副作用")
check(line_409 and line_atomic and line_409 < line_atomic,
      "409 在 DB 事务之前（不会出现「删一半再拒绝」）")

print("\n" + "=" * 78)
print(f"PASS {_passed} / FAIL {len(_FAILS)}")
if _FAILS:
    for f in _FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PASS")
