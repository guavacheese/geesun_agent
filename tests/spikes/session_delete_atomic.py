"""Spike: session 删除原子化（单事务 5 删 + 并发护栏）—— 2026-09-12 回归防线。

不依赖 pytest，直接：python tests/spikes/session_delete_atomic.py

取向说明（对齐 AGENTS.md 2026-09-12 教训）：本 spike 测**真实源码的结构与
位置关系**，不做 ast exec 复刻——exec 注入 globals 会掩盖接线错误。每条断言
对应一条曾在排查中确认的缺陷路径：

背景（改造前 sessions.py:434 的 delete_session）：
  1. checkpointer 三表（checkpoints / checkpoint_blobs / checkpoint_writes）
     **根本没删**——thread_id="{user}:{sid}"，单会话残留 171+46+217 行
     （生产 agent_mem_prod 实测），checkpoint_blobs 还存 channel 大对象；
  2. 会话行（aput(None)）与消息（adelete_prefix）分属两次独立写，中途失败
     = 半删除状态（行没了消息还在，或反之），无原子性；
  3. 删除期间在跑的对话轮结束时 _persist_session 会把刚删的行**写回来**
     （"会话不存在则创建"兼容分支正是复活路径）→ 孤儿数据。

目标语义（方案 A）：
  ① 文件先删（尽力，失败可重试删除会话；反之留不可见孤儿目录）
  ② DB 单事务 5 删（store 和 checkpointer 同库 agent_mem_prod，psycopg
     transaction 包住：sessions 行 + messages 前缀 + checkpointer 三表）
  ③ 并发护栏：chat 端点在 agent 开跑前快照 session_existed_at_start；
     _persist_session 发现"快照=True 且行已没了"→ 整轮跳过持久化
  ④ checkpointer 三表无外键（langgraph base.py MIGRATIONS 纯 PK 表，生产
     pg_constraint 实测一致），DELETE 顺序无关
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DB_PY = ROOT / "src" / "infra" / "database.py"
SESSIONS_PY = ROOT / "src" / "api" / "endpoints" / "sessions.py"
CHAT_PY = ROOT / "src" / "api" / "endpoints" / "chat.py"

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


db_src = DB_PY.read_text(encoding="utf-8")
sess_src = SESSIONS_PY.read_text(encoding="utf-8")
chat_src = CHAT_PY.read_text(encoding="utf-8")

# ─── 1. database.py：原子删除函数 ───
print("\n[1] adelete_session_atomic（database.py）")
check("async def adelete_session_atomic(" in db_src, "原子删除函数存在")
check("async with conn.transaction():" in db_src, "5 删包在同一个事务里（conn.transaction）")
# 以 def 行为锚切出函数体（函数名在文档字符串里也出现，不能按名字裸切）
_fn_seg = db_src[db_src.index("async def adelete_session_atomic("):]
_fn_seg = _fn_seg.split("async def awrite_messages")[0]
for tbl in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
    check(f'"{tbl}"' in _fn_seg, f"checkpointer 子表在删除范围内: {tbl}")
check('DELETE FROM store WHERE prefix = %s AND key = %s' in _fn_seg,
      "sessions 行按 prefix+key 精确删（不误删其他会话）")
check('raise ValueError("adelete_session_atomic 拒绝空 prefix/key' in db_src
      and 'adelete_session_atomic 非法 thread_id' in db_src,
      "空 prefix / 非法 thread_id 有入参守卫（防误删全表）")

# ─── 2. sessions.py：删除流程 ───
print("\n[2] delete_session 流程（sessions.py）")
check("adelete_session_atomic(" in sess_src, "删除端点走原子删除")
check("aput(namespace, session_id, None)" not in sess_src,
      "旧的 aput(None) 删行已移除（避免与事务内删除重复/竞态）")
check("adelete_prefix" not in sess_src,
      "旧的 adelete_prefix 独立删除已移除（并入单事务）")
check('"counts": counts' in sess_src, "响应带各表删除行数（验收可观测）")
check("删除会话失败，请重试" in sess_src, "事务失败 → 500 + 可重试语义（不静默吞）")

# 位置断言：文件删除（rmtree）必须在原子删除调用之前
sess_tree = ast.parse(sess_src)
rmtree_lineno = atomic_call_lineno = None
for n in ast.walk(sess_tree):
    if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "rmtree":
        rmtree_lineno = n.lineno
    if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "adelete_session_atomic":
        atomic_call_lineno = n.lineno
check(rmtree_lineno is not None and atomic_call_lineno is not None
      and rmtree_lineno < atomic_call_lineno,
      f"文件删除（L{rmtree_lineno}）在 DB 事务（L{atomic_call_lineno}）之前"
      "——事务失败时会话仍可见可重试，不留不可见孤儿目录")

# ─── 3. chat.py：并发护栏 ───
print("\n[3] 防复活护栏（chat.py）")
check("session_existed_at_start = (" in chat_src, "端点开跑前快照会话存在性")
persist_def = chat_src.index("async def _persist_session(")
snapshot_def = chat_src.index("session_existed_at_start = (")
check(snapshot_def < persist_def,
      "快照在 _persist_session 定义之前（闭包变量对两处调用可见）")
guard = chat_src[persist_def:]
check("会话 %s 在本轮进行中被删除，跳过持久化" in guard
      and "session_existed_at_start" in guard,
      "持久化入口有防复活守卫：快照=True 且行没了 → 整轮跳过")
check(guard.index("session_existed_at_start") < guard.index("awrite_messages"),
      "守卫在消息写入之前（复活路径拦在写入前，不是写完再补救）")

print("\n" + "=" * 78)
print(f"PASS {_passed} / FAIL {len(_FAILS)}")
if _FAILS:
    for f in _FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PASS")
