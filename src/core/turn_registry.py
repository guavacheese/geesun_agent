"""在跑对话轮次登记表（2026-09-14）。

用途：删除会话端点据此返回 **409 拒绝**并发删除（deer-flow 式护栏，
参考其 threads.py 的 `reserve_thread_operation` + ConflictError → 409），
避免"删除刚清完、正在跑的轮次又把台账写回来"的竞态。

设计约束与取舍：

1. **单副本前提**：生产 stack 全部服务 1/1 副本，进程内 dict 即全局真相。
   若未来扩容到多副本，本表只覆盖当前进程，需换 DB 租约（sessions 行里
   存 `turn_lease_until`，删除端点读会话行时顺带判断）。
2. **陈旧判定兜底**：轮次异常终止（进程被杀、事件循环崩溃）时 release 可能
   没执行，故 is_active 要求"距最近心跳不超过 stale_ms"；心跳由 SSE 生成器
   的每个 chunk 与空转 ping 刷新。stale_ms 取 5 分钟——远大于心跳间隔
   （秒级），远小于用户对"删不掉"的容忍上限。
3. **不加锁**：所有读写都是同步 dict 操作，操作之间没有 await，事件循环
   不会在中途切走，天然原子。

注意：本模块只是"护栏"，不是唯一防线。chat.py 端点的 session_existed_at_start
快照守卫仍在（快照=True 且会话行已消失 → 整轮跳过持久化），两层叠加后
不存在产生孤儿行的交错（详见 AGENTS.md 2026-09-14 条目）。
"""

from __future__ import annotations

import time

# thread_id = f"{user_id}:{session_id}" -> 最近活跃时刻（毫秒）
_active: dict[str, int] = {}

# 距最近心跳超过该值即视为轮次已结束（异常终止路径的兜底）
STALE_MS = 300_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def acquire(thread_id: str) -> None:
    """登记轮次开始（SSE 生成器启动时）。"""
    _active[thread_id] = _now_ms()


def beat(thread_id: str) -> None:
    """刷新活跃时间（每个 SSE chunk / 空转心跳）。未登记则不新建（幂等）。"""
    if thread_id in _active:
        _active[thread_id] = _now_ms()


def release(thread_id: str) -> None:
    """轮次结束（含断连持久化完成之后）注销。重复调用无害。"""
    _active.pop(thread_id, None)


def is_active(thread_id: str, *, stale_ms: int = STALE_MS) -> bool:
    """是否有轮次正在跑。陈旧条目顺手清除，避免异常终止后永久拒绝删除。"""
    ts = _active.get(thread_id)
    if ts is None:
        return False
    if _now_ms() - ts > stale_ms:
        _active.pop(thread_id, None)
        return False
    return True


def active_count() -> int:
    """当前活跃轮次数（诊断用，不计陈旧条目）。"""
    now = _now_ms()
    return sum(1 for ts in _active.values() if now - ts <= STALE_MS)
